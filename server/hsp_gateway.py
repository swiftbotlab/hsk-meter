#!/usr/bin/env python3
"""HashKey Merchant (HSP) gateway integration for HSK Meter (Swift Bot Labs).

Built from the official HashKey Merchant API documentation (https://hashkey-merchant.github.io/en/, pages onboarding,
authentication, api-reference, cart-mandate, webhook and appendix, read 2026-10-27) and, where the docs are silent,
checked against the official SDKs (github.com/hashkey-merchant/sdks, commit 4ea5f61; read as data, not installed).
Terms follow AP2 (https://ap2-protocol.org): the merchant (receiving agent) signs a CART MANDATE, the payer (payment
agent) signs a PAYMENT MANDATE, and the HSP gateway verifies it and executes the transfer on-chain.

What is here (stdlib + the repo's pinned eth-* packages, no new dependencies)
  - canonical_json(): sorted keys, compact, the form the docs require for the HMAC body hash and cart_hash.
  - sign_request(): HMAC-SHA256 request signing, X-App-Key / X-Signature / X-Timestamp / X-Nonce, message
    "METHOD\\nPATH\\nQUERY\\nbodyHash\\ntimestamp\\nnonce" (authentication page).
  - ES256K (secp256k1 + SHA-256) JWTs for merchant_authorization, with a strict PEM/DER reader (SEC1 or PKCS8).
  - The documented token table, a Cart Mandate builder, and a validator that applies the documented amount and
    modifier rules (a cart the gateway would answer with 10001 is refused here first).
  - Gateway: create one-time / reusable orders, query payments, and the public checkout read
    GET /api/v1/checkout/orders/{flow_id} (route taken from the official Go SDK).
  - Webhooks: X-Signature "t=<unix>,v1=<hex>", HMAC over "<t>.<raw body>", 300 s tolerance, constant-time compare,
    and an idempotent 0600 JSONL journal (a retried or replayed delivery changes nothing).
Switch: HSP mode is OFF unless secrets/hsp/hsp.env holds APP_KEY, APP_SECRET and BASE_URL (load_config()).
Nothing here prints app_secret, the merchant private key, JWTs or signing strings.

  hsp_gateway.py status                         config check (never prints secrets)
  hsp_gateway.py create-order [--usd 0.01] [--network hashkey-testnet] [--reusable]
  hsp_gateway.py query (--payment-request-id ID | --cart-mandate-id ID | --flow-id ID) [--reusable]
  hsp_gateway.py checkout --flow-id ID
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from eth_keys.backends.native.ecdsa import (decompress_public_key, ecdsa_raw_sign, ecdsa_raw_verify,
                                            private_key_to_public_key)
from eth_keys.constants import SECPK1_N as N, SECPK1_P as P_FIELD

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SECRETS = os.path.join(os.path.dirname(os.path.dirname(ROOT)), "secrets", "hsp")
DEFAULT_ENV = os.path.join(SECRETS, "hsp.env")
DEFAULT_KEY = os.path.join(SECRETS, "merchant_private_key.pem")
PUBLIC_KEY_FILE = os.path.join(ROOT, "hsp_merchant_public_key.pem")

X402_METHOD = "https://www.x402.org/"
JWT_AUD_DOCS = "HashkeyMerchant"        # authentication page and the Go SDK; the Python/Node SDKs send "HashKeyMerchant"
API = "/api/v1"
ROUTES = {"orders": API + "/merchant/orders", "orders_reusable": API + "/merchant/orders/reusable",
          "payments": API + "/merchant/payments", "payments_reusable": API + "/merchant/payments/reusable",
          "checkout_order": API + "/checkout/orders/{flow_id}"}      # checkout route: official Go SDK, no auth
# Base URLs from the API reference. QA = testnet tokens; staging and production = mainnet tokens only.
HOSTS = {"merchant-qa.hashkeymerchant.com": "testnet", "merchant-stg.hashkeymerchant.com": "mainnet",
         "hsp.hashkey.com": "mainnet"}
STATUS_RANK = {"payment-required": 0, "payment-submitted": 1, "payment-verified": 2, "payment-processing": 3,
               "payment-included": 4, "payment-safe": 5, "payment-finalized": 6, "payment-failed": 7}
TERMINAL = {"payment-finalized", "payment-failed"}
WEBHOOK_STATUSES = {"payment-included", "payment-safe", "payment-finalized", "payment-failed"}
MAX_RESPONSE = 1 << 20
MAX_WEBHOOK_BODY = 64 * 1024
SKEW_S = 300

# Supported chain/token rows, verbatim from the onboarding page (network, chainId, token, contract, decimals,
# protocol). eip712 = the token's EIP-712 domain (name, version), read on-chain on 2026-10-27 and checked against
# DOMAIN_SEPARATOR(); None = not checked, so a payer must not sign for it. Note: Base Sepolia USDC's name is "USDC",
# not "USD Coin" as in the official SDK's constants.
TOKENS = [
    ("ethereum", 1, "HSK", "0xe7c6bf469e97eeb0bfb74c8dbff5bd47d4c1c98a", 18, "permit2", True, None),
    ("ethereum", 1, "USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7", 6, "permit2", True, None),
    ("ethereum", 1, "USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6, "eip3009", True, None),
    ("hashkey", 177, "USDC", "0x054ed45810DbBAb8B27668922D110669c9D88D0a", 6, "eip3009", True, ("Bridged USDC", "2")),
    ("hashkey", 177, "USDT", "0xf1b50ed67a9e2cc94ad3c477779e2d4cbfff9029", 6, "permit2", True, None),
    ("base", 8453, "USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6, "eip3009", True, None),
    ("arbitrum", 42161, "USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6, "eip3009", True, None),
    ("arbitrum", 42161, "USDT", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6, "permit2", True, None),
    ("bsc", 56, "USDT", "0x55d398326f99059fF775485246999027B3197955", 18, "permit2", True, None),
    ("sepolia", 11155111, "USDC", "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238", 6, "eip3009", False, None),
    ("sepolia", 11155111, "USDT", "0xff5588b3b38dff1b4b49bfdcbf985e84d8751a0e", 6, "permit2", False, None),
    ("sepolia", 11155111, "HSK", "0x31bdac8e4b897e470b70ebe286f94245baa793c2", 18, "permit2", False, None),
    ("base-sepolia", 84532, "USDC", "0x036CbD53842c5426634e7929541eC2318f3dCF7e", 6, "eip3009", False, ("USDC", "2")),
    ("hashkey-testnet", 133, "USDC", "0x8FE3cB719Ee4410E236Cd6b72ab1fCDC06eF53c6", 6, "eip3009", False, ("USD Coin", "2")),
    ("hashkey-testnet", 133, "USDT", "0x372325443233fEbaC1F6998aC750276468c83CC6", 6, "permit2", False, None),
    ("bsc-testnet", 97, "USDT", "0xBDec532A2675D3a6a091624b1FE878Ff58a43997", 18, "permit2", False, None),
    ("arbitrum-sepolia", 421614, "USDT", "0xc52f0FD29292393D253420205BF1dDCc5a0ED224", 6, "permit2", False, None),
    ("arbitrum-sepolia", 421614, "USDC", "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d", 6, "eip3009", False, None),
]
TOKEN_FIELDS = ("network", "chain_id", "coin", "contract", "decimals", "protocol", "mainnet", "eip712")

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")          # ids WE create
FOREIGN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")  # ids we accept from the gateway
DEC_RE = re.compile(r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,18})?$")
FLOW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,63}$")
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$")


class HspError(Exception):
    pass


class CartError(HspError):
    """A Cart Mandate that the documented rules reject (the gateway answers these with 10001)."""


class HspApiError(HspError):
    def __init__(self, http_status, code, msg, request_id=None):
        self.http_status, self.code, self.msg, self.request_id = http_status, code, msg, request_id
        super().__init__(f"HSP API error http={http_status} code={code}: {msg}")


def token_rows():
    return [dict(zip(TOKEN_FIELDS, r)) for r in TOKENS]


def token_for(network, coin):
    for r in token_rows():
        if r["network"] == network and r["coin"] == coin:
            return r
    raise HspError(f"{coin} on {network} is not in the documented token table")


def token_by_contract(chain_id, contract):
    for r in token_rows():
        if r["chain_id"] == chain_id and r["contract"].lower() == str(contract).lower():
            return r
    return None


# ---------------------------------------------------------------- canonical JSON (docs: cart-mandate, appendix)

MAX_SAFE_INT = 2 ** 53 - 1


def _utf8(s):
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise HspError("string is not valid Unicode (lone surrogate)") from None


def _check_json(v, depth=0):
    if depth > 32:
        raise HspError("JSON nested too deeply")
    if v is None or isinstance(v, bool):
        return
    if isinstance(v, int):
        if abs(v) > MAX_SAFE_INT:       # JavaScript's JSON.stringify would print another number
            raise HspError("integer outside the IEEE-754 safe range")
        return
    if isinstance(v, float):
        raise HspError("floats are not allowed in signed JSON; use decimal strings")
    if isinstance(v, str):
        _utf8(v)
        return
    if isinstance(v, (list, tuple)):
        for x in v:
            _check_json(x, depth + 1)
        return
    if isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str):
                raise HspError("object keys must be strings")
            _utf8(k)
            _check_json(x, depth + 1)
        return
    raise HspError(f"type {type(v).__name__} is not JSON")


def _dump(v):
    if isinstance(v, dict):
        # JSON.stringify(sortKeys(x)) in the docs sorts by UTF-16 code units; so do we.
        items = sorted(v.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _dump(x) for k, x in items) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_dump(x) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False)


def canonical_json(v):
    """Sorted keys (recursively), arrays in order, no whitespace (RFC 8785 for the value types we allow)."""
    _check_json(v)
    return _dump(v)


def sha256_hex(data):
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else bytes(data)).hexdigest()


def cart_hash(contents):
    return sha256_hex(canonical_json(contents))


def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise HspError(f"duplicate JSON key {k[:40]!r}")
        out[k] = v
    return out


def _bad_constant(name):
    raise HspError(f"JSON constant {name} is not allowed")


def _bad_float(text):
    raise HspError("floats are not allowed")


def loads_strict(raw, limit=MAX_RESPONSE):
    """Parse untrusted JSON: size cap, UTF-8, no duplicate keys, no NaN/Infinity, no floats."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > limit:
        raise HspError("JSON too large")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_bad_constant,
                          parse_float=_bad_float)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        if isinstance(e, HspError):
            raise
        raise HspError("invalid JSON") from None


# ---------------------------------------------------------------- HMAC request signing (docs: authentication)

def body_hash(body):
    """hex(SHA256(canonicalJSON(body))); the empty string when there is no body. We always SEND the canonical
    serialization, so the hash of the raw bytes and the hash of the canonical form are the same value."""
    return "" if not body else hashlib.sha256(body).hexdigest()


def signing_message(method, path, query, bhash, timestamp, nonce):
    return "\n".join([method.upper(), path, query, bhash, str(int(timestamp)), nonce])


def hmac_hex(secret, message):
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_request(app_key, app_secret, method, path, query="", body=None, timestamp=None, nonce=None):
    if not path.startswith(API + "/merchant/") or "?" in path or "\n" in path:
        raise HspError("HMAC signing is for /api/v1/merchant/* paths only (query separate, no '?')")
    if "\n" in query or query.startswith("?"):
        raise HspError("query must be the raw query string without '?'")
    ts = int(time.time()) if timestamp is None else int(timestamp)
    nonce = nonce or secrets.token_hex(16)
    if not re.fullmatch(r"[0-9a-fA-F]{12,32}|[0-9a-fA-F-]{36}", nonce):
        raise HspError("nonce must be 12-32 hex chars or a UUID")
    msg = signing_message(method, path, query, body_hash(body), ts, nonce)
    return {"X-App-Key": app_key, "X-Timestamp": str(ts), "X-Nonce": nonce, "X-Signature": hmac_hex(app_secret, msg)}


# ---------------------------------------------------------------- PEM / DER / ES256K

_OID_EC = bytes.fromhex("2a8648ce3d0201")      # 1.2.840.10045.2.1 id-ecPublicKey
_OID_K1 = bytes.fromhex("2b8104000a")          # 1.3.132.0.10 secp256k1


def _der(buf, i=0):
    if i + 2 > len(buf):
        raise HspError("truncated DER")
    tag, ln, i = buf[i], buf[i + 1], i + 2
    if ln & 0x80:
        n = ln & 0x7F
        if n == 0 or n > 2 or i + n > len(buf):
            raise HspError("bad DER length")
        ln, i = int.from_bytes(buf[i:i + n], "big"), i + n
    if i + ln > len(buf):
        raise HspError("truncated DER")
    return tag, buf[i:i + ln], i + ln


def _der_items(buf):
    out, i = [], 0
    while i < len(buf):
        t, v, i = _der(buf, i)
        out.append((t, v))
    return out


def _der_whole(buf, tag):
    t, v, end = _der(buf, 0)
    if t != tag or end != len(buf):
        raise HspError("unexpected DER structure")
    return v


def _pem_blocks(text):
    blocks = re.findall(r"-----BEGIN ([A-Z ]+)-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END \1-----", text)
    out = {}
    for label, b64 in blocks:
        try:
            out[label] = base64.b64decode("".join(b64.split()), validate=True)
        except ValueError:
            raise HspError("bad base64 in PEM") from None
    return out


def _sec1_private(der, need_curve):
    items = _der_items(_der_whole(der, 0x30))
    if len(items) < 2 or items[0] != (0x02, b"\x01") or items[1][0] != 0x04 or len(items[1][1]) != 32:
        raise HspError("not an EC private key (SEC1)")
    curve_ok = False
    for t, v in items[2:]:
        if t == 0xA0:
            if _der_whole(v, 0x06) != _OID_K1:
                raise HspError("EC key is not on secp256k1 (ES256K needs secp256k1)")
            curve_ok = True
    if need_curve and not curve_ok:
        raise HspError("EC key does not name its curve")
    d = int.from_bytes(items[1][1], "big")
    if not 1 <= d < N:
        raise HspError("EC private key out of range")
    return d


def parse_private_pem(text):
    """secp256k1 private key from PEM: SEC1 ("EC PRIVATE KEY") or PKCS8 ("PRIVATE KEY"). Returns the scalar."""
    blocks = _pem_blocks(text)
    if "EC PRIVATE KEY" in blocks:
        return _sec1_private(blocks["EC PRIVATE KEY"], need_curve=True)
    if "PRIVATE KEY" in blocks:
        items = _der_items(_der_whole(blocks["PRIVATE KEY"], 0x30))
        if len(items) < 3 or items[0] != (0x02, b"\x00") or items[1][0] != 0x30 or items[2][0] != 0x04:
            raise HspError("not a PKCS8 private key")
        alg = _der_items(items[1][1])
        if alg != [(0x06, _OID_EC), (0x06, _OID_K1)]:
            raise HspError("PKCS8 key is not an EC secp256k1 key")
        return _sec1_private(items[2][1], need_curve=False)
    raise HspError("no EC private key block in PEM")


def _point_ok(x, y):
    return 0 <= x < P_FIELD and 0 <= y < P_FIELD and (y * y - x * x * x - 7) % P_FIELD == 0


def parse_public_pem(text):
    """secp256k1 public key from a SubjectPublicKeyInfo PEM. Returns 64 bytes X||Y (point checked on the curve)."""
    blocks = _pem_blocks(text)
    if "PUBLIC KEY" not in blocks:
        raise HspError("no PUBLIC KEY block in PEM")
    items = _der_items(_der_whole(blocks["PUBLIC KEY"], 0x30))
    if len(items) != 2 or items[0][0] != 0x30 or items[1][0] != 0x03:
        raise HspError("not a SubjectPublicKeyInfo")
    if _der_items(items[0][1]) != [(0x06, _OID_EC), (0x06, _OID_K1)]:
        raise HspError("public key is not EC secp256k1")
    bits = items[1][1]
    if not bits or bits[0] != 0:
        raise HspError("bad BIT STRING")
    pt = bits[1:]
    if len(pt) == 65 and pt[0] == 4:
        xy = pt[1:]
    elif len(pt) == 33 and pt[0] in (2, 3):
        xy = decompress_public_key(pt)
    else:
        raise HspError("bad EC point encoding")
    if not _point_ok(int.from_bytes(xy[:32], "big"), int.from_bytes(xy[32:], "big")):
        raise HspError("public key is not on secp256k1")
    return bytes(xy)


def public_pem(xy):
    spki = (bytes.fromhex("3056301006072a8648ce3d020106052b8104000a034200") + b"\x04" + bytes(xy))
    b64 = base64.b64encode(spki).decode()
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64)) + "\n-----END PUBLIC KEY-----\n"


def read_secret_file(path, limit=16384):
    """A secret file must be a regular file (no symlink), owned by us, mode 0600 or stricter."""
    try:
        st_ = os.lstat(path)
    except OSError:
        raise HspError(f"missing secret file: {os.path.basename(path)}") from None
    if stat.S_ISLNK(st_.st_mode) or not stat.S_ISREG(st_.st_mode):
        raise HspError(f"secret file must be a regular file: {os.path.basename(path)}")
    if st_.st_mode & 0o077:
        raise HspError(f"secret file must be mode 0600: {os.path.basename(path)}")
    if st_.st_uid != os.getuid():
        raise HspError(f"secret file must be owned by this user: {os.path.basename(path)}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise HspError(f"secret file too large: {os.path.basename(path)}")
    return raw.decode("utf-8")


class MerchantKey:
    """The merchant's ES256K signing key. The scalar never leaves this object; repr() shows the public key only."""

    def __init__(self, d):
        self._d = d.to_bytes(32, "big")
        self.public = private_key_to_public_key(self._d)

    @classmethod
    def load(cls, path=DEFAULT_KEY):
        return cls(parse_private_pem(read_secret_file(path)))

    def sign_digest(self, digest):
        _, r, s = ecdsa_raw_sign(digest, self._d)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def public_pem(self):
        return public_pem(self.public)

    def __repr__(self):
        return f"MerchantKey(public={self.public[:4].hex()}...)"


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64url_decode(s):
    if not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", s) or len(s) % 4 == 1:
        raise HspError("bad base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


JWT_HEADER = {"alg": "ES256K", "typ": "JWT"}


def jwt_sign(claims, key):
    signing_input = b64url(json.dumps(JWT_HEADER, separators=(",", ":")).encode()) + "." + \
        b64url(canonical_json(claims).encode())
    return signing_input + "." + b64url(key.sign_digest(hashlib.sha256(signing_input.encode()).digest()))


def jwt_verify(token, public_xy):
    """Verify an ES256K JWT against a pinned public key. Returns the claims. Non-ES256K tokens are refused."""
    if not isinstance(token, str) or len(token) > 8192 or token.count(".") != 2:
        raise HspError("not a compact JWT")
    h64, p64, s64 = token.split(".")
    header = loads_strict(b64url_decode(h64), 1024)
    if not isinstance(header, dict) or header.get("alg") != "ES256K" or header.get("typ", "JWT") != "JWT" \
            or set(header) - {"alg", "typ", "kid"}:
        raise HspError("JWT header must be {alg: ES256K, typ: JWT}")
    sig = b64url_decode(s64)
    if len(sig) != 64:
        raise HspError("ES256K signature must be 64 bytes (r||s)")
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if not (0 < r < N and 0 < s < N):
        raise HspError("signature out of range")
    if not ecdsa_raw_verify(hashlib.sha256((h64 + "." + p64).encode()).digest(), (r, s), bytes(public_xy)):
        raise HspError("merchant_authorization signature does not verify")
    claims = loads_strict(b64url_decode(p64), 4096)
    if not isinstance(claims, dict):
        raise HspError("JWT claims must be an object")
    return claims


def merchant_authorization(contents, merchant_name, key, aud=JWT_AUD_DOCS, ttl_s=600, now=None):
    """The ES256K merchant_authorization JWT: iss = sub = merchant name, aud, iat, exp, jti, cart_hash."""
    now = int(time.time()) if now is None else int(now)
    claims = {"iss": merchant_name, "sub": merchant_name, "aud": aud, "iat": now, "exp": now + int(ttl_s),
              "jti": f"JWT-{now}-{secrets.token_hex(8)}", "cart_hash": cart_hash(contents)}
    return jwt_sign(claims, key)


def check_merchant_claims(claims, contents, aud=JWT_AUD_DOCS, now=None, leeway=60):
    """Claims of a verified merchant_authorization against the cart it came with. Raises HspError."""
    now = int(time.time()) if now is None else int(now)
    for k, t in (("iss", str), ("sub", str), ("aud", str), ("jti", str), ("cart_hash", str), ("iat", int), ("exp", int)):
        if not isinstance(claims.get(k), t) or isinstance(claims.get(k), bool):
            raise HspError(f"merchant_authorization claim {k} missing or mistyped")
    if claims["aud"] not in (aud if isinstance(aud, (set, tuple, list)) else (aud,)):
        raise HspError("merchant_authorization audience mismatch")
    if claims["iss"] != contents.get("merchant_name") or claims["sub"] != contents.get("merchant_name"):
        raise HspError("merchant_authorization issuer is not the cart's merchant")
    if claims["iat"] > now + leeway or claims["exp"] <= now:
        raise HspError("merchant_authorization expired or not yet valid")
    if not hmac.compare_digest(claims["cart_hash"], cart_hash(contents)):
        raise HspError("cart_hash does not match the cart contents (tampered cart)")
    return True


# ---------------------------------------------------------------- Cart Mandate (docs: cart-mandate, api-reference)

def dec(value, what="amount"):
    if not isinstance(value, str) or not DEC_RE.match(value):
        raise CartError(f"{what} must be a decimal string")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise CartError(f"{what} is not a decimal") from None


def usd_from_units(units, decimals=6):
    s = format(Decimal(int(units)).scaleb(-decimals).normalize(), "f")
    if "." not in s:
        s += ".00"
    elif len(s.split(".")[1]) == 1:
        s += "0"
    return s


def units_from_usd(value, decimals):
    """USD stablecoin value -> token base units at 1:1. Refuses values finer than the token's decimals."""
    q = dec(value).scaleb(decimals)
    if q != q.to_integral_value():
        raise CartError(f"{value} has more decimals than the token ({decimals})")
    return int(q)


def _norm_method(m):
    return m.strip().rstrip("/") if isinstance(m, str) else None


def _amount(obj, what):
    if not isinstance(obj, dict) or not isinstance(obj.get("amount"), dict):
        raise CartError(f"{what} needs label + amount")
    if not isinstance(obj.get("label"), str) or not obj["label"]:
        raise CartError(f"{what}.label must be a non-empty string")
    a = obj["amount"]
    if a.get("currency") != "USD":
        raise CartError(f"{what} currency must be USD")
    return dec(a.get("value"), what + ".amount.value")


def rfc3339_ts(s):
    if not isinstance(s, str) or not RFC3339_RE.match(s):
        raise CartError("cart_expiry must be RFC 3339")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise CartError("cart_expiry must be RFC 3339") from None


def rfc3339(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_cart_contents(c):
    """Apply the documented structure and amount rules. Returns {method index: effective total (Decimal)}.
    Raises CartError for anything the docs say the gateway rejects with 10001."""
    if not isinstance(c, dict):
        raise CartError("contents must be an object")
    for k in ("id", "merchant_name", "cart_expiry"):
        if not isinstance(c.get(k), str) or not c[k]:
            raise CartError(f"contents.{k} is required")
    if not FOREIGN_ID_RE.match(c["id"]):
        raise CartError("contents.id has unexpected characters")
    if not isinstance(c.get("user_cart_confirmation_required"), bool):
        raise CartError("contents.user_cart_confirmation_required must be a bool")
    rfc3339_ts(c["cart_expiry"])
    pr = c.get("payment_request")
    if not isinstance(pr, dict):
        raise CartError("contents.payment_request is required")
    md = pr.get("method_data")
    if not isinstance(md, list) or not md or len(md) > 16:
        raise CartError("method_data must be a non-empty array")
    for i, m in enumerate(md):
        if not isinstance(m, dict) or _norm_method(m.get("supported_methods")) != _norm_method(X402_METHOD):
            raise CartError(f"method_data[{i}].supported_methods must be {X402_METHOD}")
        d = m.get("data")
        if not isinstance(d, dict) or d.get("x402Version") != 2 or isinstance(d.get("x402Version"), bool):
            raise CartError(f"method_data[{i}].data.x402Version must be 2")
        if not isinstance(d.get("network"), str) or not isinstance(d.get("coin"), str):
            raise CartError(f"method_data[{i}] needs network and coin")
        if not isinstance(d.get("chain_id"), int) or isinstance(d.get("chain_id"), bool) or d["chain_id"] <= 0:
            raise CartError(f"method_data[{i}].data.chain_id must be a positive integer")
        for k in ("contract_address", "pay_to"):
            if not isinstance(d.get(k), str) or not ADDR_RE.match(d[k]):
                raise CartError(f"method_data[{i}].data.{k} must be an address")
    det = pr.get("details")
    if not isinstance(det, dict) or not isinstance(det.get("id"), str) or not FOREIGN_ID_RE.match(det["id"]):
        raise CartError("details.id (payment_request_id) is required")
    total = _amount(det.get("total"), "details.total")
    items = det.get("display_items") or []
    if not isinstance(items, list) or len(items) > 64:
        raise CartError("display_items must be an array")
    if items and sum((_amount(it, "display_items[]") for it in items), Decimal(0)) != total:
        raise CartError("sum(display_items) must equal details.total")
    eff = {i: total for i in range(len(md))}
    claimed = set()
    mods = det.get("modifiers") or []
    if not isinstance(mods, list) or len(mods) > 16:
        raise CartError("modifiers must be an array")
    for j, mod in enumerate(mods):
        if not isinstance(mod, dict) or not isinstance(mod.get("supported_methods"), str):
            raise CartError(f"modifiers[{j}].supported_methods is required")
        meth = _norm_method(mod["supported_methods"])
        adds = mod.get("additional_display_items")
        if not isinstance(adds, list) or not adds:
            raise CartError(f"modifiers[{j}].additional_display_items must be non-empty")
        extra = sum((_amount(a, f"modifiers[{j}].additional_display_items[]") for a in adds), Decimal(0))
        mtotal = None
        if mod.get("total") is not None:
            mtotal = _amount(mod["total"], f"modifiers[{j}].total")
            if mtotal <= 0:
                raise CartError(f"modifiers[{j}].total must be > 0")
            if mtotal != total + extra:
                raise CartError(f"modifiers[{j}].total must equal details.total + additional items")
        data = mod.get("data")
        if data is not None and not isinstance(data, dict):
            raise CartError(f"modifiers[{j}].data must be an object")
        idx = (data or {}).get("method_data_indexes")
        if idx is None:
            targets = [i for i, m in enumerate(md) if _norm_method(m["supported_methods"]) == meth]
        else:
            if not isinstance(idx, list) or not idx or len(set(map(repr, idx))) != len(idx):
                raise CartError(f"modifiers[{j}].method_data_indexes must be a non-empty array of unique indexes")
            for i in idx:
                if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(md):
                    raise CartError(f"modifiers[{j}].method_data_indexes has an out-of-range index")
                if _norm_method(md[i]["supported_methods"]) != meth:
                    raise CartError(f"modifiers[{j}] index {i} is a different payment method")
            targets = list(idx)
        if not targets:
            raise CartError(f"modifiers[{j}] matches no method_data entry")
        for i in targets:
            if i in claimed:
                raise CartError(f"method_data[{i}] is matched by two modifiers")
            claimed.add(i)
            if mtotal is not None:
                eff[i] = mtotal
    return eff


def method_data(network, coin, pay_to):
    t = token_for(network, coin)
    if not ADDR_RE.match(pay_to):
        raise CartError("pay_to must be an address")
    return {"supported_methods": X402_METHOD,
            "data": {"x402Version": 2, "network": t["network"], "chain_id": t["chain_id"],
                     "contract_address": t["contract"], "pay_to": pay_to, "coin": t["coin"]}}


def build_cart_contents(*, cart_id, payment_request_id, merchant_name, items, methods, expiry_s=7200, now=None,
                        user_confirmation=False):
    """items: [(label, usd value string)], methods: [method_data(...)]. Validated before it is returned."""
    if not ID_RE.match(cart_id) or not ID_RE.match(payment_request_id):
        raise CartError("cart_id / payment_request_id must match " + ID_RE.pattern)
    now = int(time.time()) if now is None else int(now)
    total = sum((dec(v) for _, v in items), Decimal(0))
    contents = {"id": cart_id, "user_cart_confirmation_required": bool(user_confirmation),
                "payment_request": {"method_data": list(methods),
                                    "details": {"id": payment_request_id,
                                                "display_items": [{"label": lb, "amount": {"currency": "USD", "value": v}}
                                                                  for lb, v in items],
                                                "total": {"label": "Total",
                                                          "amount": {"currency": "USD", "value": format(total, "f")}}}},
                "cart_expiry": rfc3339(now + int(expiry_s)), "merchant_name": merchant_name}
    validate_cart_contents(contents)
    return contents


def flow_id_from_url(payment_url):
    """The flow id is the last path segment of payment_url (docs: 'flow_id ... used in payment_url')."""
    try:
        u = urllib.parse.urlsplit(payment_url)
    except ValueError:
        return None
    seg = u.path.rstrip("/").rsplit("/", 1)[-1]
    return seg if FLOW_RE.match(seg or "") else None


# ---------------------------------------------------------------- config (the HSP switch)

PLACEHOLDER = re.compile(r"^(|REPLACE_ME.*|<.*>|changeme|xxx+)$", re.I)


def parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        out[k] = v
    return out


def check_base_url(url, allow_loopback=False, allow_mainnet=False):
    u = urllib.parse.urlsplit(url)
    if u.query or u.fragment or u.username or u.password or (u.path not in ("", "/")):
        raise HspError("BASE_URL must be scheme://host[:port] only")
    host = (u.hostname or "").lower()
    if u.scheme == "http" and allow_loopback and host in ("127.0.0.1", "localhost", "::1"):
        return url.rstrip("/"), "testnet"
    if u.scheme != "https" or host not in HOSTS or (u.port not in (None, 443)):
        raise HspError("BASE_URL must be one of the documented https hosts: " + ", ".join(sorted(HOSTS)))
    env = HOSTS[host]
    if env == "mainnet" and not allow_mainnet:
        raise HspError("staging/production take mainnet tokens only; this build is testnet-only "
                       "(set ALLOW_MAINNET=1 deliberately to change that)")
    return f"https://{host}", env


class HspConfig:
    def __init__(self, env, allow_loopback=False):
        self.app_key = env["APP_KEY"]
        self._app_secret = env["APP_SECRET"]
        self.allow_mainnet = env.get("ALLOW_MAINNET") == "1"
        self.base_url, self.environment = check_base_url(env["BASE_URL"], allow_loopback, self.allow_mainnet)
        self.merchant_name = env.get("MERCHANT_NAME") or "Swift Bot Labs"
        self.key_file = env.get("MERCHANT_PRIVATE_KEY_FILE") or DEFAULT_KEY
        self.jwt_aud = env.get("JWT_AUDIENCE") or JWT_AUD_DOCS
        self.jwt_ttl = int(env.get("JWT_TTL_SECONDS") or 600)
        self.pay_to = env.get("PAY_TO") or "0xA8A49A6Ca872D54a8FD5FB121139De9bff62a48D"
        self.networks = [n.strip() for n in (env.get("NETWORKS") or "hashkey-testnet,base-sepolia").split(",") if n.strip()]
        self.min_status = env.get("MIN_STATUS") or "payment-included"
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.app_key):
            raise HspError("APP_KEY has unexpected characters")
        if len(self._app_secret) < 8:
            raise HspError("APP_SECRET looks too short")
        if not ADDR_RE.match(self.pay_to):
            raise HspError("PAY_TO must be an address")
        if not 60 <= self.jwt_ttl <= 3600:
            raise HspError("JWT_TTL_SECONDS must be 60..3600")
        if self.min_status not in ("payment-included", "payment-safe", "payment-finalized"):
            raise HspError("MIN_STATUS must be payment-included, payment-safe or payment-finalized")
        for n in self.networks:
            rows = [r for r in token_rows() if r["network"] == n and r["coin"] == "USDC"]
            if not rows:
                raise HspError(f"network {n} has no USDC row in the documented table")
            if rows[0]["mainnet"] != (self.environment == "mainnet"):
                raise HspError(f"network {n} does not belong to the {self.environment} gateway")

    @property
    def app_secret(self):
        return self._app_secret

    def __repr__(self):
        return f"HspConfig(base_url={self.base_url!r}, app_key={self.app_key[:4]}..., networks={self.networks})"


def load_config(path=DEFAULT_ENV, allow_loopback=False):
    """HspConfig when HSP mode is ON, None when it is OFF (no file, or APP_KEY/APP_SECRET/BASE_URL missing or
    placeholders). A present but unsafe file (symlink, group/other-readable) or an invalid value raises HspError."""
    if not os.path.lexists(path):
        return None
    env = parse_env(read_secret_file(path))
    if any(PLACEHOLDER.match(env.get(k, "")) for k in ("APP_KEY", "APP_SECRET", "BASE_URL")):
        return None
    return HspConfig(env, allow_loopback=allow_loopback)


# ---------------------------------------------------------------- HTTP (no redirects, capped, JSON envelope)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HspApiError(code, None, "redirects are refused")


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_json(method, url, headers=None, body=None, timeout=15):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Accept": "application/json", "User-Agent": "hsk-meter-hsp/1",
                                          **({"Content-Type": "application/json"} if body is not None else {}),
                                          **(headers or {})})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            status, raw, rid = r.status, r.read(MAX_RESPONSE + 1), r.headers.get("x-request-id")
    except urllib.error.HTTPError as e:
        status, raw, rid = e.code, e.read(MAX_RESPONSE + 1), e.headers.get("x-request-id")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HspApiError(None, None, f"gateway unreachable: {type(e).__name__}") from None
    if len(raw) > MAX_RESPONSE:
        raise HspApiError(status, None, "response too large")
    try:
        env = loads_strict(raw)
    except HspError:
        raise HspApiError(status, None, "non-JSON response (HTML: a WAF block, or no API at this host/path)", rid) from None
    if not isinstance(env, dict) or not isinstance(env.get("code"), int):
        raise HspApiError(status, None, "response is not the {code,msg,data} envelope", rid)
    if env["code"] != 0 or not 200 <= status < 300:
        msg = re.sub(r"[^\x20-\x7e]", "?", str(env.get("msg", "")))[:200]
        raise HspApiError(status, env["code"], msg, rid)
    return env.get("data")


class Gateway:
    """Merchant-side client of the HSP gateway (receiving agent). All /merchant/* calls are HMAC-signed."""

    def __init__(self, cfg, key=None):
        self.cfg = cfg
        self.key = key or MerchantKey.load(cfg.key_file)

    def _signed(self, method, path, query="", body_obj=None):
        body = canonical_json(body_obj).encode("utf-8") if body_obj is not None else None
        hdr = sign_request(self.cfg.app_key, self.cfg.app_secret, method, path, query, body)
        return http_json(method, self.cfg.base_url + path + ("?" + query if query else ""), hdr, body)

    def sign_cart(self, contents):
        validate_cart_contents(contents)
        return {"contents": contents,
                "merchant_authorization": merchant_authorization(contents, self.cfg.merchant_name, self.key,
                                                                 self.cfg.jwt_aud, self.cfg.jwt_ttl)}

    def create_order(self, contents, reusable=False, redirect_url=None):
        if contents.get("merchant_name") != self.cfg.merchant_name:
            raise CartError("contents.merchant_name must be the configured merchant name (it is the JWT issuer)")
        body = {"cart_mandate": self.sign_cart(contents)}
        if redirect_url:
            if not redirect_url.startswith("https://") or len(redirect_url) > 512:
                raise HspError("redirect_url must be https and <= 512 chars")
            body["redirect_url"] = redirect_url
        data = self._signed("POST", ROUTES["orders_reusable" if reusable else "orders"], body_obj=body)
        if not isinstance(data, dict):
            raise HspApiError(200, 0, "create order: data is not an object")
        prid, url = data.get("payment_request_id"), data.get("payment_url")
        if prid != contents["payment_request"]["details"]["id"]:
            raise HspApiError(200, 0, "create order: payment_request_id differs from the cart's details.id")
        if not isinstance(url, str) or len(url) > 512 or not (url.startswith("https://") or
                                                               url.startswith(self.cfg.base_url + "/")):
            raise HspApiError(200, 0, "create order: payment_url is not an https URL")
        if bool(data.get("multi_pay")) != bool(reusable):
            raise HspApiError(200, 0, "create order: multi_pay does not match the endpoint used")
        return {"payment_request_id": prid, "payment_url": url, "flow_id": flow_id_from_url(url),
                "multi_pay": bool(reusable), "cart_mandate": body["cart_mandate"]}

    def query_payments(self, reusable=False, page=None, page_size=None, **ids):
        allowed = ("cart_mandate_id", "flow_id", "request_id") if reusable else ("cart_mandate_id", "payment_request_id", "flow_id")
        sel = [(k, v) for k, v in ids.items() if v is not None]
        if len(sel) != 1 or sel[0][0] not in allowed or not FOREIGN_ID_RE.match(str(sel[0][1])):
            raise HspError("exactly one of " + ", ".join(allowed) + " is required")
        q = list(sel)
        if reusable:
            for k, v in (("page", page), ("page_size", page_size)):
                if v is not None:
                    if not isinstance(v, int) or not 1 <= v <= 100:
                        raise HspError(f"{k} must be 1..100")
                    q.append((k, str(v)))
        return self._signed("GET", ROUTES["payments_reusable" if reusable else "payments"], urllib.parse.urlencode(q))


def checkout_order(base_url, flow_id, timeout=15):
    """Public checkout read (no auth; route from the official Go SDK): payment_request_id, cartmandate, status, amount."""
    if not FLOW_RE.match(str(flow_id)):
        raise HspError("bad flow_id")
    return http_json("GET", base_url + ROUTES["checkout_order"].format(flow_id=urllib.parse.quote(flow_id)), timeout=timeout)


def payment_record_ok(rec, order, min_status="payment-included", payer=None, min_ratio=Decimal("0.99")):
    """Does a payment record (merchant query API or verified webhook) pay `order`? Returns (ok, reason, method).
    The business-field checks the webhook page asks for (cart id, amounts, token) plus payee, chain, a tx hash and,
    when the receiving agent knows it, the payer. order = {cart_id, payment_request_id, usd, methods: [{chain_id,
    contract, pay_to, decimals}]}. Amounts are token-denominated; USDC carts are USD-denominated, so the record must
    carry at least min_ratio x the USD total (the gateway converts at its usd_amount_rate)."""
    if not isinstance(rec, dict):
        return False, "record is not an object", None
    status = rec.get("status")
    if status not in STATUS_RANK:
        return False, f"unknown status {str(status)[:40]}", None
    if status == "payment-failed":
        return False, "payment-failed", None
    if STATUS_RANK[status] < STATUS_RANK[min_status]:
        return False, f"pending ({status})", None
    if rec.get("content_id", rec.get("cart_mandate_id")) != order["cart_id"]:
        return False, "cart_mandate_id mismatch", None
    if rec.get("payment_request_id") not in (None, order["payment_request_id"]):
        return False, "payment_request_id mismatch", None
    method = None
    for m in order["methods"]:
        if (str(rec.get("token_address", "")).lower() == m["contract"].lower()
                and str(rec.get("to_pay_address", "")).lower() == m["pay_to"].lower()
                and rec.get("chain") in (None, f"eip155:{m['chain_id']}")):
            method = m
            break
    if method is None:
        return False, "token / payee / chain is not one of the cart's methods", None
    try:
        need = Decimal(order["usd"]) * min_ratio
        product = Decimal(str(rec.get("product_amount", rec.get("order_amount", "0"))))
        paid = Decimal(str(rec.get("pay_amount", rec.get("amount", "0"))))
    except (InvalidOperation, TypeError):
        return False, "amount is not a decimal", None
    if product < need or paid < need:
        return False, "amount below the cart total", None
    if not TX_RE.match(str(rec.get("tx_signature", ""))):
        return False, "no transaction hash yet", None
    if payer and str(rec.get("payer_address", "")).lower() != payer.lower():
        return False, "paid by another address", None
    return True, status, method


TESTNET_RPC = {133: "https://testnet.hsk.xyz", 84532: "https://sepolia.base.org"}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def rpc_call(chain_id, method, params, timeout=10):
    url = TESTNET_RPC.get(int(chain_id))
    if not url:
        raise HspError(f"no testnet RPC for chain {chain_id}")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "hsk-meter-hsp/1"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            j = loads_strict(r.read(MAX_RESPONSE + 1))
    except (urllib.error.URLError, TimeoutError, OSError, HspApiError) as e:
        raise HspError(f"RPC unavailable: {type(e).__name__}") from None
    if not isinstance(j, dict) or "error" in j:
        raise HspError("RPC error")
    return j.get("result")


def onchain_transfer_ok(chain_id, tx, token, frm, to, min_units, call=rpc_call):
    """Independent check of the gateway's claim: a successful tx with an ERC-20 Transfer(frm -> to, >= min_units)
    of `token`. Returns (ok, reason)."""
    if not TX_RE.match(str(tx)):
        return False, "bad tx hash"
    rc = call(chain_id, "eth_getTransactionReceipt", [tx.lower()])
    if not isinstance(rc, dict):
        return False, "transaction not found"
    if rc.get("status") != "0x1":
        return False, "transaction reverted"
    for lg in rc.get("logs") or []:
        t = lg.get("topics") or []
        if (str(lg.get("address", "")).lower() == token.lower() and len(t) == 3 and str(t[0]).lower() == TRANSFER_TOPIC
                and str(t[1]).lower()[-40:] == frm.lower()[2:] and str(t[2]).lower()[-40:] == to.lower()[2:]):
            try:
                if int(lg.get("data", "0x0"), 16) >= int(min_units):
                    return True, "transfer found"
            except ValueError:
                pass
    return False, "no matching token transfer in the transaction"


# ---------------------------------------------------------------- webhooks (docs: webhook)

def parse_signature_header(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise HspError("X-Signature header missing")
    ts, sigs = None, []
    for part in value.split(","):
        part = part.strip()
        if part.startswith("t="):
            if ts is not None or not re.fullmatch(r"[0-9]{1,12}", part[2:]):
                raise HspError("bad t= in X-Signature")
            ts = int(part[2:])
        elif part.startswith("v1="):
            if not re.fullmatch(r"[0-9a-fA-F]{64}", part[3:]):
                raise HspError("bad v1= in X-Signature")
            sigs.append(part[3:].lower())
    if ts is None or not sigs:
        raise HspError("X-Signature needs t= and v1=")
    return ts, sigs


def webhook_signature(secret, ts, raw):
    return hmac.new(secret.encode("utf-8"), str(int(ts)).encode() + b"." + bytes(raw), hashlib.sha256).hexdigest()


def verify_webhook(raw, header, secret, now=None, tolerance=SKEW_S):
    """Verify and parse one webhook delivery. Returns (event dict, (t, v1)). Raises HspError."""
    if not secret:
        raise HspError("webhook secret is required")
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_WEBHOOK_BODY:
        raise HspError("webhook body missing or too large")
    ts, sigs = parse_signature_header(header)
    now = time.time() if now is None else now
    if abs(now - ts) > tolerance:
        raise HspError("webhook timestamp out of tolerance")
    want = webhook_signature(secret, ts, raw)
    match = [s for s in sigs if hmac.compare_digest(want, s)]
    if not match:
        raise HspError("webhook signature mismatch")
    ev = loads_strict(bytes(raw), MAX_WEBHOOK_BODY)
    if not isinstance(ev, dict) or ev.get("event_type") != "payment":
        raise HspError("webhook event_type must be 'payment'")
    if ev.get("status") not in WEBHOOK_STATUSES:
        raise HspError("webhook status is not one the docs list")
    for k in ("payment_request_id", "cart_mandate_id"):
        if not isinstance(ev.get(k), str) or not FOREIGN_ID_RE.match(ev[k]):
            raise HspError(f"webhook {k} missing or malformed")
    if ev.get("request_id") is not None and not (isinstance(ev["request_id"], str) and FOREIGN_ID_RE.match(ev["request_id"])):
        raise HspError("webhook request_id malformed")
    return ev, (ts, match[0])


class WebhookJournal:
    """Idempotent store of verified webhook events (0600 JSONL, replayed at start).

    A delivery is applied at most once: the same (t, v1) is a replay; the same (payment key, status) is a retry;
    a status at or below the one we hold is stale; nothing moves a payment out of a terminal state. The payment key
    is request_id (ID5) when present, else payment_request_id (ID2), so reusable orders keep one row per payment."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.seen_sigs = {}         # (t, v1) -> t
        self.payments = {}          # key -> latest event
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    self.payments[row["key"]] = row["event"]
                    self.seen_sigs[tuple(row["sig"])] = row["sig"][0]

    @staticmethod
    def key(ev):
        return ev.get("request_id") or ev["payment_request_id"]

    def apply(self, ev, sig, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for s, t in list(self.seen_sigs.items()):
                if now - t > 2 * SKEW_S:
                    del self.seen_sigs[s]
            if tuple(sig) in self.seen_sigs:
                return "replay"
            self.seen_sigs[tuple(sig)] = sig[0]
            k = self.key(ev)
            cur = self.payments.get(k)
            if cur is not None:
                if cur.get("status") == ev["status"]:
                    return "duplicate"
                if cur.get("status") in TERMINAL or STATUS_RANK[ev["status"]] < STATUS_RANK[cur["status"]]:
                    return "stale"
            self.payments[k] = ev
            if self.path:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"key": k, "sig": list(sig), "event": ev}, separators=(",", ":")) + "\n")
            return "applied"

    def latest(self, key):
        with self.lock:
            return self.payments.get(key)

    def by_cart(self, cart_id):
        with self.lock:
            return [e for e in self.payments.values() if e.get("cart_mandate_id") == cart_id]


# ---------------------------------------------------------------- CLI (the live test once credentials arrive)

def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="hsp_gateway.py")
    ap.add_argument("--env", default=DEFAULT_ENV)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    co = sub.add_parser("create-order")
    co.add_argument("--usd", default="0.01")
    co.add_argument("--network", default="hashkey-testnet")
    co.add_argument("--reusable", action="store_true")
    co.add_argument("--label", default="HSK Meter: HSP gateway live check")
    qy = sub.add_parser("query")
    qy.add_argument("--payment-request-id")
    qy.add_argument("--cart-mandate-id")
    qy.add_argument("--flow-id")
    qy.add_argument("--request-id")
    qy.add_argument("--reusable", action="store_true")
    ck = sub.add_parser("checkout")
    ck.add_argument("--flow-id", required=True)
    a = ap.parse_args(argv)
    cfg = load_config(a.env)
    if cfg is None:
        print(json.dumps({"hsp": "off", "why": f"{a.env} missing or APP_KEY/APP_SECRET/BASE_URL not filled in"}))
        return 2
    key = MerchantKey.load(cfg.key_file)
    if a.cmd == "status":
        pinned = parse_public_pem(open(PUBLIC_KEY_FILE).read()) if os.path.exists(PUBLIC_KEY_FILE) else None
        print(json.dumps({"hsp": "on", "base_url": cfg.base_url, "environment": cfg.environment,
                          "merchant_name": cfg.merchant_name, "networks": cfg.networks, "pay_to": cfg.pay_to,
                          "jwt_audience": cfg.jwt_aud, "merchant_key_matches_public_pem": pinned == key.public}))
        return 0
    gw = Gateway(cfg, key)
    try:
        if a.cmd == "create-order":
            tag = secrets.token_hex(6)
            contents = build_cart_contents(cart_id=f"hskm-live-{tag}", payment_request_id=f"pr-live-{tag}",
                                           merchant_name=cfg.merchant_name, items=[(a.label, a.usd)],
                                           methods=[method_data(a.network, "USDC", cfg.pay_to)],
                                           expiry_s=(365 * 86400 if a.reusable else 7200))
            out = gw.create_order(contents, reusable=a.reusable)
            out.pop("cart_mandate")
            out["cart_mandate_id"] = contents["id"]
        elif a.cmd == "query":
            out = gw.query_payments(reusable=a.reusable, cart_mandate_id=a.cart_mandate_id,
                                    payment_request_id=None if a.reusable else a.payment_request_id,
                                    flow_id=a.flow_id, **({"request_id": a.request_id} if a.reusable else {}))
        else:
            out = checkout_order(cfg.base_url, a.flow_id)
    except HspApiError as e:
        print(json.dumps({"ok": False, "http": e.http_status, "code": e.code, "msg": e.msg, "x-request-id": e.request_id}))
        return 1
    print(json.dumps({"ok": True, "data": out}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
