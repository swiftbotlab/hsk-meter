"""hsk-paywall/v1 protocol pieces shared by the seller server and the buyer agent (Swift Bot Labs).

- requestId derivation (payer-bound, resource-bound, token-bound), identical to the Solidity/JS vector
- the EIP-3009 authorization nonce the contract expects: keccak256(abi.encode(contract, endpointId, requestId))
- EIP-712 typed data for USDC.e ReceiveWithAuthorization on HashKey Chain
- x402 v2 envelopes (PAYMENT-REQUIRED / PAYMENT-SIGNATURE / PAYMENT-RESPONSE: base64 JSON), with strict size caps
Everything that arrives from the network is data: parsed with size caps and strict patterns, never evaluated.
"""
import base64
import json
import re

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

DOMAIN_TAG = "hsk-paywall/v1"
DOMAIN = keccak(text=DOMAIN_TAG)
X402_VERSION = 2
CHAIN_ID = 177
NETWORK = "eip155:177"
USDCE = "0x054ed45810DbBAb8B27668922D110669c9D88D0a"
USDCE_NAME = "Bridged USDC"   # FiatTokenV2_2 EIP-712 domain name on 177 (checked: DOMAIN_SEPARATOR matches)
USDCE_VERSION = "2"
ZERO = "0x" + "0" * 40

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
B32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
SIG_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")          # 65-byte r||s||v (EOA)
UINT_RE = re.compile(r"^(0|[1-9][0-9]{0,77})$")
MAX_HEADER_B64 = 8192                                  # PAYMENT-SIGNATURE cap (chars)
MAX_JSON = 6144


class ProtocolError(ValueError):
    pass


def _word_uint(v):
    return int(v).to_bytes(32, "big")


def resource_hash(resource):
    if not isinstance(resource, str) or not (0 < len(resource) <= 2048):
        raise ProtocolError("bad resource")
    return keccak(text=resource)


def compute_request_id(chain_id, contract, endpoint_id, payer, resource, token):
    """digest = keccak256(abi.encode(bytes32 DOMAIN, uint256 chainId, address contract, uint256 endpointId,
    address payer, bytes32 keccak256(resource), bytes32 token)); requestId = payer (20 bytes) || digest[20:32]."""
    p = payer or ZERO
    if not ADDR_RE.match(contract) or not ADDR_RE.match(p) or not B32_RE.match(token):
        raise ProtocolError("bad requestId input")
    digest = keccak(encode(["bytes32", "uint256", "address", "uint256", "address", "bytes32", "bytes32"],
                           [DOMAIN, int(chain_id), to_checksum_address(contract), int(endpoint_id),
                            to_checksum_address(p), resource_hash(resource), bytes.fromhex(token[2:])]))
    rid = bytes.fromhex(p[2:].lower()) + digest[20:]
    return "0x" + digest.hex(), "0x" + rid.hex()


def payer_of(request_id):
    if not B32_RE.match(request_id or ""):
        raise ProtocolError("bad requestId")
    return "0x" + request_id[2:42].lower()


def authorization_nonce(contract, endpoint_id, request_id):
    if not ADDR_RE.match(contract) or not B32_RE.match(request_id):
        raise ProtocolError("bad nonce input")
    return "0x" + keccak(encode(["address", "uint256", "bytes32"],
                                [to_checksum_address(contract), int(endpoint_id), bytes.fromhex(request_id[2:])])).hex()


def receive_auth_typed_data(frm, to, value, valid_after, valid_before, nonce, chain_id=CHAIN_ID, token=USDCE):
    return {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
            "ReceiveWithAuthorization": [
                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}],
        },
        "primaryType": "ReceiveWithAuthorization",
        "domain": {"name": USDCE_NAME, "version": USDCE_VERSION, "chainId": int(chain_id),
                   "verifyingContract": to_checksum_address(token)},
        "message": {"from": to_checksum_address(frm), "to": to_checksum_address(to), "value": int(value),
                    "validAfter": int(valid_after), "validBefore": int(valid_before),
                    "nonce": bytes.fromhex(nonce[2:])},
    }


# ------------------------------------------------------------------ x402 v2 envelopes
def b64json(obj):
    return base64.b64encode(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()).decode()


def parse_b64json(value, max_chars=MAX_HEADER_B64):
    if not isinstance(value, str) or not (0 < len(value) <= max_chars):
        raise ProtocolError("payment header missing or too large")
    if not re.match(r"^[A-Za-z0-9+/=_-]+$", value):
        raise ProtocolError("payment header is not base64")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_" if ("-" in value or "_" in value) else None,
                               validate=False)
    except Exception:
        raise ProtocolError("payment header is not base64")
    if len(raw) > MAX_JSON:
        raise ProtocolError("payment payload too large")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ProtocolError("payment payload is not JSON")
    if not isinstance(obj, dict):
        raise ProtocolError("payment payload must be an object")
    return obj


def payment_required(*, url, description, resource, contract, endpoint_id, price, payer, token, request_id, nonce,
                     max_timeout, explorer, hsp_ext=None):
    """The 402 body (also sent base64 in PAYMENT-REQUIRED). Two ways to pay:
    accepts[0] scheme "exact" (x402 v2 shape): sign an EIP-3009 ReceiveWithAuthorization for exactly `amount` to the
                settlement contract with the given nonce; the server settles it (the payer needs no gas).
    accepts[1] scheme "hsk-paywall-tx": call pay(endpointId, requestId, amount) yourself, retry with the tx hash."""
    common = {"endpointId": str(endpoint_id), "requestId": request_id, "token": token, "payer": payer,
              "resource": resource, "protocol": DOMAIN_TAG}
    body = {
        "x402Version": X402_VERSION,
        "error": "payment required",
        "resource": {"url": url, "description": description, "mimeType": "application/json"},
        "accepts": [
            {"scheme": "exact", "network": NETWORK, "amount": str(price), "asset": USDCE, "payTo": contract,
             "maxTimeoutSeconds": max_timeout,
             "extra": {"name": USDCE_NAME, "version": USDCE_VERSION, "assetTransferMethod": "eip3009",
                       "primaryType": "ReceiveWithAuthorization", "settlement": "contract", "nonce": nonce,
                       **common}},
            {"scheme": "hsk-paywall-tx", "network": NETWORK, "amount": str(price), "asset": USDCE, "payTo": contract,
             "maxTimeoutSeconds": max_timeout,
             "extra": {"call": "approve(contract, amount) then pay(uint256 endpointId, bytes32 requestId, uint256 amount)",
                       "retryWith": {"X-Payer": payer, "X-Paywall-Token": token, "X-Paywall-Tx": "<pay tx hash>"},
                       **common}},
        ],
    }
    if explorer:
        body["accepts"][0]["extra"]["explorer"] = explorer
    if hsp_ext:
        body["extensions"] = {"hsp-draft": hsp_ext}
    return body


def _need(cond, msg):
    if not cond:
        raise ProtocolError(msg)


def validate_payment_payload(obj):
    """Shape check of an x402 v2 PaymentPayload for scheme "exact" (no business rules here). Returns
    (accepted: dict, auth: dict with ints, signature: str, extensions: dict)."""
    _need(obj.get("x402Version") == X402_VERSION, "x402Version must be 2")
    acc = obj.get("accepted")
    _need(isinstance(acc, dict), "accepted requirements missing")
    _need(acc.get("scheme") == "exact" and acc.get("network") == NETWORK, "only scheme exact on eip155:177")
    extra = acc.get("extra")
    _need(isinstance(extra, dict), "accepted.extra missing")
    for k in ("endpointId", "requestId", "token", "nonce", "resource"):
        _need(isinstance(extra.get(k), str), f"accepted.extra.{k} missing")
    _need(UINT_RE.match(extra["endpointId"]) is not None, "bad endpointId")
    _need(B32_RE.match(extra["requestId"]) and B32_RE.match(extra["token"]) and B32_RE.match(extra["nonce"]),
          "bad requestId/token/nonce")
    pl = obj.get("payload")
    _need(isinstance(pl, dict), "payload missing")
    sig = pl.get("signature")
    _need(isinstance(sig, str) and SIG_RE.match(sig), "signature must be 0x + 130 hex (65 bytes)")
    au = pl.get("authorization")
    _need(isinstance(au, dict), "authorization missing")
    for k in ("from", "to"):
        _need(isinstance(au.get(k), str) and ADDR_RE.match(au[k]), f"authorization.{k} must be an address")
    out = {"from": au["from"].lower(), "to": au["to"].lower()}
    for k in ("value", "validAfter", "validBefore"):
        v = au.get(k)
        _need(isinstance(v, str) and UINT_RE.match(v) and len(v) <= 30, f"authorization.{k} must be a decimal string")
        out[k] = int(v)
    _need(isinstance(au.get("nonce"), str) and B32_RE.match(au["nonce"]), "authorization.nonce must be bytes32")
    out["nonce"] = au["nonce"].lower()
    ext = obj.get("extensions") or {}
    _need(isinstance(ext, dict), "extensions must be an object")
    return acc, out, sig.lower(), ext
