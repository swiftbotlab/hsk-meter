#!/usr/bin/env python3
"""HSK Meter seller: HashKey Chain data APIs sold per request behind an x402 v2 paywall, settled in USDC.e through the
HskPayPerRequest contract (Swift Bot Labs).

  seller.py --key-dir DIR [--state-dir DIR] [--port 8402] [--host 127.0.0.1] [--contract 0x...]
            [--network mainnet|local --rpc URL --chain-id N]
  (--key-dir defaults to $HSK_METER_KEY_DIR, --state-dir to $HSK_METER_STATE_DIR or ./run)

Paid resources (one on-chain endpoint each; price read from the contract):
  GET /v1/hsk/address-profile?address=0x...   balances (HSK, USDC.e, USDT, WHSK), nonce, contract or EOA
  GET /v1/hsk/tx-receipt?hash=0x...           status, fee, ERC-20 transfers of a mined HashKey Chain transaction
                                              (a hash that is not on chain gets a free 404 before any payment)
Free: GET /  GET /health  GET /.well-known/x402

Flow (x402 v2): 1. GET with X-Payer: <address>  ->  402, PAYMENT-REQUIRED (base64 JSON, mirrored in the body)
  2a. scheme "exact": sign the EIP-3009 ReceiveWithAuthorization described in accepts[0] (to = contract, value = price,
      nonce = extra.nonce) and retry with PAYMENT-SIGNATURE: base64(PaymentPayload). The server checks the signature
      locally, checks the payer on-chain (plain account, balance, unused nonce), simulates payWithAuthorization with
      the real gas and fee fields, submits it (it pays the gas), checks the Paid event, does the work, answers 200 +
      PAYMENT-RESPONSE. If the work fails, the payment is refunded on-chain.
  2b. scheme "hsk-paywall-tx": call pay(endpointId, requestId, amount) yourself and retry with X-Payer and
      X-Paywall-Token (X-Paywall-Tx optional): the server reads getPayment(requestId) and the Paid log itself.
  Optional: sign the HSP-draft mandate offered in extensions["hsp-draft"] and send it in the payload's extensions; the
  answer then carries an adapter-signed receipt (one per payment) and the verifier's decision (served only on ACCEPT;
  a payment whose mandate is not accepted after settlement is refunded).
Keys: three files in --key-dir, each holding ONE value (provider.env: HKPW_PROVIDER_PK, adapter.env: HKPW_ADAPTER_PK,
token.env: PAYWALL_TOKEN_KEY), mode 0600, never created, logged or printed by the server. The provider key is hot (it
submits settlements and refunds); the payee key is never on this server, so a server compromise cannot withdraw
earned credit. State (settlements, serves, refunds, bans, gas spent) is journaled in --state-dir and replayed at start.
"""
import argparse
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import secrets
import socketserver
import stat
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chain as C        # noqa: E402
import hsp_draft as HSP  # noqa: E402
import protocol as P     # noqa: E402
import state as ST       # noqa: E402
from eth_abi import encode  # noqa: E402
from eth_account import Account  # noqa: E402

VERSION = "0.2.0"
ROOT = os.path.dirname(HERE)
USDT = "0xf1b50ed67a9e2cc94ad3c477779e2d4cbfff9029"
WHSK = "0xB210D2120d57b758EE163cFfb43e73728c471Cf1"
TRANSFER_TOPIC = "0x" + __import__("eth_utils").keccak(text="Transfer(address,address,uint256)").hex()
KNOWN_TOKENS = {P.USDCE.lower(): ("USDC.e", 6), USDT.lower(): ("USDT", 6), WHSK.lower(): ("WHSK", 18)}
RESOURCES = {
    "/v1/hsk/address-profile": {"endpointId": 1, "param": "address", "re": P.ADDR_RE,
                                "desc": "HashKey Chain address profile: HSK, USDC.e, USDT, WHSK balances, nonce, contract or EOA."},
    "/v1/hsk/tx-receipt": {"endpointId": 2, "param": "hash", "re": P.B32_RE,
                           "desc": "HashKey Chain transaction check: status, fee, ERC-20 transfers of a mined transaction."},
}
MAX_SERVES = 3              # a paid response can be fetched at most 3 times (lost-response retries)
SERVE_WINDOW = 3600         # ... within 1 h of the payment
MAX_TIMEOUT = 300
MIN_VALIDITY = 60           # an authorization must stay valid >= 60 s after it reaches us (time to settle)
HSP_MIN_DEADLINE = 120      # an HSP mandate must stay valid >= 120 s after it reaches us
HSP_CAPS = {HSP.CAP_SETTLED, HSP.CAP_REFUNDABLE}   # what this adapter can prove
BAN_S = 86400               # a payer whose settlement reverted on-chain is refused for 24 h
SETTLE_GAS_WORST = 246_503  # observed on mainnet: 178,123 (warm storage) .. 246,503 (first payment, all storage cold)
DEFAULTS = {"settle_per_min": 12, "payer_settle_per_min": 3, "refunds_per_payer_day": 2, "max_refund_attempts": 5,
            "settle_budget_wei": 2 * 10**18, "refund_budget_wei": 5 * 10**17, "hsk_usd": 0.10, "bind_host": "127.0.0.1",
            "smoke_refund_address": None, "state_dir": None, "chain_id": P.CHAIN_ID, "network": "mainnet", "tx_wait_s": 90}
LOOPBACK = {"127.0.0.1", "::1"}
KEY_VARS = {"provider.env": "HKPW_PROVIDER_PK", "adapter.env": "HKPW_ADAPTER_PK", "token.env": "PAYWALL_TOKEN_KEY"}


class NotFound(Exception):
    pass


class KeyFileError(RuntimeError):
    pass


def read_key_file(path, var):
    """Return the value of `var` from a key file that holds ONLY that one variable. Refuses symlinks, files readable
    by group/others, and files holding any other variable (so a multi-role key file can never be loaded by mistake)."""
    try:
        st_ = os.lstat(path)
    except OSError:
        raise KeyFileError(f"key file missing: {os.path.basename(path)}")
    if stat.S_ISLNK(st_.st_mode) or not stat.S_ISREG(st_.st_mode):
        raise KeyFileError(f"key file must be a regular file: {os.path.basename(path)}")
    if st_.st_mode & 0o077:
        raise KeyFileError(f"key file must be mode 0600: {os.path.basename(path)}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        raw = f.read(4096)
    vals = {}
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    if set(vals) != {var}:
        raise KeyFileError(f"{os.path.basename(path)} must hold exactly one variable, {var}")
    return vals[var]


def load_keys(key_dir):
    out = {}
    for fname, var in KEY_VARS.items():
        out[var] = read_key_file(os.path.join(key_dir, fname), var)
    tk = out["PAYWALL_TOKEN_KEY"]
    if not (len(tk) >= 64 and all(c in "0123456789abcdefABCDEF" for c in tk)):
        raise KeyFileError("PAYWALL_TOKEN_KEY must be >= 32 bytes of hex")
    return Account.from_key(out["HKPW_PROVIDER_PK"]), Account.from_key(out["HKPW_ADAPTER_PK"]), bytes.fromhex(tk)


class RateLimiter:
    """Fixed window per key; when full, the least recently used key is evicted (never a global reset)."""

    def __init__(self, limit, window_s, max_keys=5000):
        self.limit, self.window, self.max_keys = limit, window_s, max_keys
        self.b = {}
        self.lock = threading.Lock()

    def hit(self, key):
        with self.lock:
            now = time.monotonic()
            start, n = self.b.pop(key, (now, 0))
            if now - start > self.window:
                start, n = now, 0
            n += 1
            self.b[key] = (start, n)
            while len(self.b) > self.max_keys:
                self.b.pop(next(iter(self.b)))
            return max(1, int(start + self.window - now) + 1) if n > self.limit else 0

    def peek(self, key):
        """Seconds to wait if one more hit would be refused, else 0 (counts nothing)."""
        with self.lock:
            now = time.monotonic()
            start, n = self.b.get(key, (now, 0))
            if now - start > self.window:
                return 0
            return max(1, int(start + self.window - now) + 1) if n + 1 > self.limit else 0


def client_key(peer, headers, bind_host):
    """Rate-limit key of a request. CF-Connecting-IP is trusted only when this server listens on loopback and the
    connection comes from loopback (the local tunnel). IPv6 clients are keyed by their /64."""
    ip = peer
    if bind_host in LOOPBACK and peer in LOOPBACK:
        cf = headers.get("CF-Connecting-IP")
        if cf:
            try:
                ip = str(ipaddress.ip_address(str(cf).strip()))
            except ValueError:
                pass
    try:
        a = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return "invalid"
    if a.version == 6:
        if a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(a)


# ------------------------------------------------------------------ the paid work (real chain data)
def address_profile(rpc, addr):
    a = P.ADDR_RE.match(addr) and addr.lower()
    bal = lambda tok: C.eth_call(rpc, tok, "balanceOf(address)", ["address"], [a], ["uint256"])[0]  # noqa: E731
    code = rpc("eth_getCode", [a, "latest"], tries=3)
    hsk = int(rpc("eth_getBalance", [a, "latest"], tries=3), 16)
    nonce = int(rpc("eth_getTransactionCount", [a, "latest"], tries=3), 16)
    block = int(rpc("eth_blockNumber", [], tries=3), 16)
    return {"address": a, "chainId": P.CHAIN_ID, "block": block, "type": "contract" if len(code) > 2 else "eoa",
            "codeSize": (len(code) - 2) // 2, "nonce": nonce,
            "balances": {"HSK": f"{hsk / 1e18:.6f}", "USDC.e": f"{bal(P.USDCE) / 1e6:.6f}",
                         "USDT": f"{bal(USDT) / 1e6:.6f}", "WHSK": f"{bal(WHSK) / 1e18:.6f}"},
            "explorer": "https://hsk.blockscout.com/address/" + a}


def tx_receipt(rpc, h):
    rc = rpc("eth_getTransactionReceipt", [h.lower()], tries=3)
    if not rc:
        raise NotFound("no such transaction on HashKey Chain")
    tx = rpc("eth_getTransactionByHash", [h.lower()], tries=3) or {}
    fee = int(rc["gasUsed"], 16) * int(rc.get("effectiveGasPrice", "0x0"), 16)
    transfers = []
    for lg in rc.get("logs") or []:
        t = lg.get("topics") or []
        tok = str(lg.get("address", "")).lower()
        if tok in KNOWN_TOKENS and len(t) == 3 and t[0].lower() == TRANSFER_TOPIC:
            sym, dec = KNOWN_TOKENS[tok]
            v = int(lg["data"], 16)
            transfers.append({"token": sym, "from": "0x" + t[1][-40:], "to": "0x" + t[2][-40:], "amount": f"{v / 10**dec:.6f}"})
    return {"hash": h.lower(), "status": "success" if rc.get("status") == "0x1" else "reverted",
            "block": int(rc["blockNumber"], 16), "from": rc.get("from"), "to": rc.get("to"),
            "contractCreated": rc.get("contractAddress"), "valueHSK": f"{int(tx.get('value', '0x0'), 16) / 1e18:.6f}",
            "gasUsed": int(rc["gasUsed"], 16), "feeHSK": f"{fee / 1e18:.6f}", "logs": len(rc.get("logs") or []),
            "knownTokenTransfers": transfers[:20], "explorer": "https://hsk.blockscout.com/tx/" + h.lower()}


WORK = {"/v1/hsk/address-profile": address_profile, "/v1/hsk/tx-receipt": tx_receipt}


# ------------------------------------------------------------------ the seller
class Seller:
    """All request logic. Network and keys can be injected (tests); by default they come from cfg."""

    def __init__(self, cfg, rpc=None, provider=None, adapter=None, token_key=None, sender=None):
        self.cfg = cfg = {**DEFAULTS, **cfg}
        self.rpc = rpc or C.Rpc(cfg["rpc"], allow_local=cfg["network"] == "local")
        if provider is None or adapter is None or token_key is None:
            p, a, t = load_keys(cfg["key_dir"])
            provider, adapter, token_key = provider or p, adapter or a, token_key or t
        if len(token_key) < 32:
            raise KeyFileError("token key too short")
        self.provider, self.adapter, self.token_key = provider, adapter, token_key
        self.contract = cfg["contract"].lower()
        jpath = os.path.join(cfg["state_dir"], "seller_journal.jsonl") if cfg.get("state_dir") else None
        self.state = ST.SellerState(jpath)
        self.lock = self.state.lock                # one lock: reservations + state changes
        self.inflight = set()
        self.budgets = {"settle": int(cfg["settle_budget_wei"]), "refund": int(cfg["refund_budget_wei"])}
        self.sender = sender or C.Sender(self.rpc, provider, cfg["chain_id"], budgets=self.budgets,
                                         spent=self.state.gas_today(), on_spend=self._on_spend)
        self.quote_rl, self.verify_rl = RateLimiter(60, 60), RateLimiter(30, 60)
        self.payer_rl = RateLimiter(cfg["payer_settle_per_min"], 60)
        self.settle_rl = RateLimiter(cfg["settle_per_min"], 60, max_keys=1)   # counts only transactions sent
        self.cache, self.exists = {}, {}
        self.low_balance = False
        self.provider_balance = None
        self.smoke = (cfg.get("smoke_refund_address") or "").lower() or None

    def _on_spend(self, kind, wei):
        self.state.record("gas", kind=kind, wei=int(wei), day=ST.utc_day())

    # ---- chain state
    def endpoint(self, eid, max_age=1.0):
        c = self.cache.get(eid)
        if c and time.monotonic() - c[1] < max_age:
            return c[0]
        e = C.read_endpoint(self.rpc, self.contract, eid)
        if e["provider"] == P.ZERO:
            raise C.RpcError("endpoint not registered")
        self.cache[eid] = (e, time.monotonic())
        return e

    def resource_missing(self, path, arg):
        """Reason string when the paid resource cannot be delivered at all (checked before any payment), else None.
        tx-receipt: the transaction must be mined (positive answers cached 10 min)."""
        if path != "/v1/hsk/tx-receipt":
            return None
        h = arg.lower()
        seen = self.exists.get(h)
        if seen and time.monotonic() - seen < 600:
            return None
        rc = self.rpc("eth_getTransactionReceipt", [h], tries=2)
        if not rc:
            return "no such transaction on HashKey Chain (nothing to pay for)"
        if len(self.exists) > 5000:
            self.exists.clear()
        self.exists[h] = time.monotonic()
        return None

    def max_cost_wei(self, price_units):
        """Gas we accept to spend on one settlement: never more than the sale is worth (price in HSK at hsk_usd)."""
        return int(price_units * 10**12 / float(self.cfg["hsk_usd"]))

    def _ctx(self, eid, payer, resource):
        return f"{P.DOMAIN_TAG}|{self.cfg['chain_id']}|{self.contract}|{eid}|{payer}|{resource}".encode()

    def issue_token(self, eid, payer, resource):
        n = secrets.token_bytes(16)
        mac = hmac.new(self.token_key, self._ctx(eid, payer, resource) + n, hashlib.sha256).digest()[:16]
        return "0x" + (n + mac).hex()

    def token_ok(self, eid, payer, resource, token):
        raw = bytes.fromhex(token[2:])
        want = hmac.new(self.token_key, self._ctx(eid, payer, resource) + raw[:16], hashlib.sha256).digest()[:16]
        return hmac.compare_digest(raw[16:], want)

    # ---- per-request reservation (one thread per request id at a time)
    def reserve(self, rid, now=None):
        now = now or time.time()
        with self.lock:
            if rid in self.inflight:
                return 409, {"ok": False, "error": "in_progress", "retryable": True,
                             "message": "this payment is being processed; retry in a moment"}, {"Retry-After": "2"}
            rec = self.state.recs.get(rid)
            if rec:
                if rec.get("refunded") or rec.get("refund_failed") or rec.get("refund_deferred"):
                    return 410, {"ok": False, "error": "payment_refunded", "refund": {"tx": rec.get("refund_tx")},
                                 "message": "this payment was refunded (or its refund is queued); get a new quote"}, {}
                if rec.get("served", 0) >= MAX_SERVES:
                    return 429, {"ok": False, "error": "already_served", "message": f"served {MAX_SERVES} times already"}, {}
                if rec.get("settled") and rec.get("paidAt", now) + SERVE_WINDOW < now:
                    return 410, {"ok": False, "error": "payment_expired", "message": "paid more than 1 h ago"}, {}
            self.inflight.add(rid)
        return None

    def release(self, rid):
        with self.lock:
            self.inflight.discard(rid)

    # ---- request handling
    def handle_paid(self, path, query, headers, ip, log):
        spec = RESOURCES[path]
        q = urllib.parse.parse_qs(query, keep_blank_values=True, strict_parsing=False, max_num_fields=3)
        if list(q) != [spec["param"]] or len(q[spec["param"]]) != 1 or not spec["re"].match(q[spec["param"]][0]):
            return 400, {"ok": False, "error": "bad_request", "message": f"GET {path}?{spec['param']}=<0x...>"}, {}
        arg = q[spec["param"]][0].lower()
        resource = f"GET {path}?{spec['param']}={arg}"
        eid = spec["endpointId"]
        payer_h = headers.get("X-Payer")
        if payer_h is not None and not P.ADDR_RE.match(payer_h):
            return 400, {"ok": False, "error": "bad_payer", "message": "X-Payer must be 0x + 40 hex"}, {}
        payer = payer_h.lower() if payer_h else None
        sig_h = headers.get("PAYMENT-SIGNATURE") or headers.get("X-PAYMENT")
        if sig_h is not None:
            return self.settle_x402(path, eid, resource, arg, payer, sig_h, ip, log)
        if headers.get("X-Paywall-Tx") is not None or headers.get("X-Paywall-Token") is not None:
            return self.verify_tx(path, eid, resource, arg, payer, headers, ip, log)
        return self.quote(path, eid, resource, arg, payer, ip, log)

    def quote(self, path, eid, resource, arg, payer, ip, log):
        if payer is None:
            return 400, {"ok": False, "error": "payer_required",
                         "message": "Send X-Payer: <the address that will pay>; the requestId is bound to it."}, {}
        wait = self.quote_rl.hit(ip)
        if wait:
            return 429, {"ok": False, "error": "rate_limited"}, {"Retry-After": str(wait)}
        try:
            e = self.endpoint(eid)
            missing = self.resource_missing(path, arg)
        except C.RpcError as err:
            log["err"] = str(err)[:120]
            return 502, {"ok": False, "error": "chain_unavailable"}, {"Retry-After": "2"}
        if missing:
            return 404, {"ok": False, "error": "not_found", "message": missing}, {}
        if not e["active"]:
            return 503, {"ok": False, "error": "endpoint_inactive"}, {"Retry-After": "60"}
        token = self.issue_token(eid, payer, resource)
        _, rid = P.compute_request_id(self.cfg["chain_id"], self.contract, eid, payer, resource, token)
        nonce = P.authorization_nonce(self.contract, eid, rid)
        log["rid"] = rid[:18]
        mandate = HSP.mandate_to_json(HSP.mandate_message(
            payer=payer, payee=e["payee"], token=P.USDCE, amount=e["price"], reference=rid,
            required_caps=[HSP.CAP_SETTLED], deadline=int(time.time()) + MAX_TIMEOUT + 300))
        hsp_ext = {"draft": HSP.DRAFT_ID, "adapterId": HSP.ADAPTER_ID, "adapter": self.adapter.address,
                   "domain": {**HSP.DOMAIN, "chainId": self.cfg["chain_id"]}, "types": HSP.MANDATE_TYPES,
                   "mandate": mandate, "optional": True, "capabilities": sorted(HSP_CAPS),
                   "how": "sign the mandate (EIP-712) and send {mandate, signature} in PaymentPayload.extensions['hsp-draft']"}
        body = P.payment_required(url=path + "?" + resource.split("?", 1)[1], description=RESOURCES[path]["desc"],
                                  resource=resource, contract=self.contract, endpoint_id=eid, price=e["price"],
                                  payer=payer, token=token, request_id=rid, nonce=nonce, max_timeout=MAX_TIMEOUT,
                                  explorer="https://hsk.blockscout.com", hsp_ext=hsp_ext)
        return 402, body, {"PAYMENT-REQUIRED": P.b64json(body)}

    def parse_mandate(self, ext, payer, rid):
        """Local checks of an optional HSP-draft mandate (no RPC). Returns (mandate, hx) or (None, None); raises
        ValueError. The payee/amount terms and the deadline are checked where they are known (see callers)."""
        if "hsp-draft" not in ext:
            return None, None
        hx = ext.get("hsp-draft")
        if not isinstance(hx, dict):
            raise ValueError("extensions.hsp-draft must be an object")
        mandate = HSP.mandate_from_json(hx.get("mandate"))
        msig = hx.get("signature")
        if not isinstance(msig, str) or not P.SIG_RE.match(msig):
            raise ValueError("bad mandate signature encoding")
        if (mandate["payer"].lower() != payer or "0x" + mandate["reference"].hex() != rid
                or mandate["token"].lower() != P.USDCE.lower()):
            raise ValueError("mandate terms differ from this payment")
        caps = set(HSP.caps_list(mandate["requiredCapabilities"]))
        if not caps <= HSP_CAPS:
            raise ValueError("this adapter cannot prove: " + ",".join(sorted(caps - HSP_CAPS)))
        if HSP.recover_mandate(mandate, msig, self.cfg["chain_id"]).lower() != payer:
            raise ValueError("mandate not signed by the payer")
        return mandate, {"mandate": hx.get("mandate"), "signature": msig.lower()}

    @staticmethod
    def mandate_terms_ok(mandate, payee, amount):
        return mandate["payee"].lower() == payee.lower() and mandate["amount"] == amount

    def settle_x402(self, path, eid, resource, arg, payer_h, sig_h, ip, log):
        wait = self.verify_rl.hit(ip)
        if wait:
            return 429, {"ok": False, "error": "rate_limited"}, {"Retry-After": str(wait)}
        try:
            obj = P.parse_b64json(sig_h)
            acc, au, sig, ext = P.validate_payment_payload(obj)
        except P.ProtocolError as e:
            return 400, {"ok": False, "error": "bad_payment_payload", "message": str(e)}, {}
        x = acc["extra"]
        payer = au["from"]
        log["payer"] = payer
        checks = [
            (payer_h is None or payer_h == payer, "X-Payer differs from authorization.from"),
            (x["resource"] == resource and int(x["endpointId"]) == eid, "the payment is for another resource"),
            (acc["payTo"].lower() == self.contract and au["to"] == self.contract, "payTo/to must be the settlement contract"),
            (acc["asset"].lower() == P.USDCE.lower(), "asset must be USDC.e"),
            (self.token_ok(eid, payer, resource, x["token"].lower()), "token was not issued by this server for this payer and resource"),
        ]
        for ok, why in checks:
            if not ok:
                return 402, {"ok": False, "error": "payment_mismatch", "message": why}, {}
        _, rid = P.compute_request_id(self.cfg["chain_id"], self.contract, eid, payer, resource, x["token"].lower())
        log["rid"] = rid[:18]
        nonce = P.authorization_nonce(self.contract, eid, rid)
        if rid != x["requestId"].lower() or au["nonce"] != nonce:
            return 402, {"ok": False, "error": "payment_mismatch", "message": "requestId/nonce do not match this quote"}, {}
        # 1. the signature, locally: a forged one costs no RPC call, no limiter slot and no gas
        if P.recover_receive_auth(au, sig, self.cfg["chain_id"]) != payer:
            return 402, {"ok": False, "error": "bad_signature",
                         "message": "the authorization is not a valid signature of authorization.from"}, {}
        try:
            mandate, hx = self.parse_mandate(ext, payer, rid)
        except (ValueError, KeyError, TypeError, AttributeError) as err:
            return 400, {"ok": False, "error": "hsp_mandate_invalid", "message": str(err)[:160]}, {}
        busy = self.reserve(rid)
        if busy:
            return busy
        try:
            rec = self.state.recs.get(rid)
            if rec and rec.get("pending"):
                rec = self.check_pending(rid, rec)
                if rec in (None, "failed"):
                    if rec is None:
                        return 503, {"ok": False, "error": "settlement_pending", "retryable": True,
                                     "tx": self.state.recs[rid]["pending"]["tx"]}, {"Retry-After": "5"}
                    return 402, {"ok": False, "error": "settlement_failed"}, {}
            if rec and rec.get("settled"):                   # replay of a settled payment: never settles twice
                log["tx"] = rec["tx"]
                return self.deliver(path, eid, rid, payer, arg, rec, mandate, hx, log)
            return self._settle_new(path, eid, rid, nonce, acc, au, sig, arg, payer, mandate, hx, log)
        finally:
            self.release(rid)

    def _settle_new(self, path, eid, rid, nonce, acc, au, sig, arg, payer, mandate, hx, log):
        now = int(time.time())
        if not (au["validAfter"] <= now and now + MIN_VALIDITY <= au["validBefore"] <= now + 3600):
            return 402, {"ok": False, "error": "bad_validity_window",
                         "message": f"need validAfter <= now and now+{MIN_VALIDITY} <= validBefore <= now+3600"}, {}
        until = self.state.banned_until(payer)
        if until:
            return 403, {"ok": False, "error": "payer_suspended",
                         "message": "a settlement from this payer reverted on-chain; try again later"}, {"Retry-After": str(int(until - now) + 1)}
        if mandate is not None and mandate["deadline"] < now + HSP_MIN_DEADLINE:
            return 400, {"ok": False, "error": "hsp_mandate_invalid",
                         "message": f"mandate deadline must be at least {HSP_MIN_DEADLINE} s away"}, {}
        try:
            e = self.endpoint(eid)
            if not e["active"]:
                return 503, {"ok": False, "error": "endpoint_inactive"}, {"Retry-After": "60"}
            if au["value"] != e["price"] or int(acc["amount"]) != e["price"]:
                return 402, {"ok": False, "error": "price_changed", "message": f"price is now {e['price']}; get a new quote"}, {}
            if mandate is not None and not self.mandate_terms_ok(mandate, e["payee"], e["price"]):
                return 400, {"ok": False, "error": "hsp_mandate_invalid", "message": "mandate terms differ from this payment"}, {}
            wait = self.payer_rl.hit(payer)          # counts settlement attempts that passed every local check
            if wait:
                return 429, {"ok": False, "error": "rate_limited", "message": "too many payments from this payer"}, {"Retry-After": str(wait)}
            missing = self.resource_missing(path, arg)
            if missing:
                return 404, {"ok": False, "error": "not_found", "message": missing + "; nothing was charged"}, {}
            # 2. the payer, on-chain (reads only): a plain account that holds the amount and has not used this nonce
            code = C.code_at(self.rpc, payer)
            if code not in ("0x", "0x0", "", None):
                return 402, {"ok": False, "error": "payer_not_eoa",
                             "message": "exact payments are accepted from plain accounts (EOA) only; use hsk-paywall-tx"}, {}
            if C.erc20_balance(self.rpc, P.USDCE, payer) < au["value"]:
                return 402, {"ok": False, "error": "insufficient_funds", "message": "the payer does not hold the price in USDC.e"}, {}
            if C.authorization_state(self.rpc, P.USDCE, payer, nonce):
                rec = self.reconcile(rid, payer, eid, au["value"])      # e.g. submitted to the contract directly
                if isinstance(rec, dict):
                    return self.deliver(path, eid, rid, payer, arg, rec, mandate, hx, log)
                return 402, {"ok": False, "error": "authorization_used", "message": "this authorization was already used or canceled"}, {}
        except C.RpcError as err:
            log["err"] = str(err)[:120]
            return 502, {"ok": False, "error": "chain_unavailable"}, {"Retry-After": "2"}
        wait = self.settle_rl.peek("all")
        if wait:
            return 503, {"ok": False, "error": "busy", "message": "too many settlements right now", "retryable": True}, {"Retry-After": str(wait)}
        # 3. simulate with the real gas + fee fields, then send (the global limiter counts only what is sent)
        data = C.selector("payWithAuthorization(uint256,bytes32,address,uint256,uint256,uint256,bytes)") + encode(
            ["uint256", "bytes32", "address", "uint256", "uint256", "uint256", "bytes"],
            [eid, bytes.fromhex(rid[2:]), payer, au["value"], au["validAfter"], au["validBefore"], bytes.fromhex(sig[2:])])
        try:
            rc = self.sender.send(self.contract, data, kind="settle", gate=lambda: self.settle_rl.hit("all") == 0,
                                  max_cost_wei=self.max_cost_wei(e["price"]), wait_s=self.cfg["tx_wait_s"])
        except C.PendingTx as pt:
            self.state.record("pending", rid=rid, payer=payer, endpointId=eid, amount=au["value"], tx=pt.tx, until=au["validBefore"])
            log["tx"] = pt.tx
            return 503, {"ok": False, "error": "settlement_pending", "retryable": True, "tx": pt.tx,
                         "message": "settlement sent; retry the same request to get the data"}, {"Retry-After": "5"}
        except C.RpcError as err:
            reason = C.revert_reason(err)
            log["err"] = reason
            if reason in ("AlreadyPaid", C.AUTH_USED):
                try:
                    rec = self.reconcile(rid, payer, eid, au["value"])
                except C.RpcError:
                    rec = None
                if isinstance(rec, dict):
                    return self.deliver(path, eid, rid, payer, arg, rec, mandate, hx, log)
            transient = any(w in reason for w in ("RPC unavailable", "budget", "busy", "gas price", "gas cost"))
            if transient:
                return 503, {"ok": False, "error": "settlement_unavailable", "message": reason, "retryable": True}, {"Retry-After": "10"}
            return 402, {"ok": False, "error": "settlement_refused", "message": reason}, {}
        finally:
            self.check_balance()
        log["tx"] = rc.get("transactionHash")
        if rc.get("status") != "0x1":
            self.state.record("ban", payer=payer, until=now + BAN_S, why="settlement reverted on-chain")
            try:
                rec = self.reconcile(rid, payer, eid, au["value"])
            except C.RpcError:
                rec = None
            if isinstance(rec, dict):
                return self.deliver(path, eid, rid, payer, arg, rec, mandate, hx, log)
            return 402, {"ok": False, "error": "settlement_failed", "tx": rc.get("transactionHash")}, {}
        logs = C.paid_logs(rc, self.contract, rid)
        if len(logs) != 1 or logs[0]["payer"] != payer or logs[0]["amount"] != au["value"] or logs[0]["endpointId"] != eid:
            return 502, {"ok": False, "error": "settlement_unexpected", "tx": rc.get("transactionHash")}, {}
        rec = self.record_settled(rid, payer, eid, {**logs[0], "tx": rc["transactionHash"].lower(),
                                                    "block": int(rc["blockNumber"], 16)}, fee=rc.get("_fee_wei", 0))
        return self.deliver(path, eid, rid, payer, arg, rec, mandate, hx, log)

    def record_settled(self, rid, payer, eid, lg, fee=0, paid_at=None, self_paid=False):
        self.state.record("settled", rid=rid, payer=payer, endpointId=eid, amount=lg["amount"], tx=lg["tx"],
                          block=lg["block"], logIndex=lg["logIndex"], payee=lg["payee"],
                          paidAt=int(paid_at or time.time()), feeWei=int(fee), selfPaid=self_paid)
        return self.state.recs[rid]

    def reconcile(self, rid, payer, eid, amount):
        """After an error/timeout: is `rid` paid on-chain by `payer` for `eid` and `amount`? Then record it from the
        contract (getPayment) and its Paid log (eth_getLogs by request id) and return the record; "refunded" if it
        was refunded; None if not paid (or paid differently). Raises RpcError when the chain cannot be read."""
        pay = C.read_payment(self.rpc, self.contract, rid)
        if pay["payer"] == P.ZERO or pay["payer"] != payer or pay["endpointId"] != eid or pay["amount"] != amount:
            return None
        lg = C.find_paid_log(self.rpc, self.contract, rid, pay["paidBlock"])
        if not lg or lg["payer"] != payer or lg["amount"] != amount or lg["endpointId"] != eid:
            return None
        rec = self.state.recs.get(rid)
        if not (rec and rec.get("settled")):
            rec = self.record_settled(rid, payer, eid, lg, paid_at=pay["paidAt"])
        if rec.get("pending"):
            self.state.record("pending_done", rid=rid)
        if pay["refunded"]:
            if not rec.get("refunded"):
                self.state.record("refund", rid=rid, tx=None, feeWei=0)
            return "refunded"
        return rec

    def check_pending(self, rid, rec):
        """A settlement we sent without seeing its receipt: settled record, None (still pending) or "failed"."""
        p = rec["pending"]
        try:
            rc = self.rpc("eth_getTransactionReceipt", [p["tx"]], tries=2)
            got = self.reconcile(rid, rec["payer"], rec["endpointId"], rec["amount"])
        except C.RpcError:
            return None
        if isinstance(got, dict):
            return got
        if got == "refunded":
            return "failed"
        if rc and rc.get("status") != "0x1":
            self.state.record("pending_done", rid=rid)
            self.state.record("ban", payer=rec["payer"], until=time.time() + BAN_S, why="settlement reverted on-chain")
            return "failed"
        if rc is None and time.time() > p["until"] + 60:     # the authorization expired: it can no longer settle
            self.state.record("pending_done", rid=rid)
            return "failed"
        return None

    def verify_tx(self, path, eid, resource, arg, payer, headers, ip, log):
        wait = self.verify_rl.hit(ip)
        if wait:
            return 429, {"ok": False, "error": "rate_limited"}, {"Retry-After": str(wait)}
        token, txh = headers.get("X-Paywall-Token", ""), headers.get("X-Paywall-Tx")
        if payer is None or not isinstance(token, str) or not P.B32_RE.match(token) or (txh is not None and not P.B32_RE.match(txh)):
            return 400, {"ok": False, "error": "bad_payment_headers", "message": "need X-Payer and X-Paywall-Token (X-Paywall-Tx optional)"}, {}
        token = token.lower()
        if not self.token_ok(eid, payer, resource, token):
            return 402, {"ok": False, "error": "unknown_token"}, {}
        _, rid = P.compute_request_id(self.cfg["chain_id"], self.contract, eid, payer, resource, token)
        log["rid"], log["payer"] = rid[:18], payer
        busy = self.reserve(rid)
        if busy:
            return busy
        try:
            rec = self.state.recs.get(rid)
            if not (rec and rec.get("settled")):
                try:
                    pay = C.read_payment(self.rpc, self.contract, rid)          # the contract first, never a client tx
                    if pay["payer"] == P.ZERO:
                        return 402, {"ok": False, "error": "payment_not_found", "retryable": True}, {"Retry-After": "2"}
                    if pay["payer"] != payer or pay["endpointId"] != eid:
                        return 402, {"ok": False, "error": "inconsistent_payment"}, {}
                    if pay["refunded"]:
                        return 410, {"ok": False, "error": "payment_refunded"}, {}
                    if pay["paidAt"] + SERVE_WINDOW < time.time():
                        return 410, {"ok": False, "error": "payment_expired"}, {}
                    lg = C.find_paid_log(self.rpc, self.contract, rid, pay["paidBlock"])
                except C.RpcError as err:
                    log["err"] = str(err)[:120]
                    return 502, {"ok": False, "error": "chain_unavailable"}, {"Retry-After": "2"}
                if not lg:
                    return 402, {"ok": False, "error": "payment_not_visible_yet", "retryable": True}, {"Retry-After": "2"}
                if lg["payer"] != payer or lg["amount"] != pay["amount"] or lg["endpointId"] != eid:
                    return 402, {"ok": False, "error": "inconsistent_payment"}, {}
                rec = self.record_settled(rid, payer, eid, lg, paid_at=pay["paidAt"], self_paid=True)
            log["tx"] = rec["tx"]
            return self.deliver(path, eid, rid, payer, arg, rec, None, None, log)
        finally:
            self.release(rid)

    def deliver(self, path, eid, rid, payer, arg, rec, mandate, hx, log):
        """Called with `rid` reserved by this thread and paid on-chain. Serves, or refunds when nothing can be served."""
        payment = {"requestId": rid, "tx": rec["tx"], "block": rec["block"], "payer": payer, "amount": str(rec["amount"]),
                   "asset": P.USDCE, "contract": self.contract, "endpointId": eid, "network": P.NETWORK,
                   "explorer": "https://hsk.blockscout.com/tx/" + rec["tx"]}
        settle_resp = {"success": True, "transaction": rec["tx"], "network": P.NETWORK, "payer": payer}
        hdr = {"PAYMENT-RESPONSE": P.b64json(settle_resp)}
        hsp_body, mh = None, None
        if mandate is not None:
            mh = HSP.mandate_hash(mandate, self.cfg["chain_id"])
            stored = rec.get("hsp")
            if stored:                                   # one receipt per payment
                if stored["mandateHash"] != mh:
                    return 409, {"ok": False, "error": "hsp_receipt_exists", "payment": payment,
                                 "message": "this payment already has an HSP receipt for another mandate"}, hdr
                hsp_body = {k: v for k, v in stored.items() if k != "mandateHash"}
            else:
                if not self.mandate_terms_ok(mandate, rec["payee"], rec["amount"]):
                    return 400, {"ok": False, "error": "hsp_mandate_invalid", "message": "mandate terms differ from this payment"}, hdr
                r = HSP.receipt_message(mandate_hash_=mh, tx_hash=rec["tx"], log_index=rec["logIndex"], contract=self.contract,
                                        payer=payer, payee=rec["payee"], token=P.USDCE, amount=rec["amount"],
                                        block=rec["block"], caps=sorted(HSP_CAPS), observed_at=int(time.time()))
                rsig = HSP.sign_receipt(self.adapter, r, self.cfg["chain_id"])
                decision = HSP.verify(mandate=mandate, mandate_sig=hx["signature"], receipt=r, receipt_sig=rsig,
                                      chain_id=self.cfg["chain_id"], pinned_adapter=self.adapter.address,
                                      onchain=lambda rr: self._onchain_check(rid, rr))
                log["hsp"] = decision["outcomeClass"]
                hsp_body = {"draft": HSP.DRAFT_ID, "paymentId": decision["paymentId"], "mandate": HSP.mandate_to_json(mandate),
                            "mandateSignature": hx["signature"], "receipt": HSP.receipt_to_json(r), "receiptSignature": rsig,
                            "adapter": self.adapter.address, "decision": decision}
                if decision["outcomeClass"] == "RETRYABLE":
                    return 503, {"ok": False, "error": "hsp_retry", "retryable": True, "payment": payment,
                                 "hsp": hsp_body}, {**hdr, "Retry-After": "3"}
                if not decision["ok"]:                   # POLICY / PERMANENT after settlement: refund, never ship
                    return self.refund(rid, payer, payment, settle_resp, "HSP verifier: " + decision["outcomeClass"], log,
                                       status=409, extra={"error": "hsp_not_accepted", "hsp": hsp_body})
        try:
            if self.smoke and path == "/v1/hsk/address-profile" and arg == self.smoke:
                raise C.RpcError("operator smoke test: simulated upstream failure")
            data = WORK[path](self.rpc, arg)
        except NotFound as nf:
            return self.refund(rid, payer, payment, settle_resp, str(nf), log, status=404)
        except C.RpcError as err:
            log["err"] = str(err)[:120]
            return self.refund(rid, payer, payment, settle_resp, "upstream data unavailable", log, status=502)
        with self.lock:
            if mandate is not None and not rec.get("hsp"):
                self.state.record("hsp_receipt", rid=rid, mandateHash=mh, hsp=hsp_body)
            self.state.record("served", rid=rid)
        body = {"ok": True, "data": data, "payment": payment}
        if hsp_body:
            body["hsp"] = hsp_body
        log["served"] = True
        return 200, body, hdr

    def _onchain_check(self, rid, receipt, tries=3):
        for i in range(tries):        # a load-balanced RPC node can be a block behind the one that gave the receipt
            try:
                pay = C.read_payment(self.rpc, self.contract, rid)
            except C.RpcError:
                return False, True, "chain unavailable"
            if pay["payer"] != P.ZERO:
                break
            if i + 1 < tries:
                time.sleep(1)
        if pay["payer"] == P.ZERO:
            return False, True, "payment not visible yet"
        if pay["refunded"]:
            return False, False, "payment was refunded"
        if pay["payer"] != receipt["payer"].lower() or pay["amount"] != receipt["amount"]:
            return False, False, "getPayment disagrees with the receipt"
        return True, False, ""

    # ---- refunds
    def refund(self, rid, payer, payment, settle_resp, why, log, status=404, extra=None):
        body = {"ok": False, "error": "not_delivered", "message": why, "payment": payment, **(extra or {})}
        rec = self.state.recs.get(rid) or {}
        if rec.get("served", 0) > 0:          # already delivered once for this payment: never refunded
            body["refund"] = {"refunded": False, "note": "this payment was already served; it is not refunded"}
            return 502, body, {"PAYMENT-RESPONSE": P.b64json(settle_resp), "Retry-After": "5"}
        res = self.send_refund(rid, payer)
        log["refund"] = res.get("tx") if res["refunded"] else ("DEFERRED" if res.get("deferred") else "FAILED")
        settle_resp = {**settle_resp, "refund": {"success": res["refunded"], "transaction": res.get("tx")}}
        rtx = res.get("tx")
        body["refund"] = {"refunded": res["refunded"], "tx": rtx, "note": res.get("note"),
                          "explorer": ("https://hsk.blockscout.com/tx/" + rtx) if rtx else None}
        return (status if res["refunded"] else 502), body, {"PAYMENT-RESPONSE": P.b64json(settle_resp)}

    def send_refund(self, rid, payer):
        """Refund `rid` on-chain once (caller holds the reservation). Per-payer daily cap and a separate gas budget;
        anything that cannot be sent now is queued for the reconciler."""
        rec = self.state.recs.get(rid) or {}
        if rec.get("refunded"):
            return {"refunded": True, "tx": rec.get("refund_tx")}
        if rec.get("served", 0) > 0:
            return {"refunded": False, "note": "served; not refundable"}
        today = self.state.refund_days.get((payer, ST.utc_day()), set())
        if len(today) >= self.cfg["refunds_per_payer_day"] and rid not in today:
            if not rec.get("refund_deferred"):
                self.state.record("refund_deferred", rid=rid, why="daily refund limit for this payer")
            return {"refunded": False, "deferred": True, "note": "refund queued (daily per-payer limit); it is sent automatically"}
        data = C.selector("refund(bytes32)") + encode(["bytes32"], [bytes.fromhex(rid[2:])])
        try:
            rc = self.sender.send(self.contract, data, kind="refund", lag_retries=3, wait_s=self.cfg["tx_wait_s"])
        except C.PendingTx as pt:
            self.state.record("refund_sent", rid=rid, payer=payer, day=ST.utc_day())
            self.state.record("refund_failed", rid=rid, why="no receipt yet for " + pt.tx)
            return {"refunded": False, "tx": pt.tx, "note": "refund sent; confirmation pending (checked automatically)"}
        except C.RpcError as err:
            reason = C.revert_reason(err)
            if reason == "AlreadyRefunded":
                self.state.record("refund", rid=rid, tx=None, feeWei=0)
                return {"refunded": True, "tx": None}
            if "budget" in reason or "gas price" in reason:
                if not rec.get("refund_deferred"):
                    self.state.record("refund_deferred", rid=rid, why=reason)
                return {"refunded": False, "deferred": True, "note": "refund queued; it is sent automatically"}
            self.state.record("refund_failed", rid=rid, why=reason)
            return {"refunded": False, "note": "refund will be retried automatically"}
        finally:
            self.check_balance()
        self.state.record("refund_sent", rid=rid, payer=payer, day=ST.utc_day())
        rtx = (rc.get("transactionHash") or "").lower() or None
        if rc.get("status") == "0x1":
            self.state.record("refund", rid=rid, tx=rtx, feeWei=int(rc.get("_fee_wei", 0)))
            return {"refunded": True, "tx": rtx}
        self.state.record("refund_failed", rid=rid, why="refund reverted " + str(rtx))
        return {"refunded": False, "tx": rtx, "note": "refund will be retried automatically"}

    # ---- background: reconciliation + balance alert
    def reconcile_once(self):
        cap = self.cfg["refunds_per_payer_day"]
        for rid, rec in list(self.state.recs.items()):
            needs_refund = ((rec.get("refund_failed") or rec.get("refund_deferred")) and not rec.get("refunded")
                            and rec.get("served", 0) == 0 and rec.get("refund_attempts", 0) < self.cfg["max_refund_attempts"])
            if not (rec.get("pending") or needs_refund):
                continue
            if self.reserve_internal(rid):
                continue
            try:
                if rec.get("pending"):
                    self.check_pending(rid, rec)
                    continue
                pay = C.read_payment(self.rpc, self.contract, rid)
                if pay["refunded"]:
                    self.state.record("refund", rid=rid, tx=rec.get("refund_tx"), feeWei=0)
                    continue
                if pay["payer"] == P.ZERO:
                    continue
                today = self.state.refund_days.get((rec["payer"], ST.utc_day()), set())
                if len(today) >= cap and rid not in today:
                    continue                                 # queued until the payer's next UTC day
                if self.sender.spent.get("refund", 0) >= self.budgets["refund"]:
                    continue                                 # no refund budget left today (the Sender enforces it exactly)
                self.send_refund(rid, rec["payer"])
            except C.RpcError:
                continue
            finally:
                self.release(rid)
        self.check_balance()

    def reserve_internal(self, rid):
        with self.lock:
            if rid in self.inflight:
                return True
            self.inflight.add(rid)
        return False

    def check_balance(self):
        """Alert (journal + log line + state-dir ALERT file) when the provider holds < 2x its daily gas budgets."""
        try:
            bal = self.sender.balance()
        except Exception:
            return
        threshold = 2 * sum(self.budgets.values())
        low = bal < threshold
        self.provider_balance = bal
        if low and not self.low_balance:
            detail = {"balanceHSK": round(bal / 1e18, 6), "thresholdHSK": round(threshold / 1e18, 6)}
            self.state.record("alert", kind="provider_low_balance", detail=detail)
            print(json.dumps({"alert": "provider_low_balance", **detail}), flush=True)
            if self.cfg.get("state_dir"):
                try:
                    fd = os.open(os.path.join(self.cfg["state_dir"], "ALERT"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(json.dumps({"alert": "provider_low_balance", "t": int(time.time()), **detail}) + "\n")
                except OSError:
                    pass
        self.low_balance = low

    def info(self):
        eps = {}
        for path, spec in RESOURCES.items():
            try:
                e = self.endpoint(spec["endpointId"], max_age=10)
                eps[path] = {"endpointId": spec["endpointId"], "price": str(e["price"]), "active": e["active"]}
            except C.RpcError:
                eps[path] = {"endpointId": spec["endpointId"], "price": None, "active": None}
        return eps


def make_handler(seller):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "hsk-meter/" + VERSION
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 10

        def log_message(self, fmt, *args):  # replaced by our JSON log
            pass

        def _send(self, status, body, headers):
            raw = json.dumps(body, default=str).encode()
            self.send_response(status)
            h = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
                 "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                 "Access-Control-Allow-Origin": "*",
                 "Access-Control-Allow-Headers": "X-Payer, PAYMENT-SIGNATURE, X-PAYMENT, X-Paywall-Token, X-Paywall-Tx",
                 "Access-Control-Expose-Headers": "PAYMENT-REQUIRED, PAYMENT-RESPONSE, Retry-After",
                 "Content-Length": str(len(raw)), **headers}
            for k, v in h.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_OPTIONS(self):
            self._send(204, {}, {"Access-Control-Allow-Methods": "GET, OPTIONS"})

        def _not_allowed(self):
            self._send(405, {"ok": False, "error": "method_not_allowed"}, {"Allow": "GET, OPTIONS"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _not_allowed

        def do_GET(self):
            if not self.server.req_sem.acquire(blocking=False):     # bounded concurrent work
                self._send(503, {"ok": False, "error": "busy", "retryable": True}, {"Retry-After": "2"})
                return
            try:
                self._get()
            finally:
                self.server.req_sem.release()

        def _get(self):
            t0 = time.time()
            ip = client_key(self.client_address[0], self.headers, seller.cfg["bind_host"])
            u = urllib.parse.urlsplit(self.path)
            log = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "p": u.path}
            try:
                if len(self.path) > 400:
                    status, body, h = 414, {"ok": False, "error": "uri_too_long"}, {}
                elif u.path in RESOURCES:
                    status, body, h = seller.handle_paid(u.path, u.query, self.headers, ip, log)
                elif u.path == "/health":
                    status, body, h = 200, {"ok": True, "service": "hsk-meter", "version": VERSION,
                                            "network": P.NETWORK, "contract": seller.contract, "endpoints": seller.info(),
                                            "settler": seller.provider.address, "hspAdapter": seller.adapter.address,
                                            "alerts": ["provider_low_balance"] if seller.low_balance else []}, {}
                elif u.path == "/.well-known/x402":
                    status, body, h = 200, {"x402Version": P.X402_VERSION, "network": P.NETWORK, "asset": P.USDCE,
                                            "payTo": seller.contract, "schemes": ["exact", "hsk-paywall-tx"],
                                            "resources": [{"path": p + "?" + s["param"] + "=0x...", "endpointId": s["endpointId"],
                                                           "description": s["desc"]} for p, s in RESOURCES.items()],
                                            "extensions": ["hsp-draft"]}, {}
                elif u.path == "/":
                    status, body, h = 200, {"service": "HSK Meter by Swift Bot Labs: HashKey Chain data, paid per request in USDC.e",
                                            "paid": [p for p in RESOURCES], "free": ["/health", "/.well-known/x402"],
                                            "how": "GET a paid path with X-Payer -> 402 (x402 v2) -> sign the EIP-3009 authorization -> retry with PAYMENT-SIGNATURE"}, {}
                else:
                    status, body, h = 404, {"ok": False, "error": "not_found"}, {}
            except Exception as err:  # never leak internals
                log["err"] = (type(err).__name__ + ": " + str(err))[:160]
                status, body, h = 500, {"ok": False, "error": "internal"}, {}
            self._send(status, body, h)
            log["s"], log["ms"] = status, int((time.time() - t0) * 1000)
            if u.path in RESOURCES:
                print(json.dumps(log), flush=True)   # never tokens or signatures: rid prefix, payer, tx only

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """One thread per connection, at most max_connections at once (extra connections get an immediate 503 and are
    closed); at most max_requests requests are worked on at once (do_GET answers 503 beyond that)."""
    daemon_threads = True
    request_queue_size = 64
    max_connections = 128
    max_requests = 32

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.conn_sem = threading.BoundedSemaphore(self.max_connections)
        self.req_sem = threading.BoundedSemaphore(self.max_requests)

    def process_request(self, request, client_address):
        if not self.conn_sem.acquire(blocking=False):
            body = b'{"ok":false,"error":"busy"}'
            try:
                request.settimeout(2)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nRetry-After: 2\r\n"
                                b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            t = threading.Thread(target=self._conn, args=(request, client_address), daemon=True)
            t.start()
        except RuntimeError:
            self.conn_sem.release()
            self.shutdown_request(request)

    def handle_error(self, request, client_address):   # one JSON line, never a traceback with internals
        print(json.dumps({"conn_error": type(sys.exc_info()[1]).__name__}), flush=True)

    def _conn(self, request, client_address):
        try:
            self.process_request_thread(request, client_address)
        finally:
            self.conn_sem.release()


def reconciler(seller, every_s=60):
    while True:
        time.sleep(every_s)
        try:
            seller.reconcile_once()
        except Exception as err:   # keep running; log the type only
            print(json.dumps({"reconciler_error": type(err).__name__}), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8402)
    ap.add_argument("--contract", default=None)
    ap.add_argument("--network", choices=["mainnet", "local"], default="mainnet")
    ap.add_argument("--rpc", default="https://mainnet.hsk.xyz")
    ap.add_argument("--chain-id", type=int, default=P.CHAIN_ID)
    ap.add_argument("--key-dir", default=os.environ.get("HSK_METER_KEY_DIR"),
                    help="dir with provider.env, adapter.env, token.env (one value each, 0600); or $HSK_METER_KEY_DIR")
    ap.add_argument("--state-dir", default=os.environ.get("HSK_METER_STATE_DIR") or os.path.join(ROOT, "run"))
    ap.add_argument("--settle-budget-hsk", type=float, default=2.0)
    ap.add_argument("--refund-budget-hsk", type=float, default=0.5)
    ap.add_argument("--hsk-usd", type=float, default=0.10, help="HSK price ceiling used for the per-sale gas guard")
    ap.add_argument("--no-egress-guard", action="store_true", help="local development only")
    ap.add_argument("--smoke-refund-address", default=None,
                    help="operator smoke test (loopback only): address-profile requests for this address are refunded")
    a = ap.parse_args()
    if not a.key_dir:
        sys.exit("--key-dir (or HSK_METER_KEY_DIR) is required")
    contract = a.contract or json.load(open(os.path.join(ROOT, "ops", "state", "deployment.json")))["address"]
    if not P.ADDR_RE.match(contract):
        sys.exit("bad contract address")
    if a.network == "mainnet" and (a.chain_id != P.CHAIN_ID or a.rpc != "https://mainnet.hsk.xyz"):
        sys.exit("mainnet uses chain 177 and the official RPC")
    if a.smoke_refund_address and (a.host not in LOOPBACK or not P.ADDR_RE.match(a.smoke_refund_address)):
        sys.exit("--smoke-refund-address needs an address and a loopback --host")
    if a.network == "mainnet" and a.no_egress_guard:
        sys.exit("the egress guard cannot be disabled on mainnet")
    if not a.no_egress_guard:
        import egress
        u = urllib.parse.urlparse(a.rpc)
        egress.install([(u.hostname, u.port or (443 if u.scheme == "https" else 80))])
    os.makedirs(a.state_dir, mode=0o700, exist_ok=True)
    cfg = {"network": a.network, "rpc": a.rpc, "chain_id": a.chain_id, "contract": contract, "key_dir": a.key_dir,
           "state_dir": a.state_dir, "settle_budget_wei": int(a.settle_budget_hsk * 1e18),
           "refund_budget_wei": int(a.refund_budget_hsk * 1e18), "hsk_usd": a.hsk_usd, "bind_host": a.host,
           "smoke_refund_address": a.smoke_refund_address}
    try:
        seller = Seller(cfg)
    except KeyFileError as e:
        sys.exit(str(e))
    chain_id = int(seller.rpc("eth_chainId", [], tries=3), 16)
    if chain_id != a.chain_id:
        sys.exit(f"RPC chain id {chain_id} != {a.chain_id}")
    if len(seller.rpc("eth_getCode", [contract, "latest"], tries=3)) <= 2:
        sys.exit("no contract code at the configured address")
    if C.eth_call(seller.rpc, contract, "token()", [], [], ["address"])[0].lower() != P.USDCE.lower():
        sys.exit("contract does not settle in USDC.e")
    base, tip, _ = seller.sender.fees()
    worst_units = SETTLE_GAS_WORST * (base + tip) / 1e18 * a.hsk_usd * 1e6
    for path, spec in RESOURCES.items():
        e = seller.endpoint(spec["endpointId"])
        if e["provider"] != seller.provider.address.lower():
            sys.exit(f"endpoint {spec['endpointId']} is not provided by this server's provider key")
        if e["price"] < 2 * worst_units:
            print(json.dumps({"warning": "price below 2x worst-case settlement gas", "endpointId": spec["endpointId"],
                              "price": e["price"], "worstGasUnits": int(worst_units)}), flush=True)
    seller.check_balance()
    srv = Server((a.host, a.port), make_handler(seller))
    threading.Thread(target=reconciler, args=(seller,), daemon=True).start()
    print(json.dumps({"listening": f"http://{a.host}:{a.port}", "contract": contract, "network": P.NETWORK,
                      "settler": seller.provider.address, "hspAdapter": seller.adapter.address,
                      "endpoints": seller.info(), "egressGuard": not a.no_egress_guard,
                      "smokeRefund": bool(a.smoke_refund_address),
                      "journalRecords": len(seller.state.recs)}), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
