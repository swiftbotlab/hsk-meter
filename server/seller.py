#!/usr/bin/env python3
"""HSK Meter seller: HashKey Chain data APIs sold per request behind an x402 v2 paywall, settled in USDC.e through the
HskPayPerRequest contract (Swift Bot Labs).

  seller.py [--port 8402] [--host 127.0.0.1] [--contract 0x...] [--network mainnet|local --rpc URL --chain-id N]

Paid resources (one on-chain endpoint each; price read from the contract):
  GET /v1/hsk/address-profile?address=0x...   balances (HSK, USDC.e, USDT, WHSK), nonce, contract or EOA
  GET /v1/hsk/tx-receipt?hash=0x...           status, fee, ERC-20 transfers of a HashKey Chain transaction;
                                              a hash that does not exist is refunded on-chain automatically
Free: GET /  GET /health  GET /.well-known/x402

Flow (x402 v2): 1. GET with X-Payer: <address>  ->  402, PAYMENT-REQUIRED (base64 JSON, mirrored in the body)
  2a. scheme "exact": sign the EIP-3009 ReceiveWithAuthorization described in accepts[0] (to = contract, value = price,
      nonce = extra.nonce) and retry with PAYMENT-SIGNATURE: base64(PaymentPayload). The server submits
      payWithAuthorization (it pays the gas), checks the Paid event, does the work, answers 200 + PAYMENT-RESPONSE.
  2b. scheme "hsk-paywall-tx": call pay(endpointId, requestId, amount) yourself and retry with X-Payer,
      X-Paywall-Token and X-Paywall-Tx.
  Optional: sign the HSP-draft mandate offered in extensions["hsp-draft"] and send it in the payload's extensions; the
  answer then carries an adapter-signed receipt and the verifier's decision (served only on ACCEPT).
Keys are read at start from files under secrets/ and never logged. The provider key is hot (it submits settlements and
refunds); the payee key is never on this server, so a server compromise cannot withdraw earned credit.
"""
import argparse
import hashlib
import hmac
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chain as C        # noqa: E402
import hsp_draft as HSP  # noqa: E402
import protocol as P     # noqa: E402
from eth_abi import decode, encode  # noqa: E402
from eth_account import Account  # noqa: E402

VERSION = "0.1.0"
ROOT = os.path.dirname(HERE)
SECRETS = "secrets"
USDT = "0xf1b50ed67a9e2cc94ad3c477779e2d4cbfff9029"
WHSK = "0xB210D2120d57b758EE163cFfb43e73728c471Cf1"
TRANSFER_TOPIC = "0x" + __import__("eth_utils").keccak(text="Transfer(address,address,uint256)").hex()
KNOWN_TOKENS = {P.USDCE.lower(): ("USDC.e", 6), USDT.lower(): ("USDT", 6), WHSK.lower(): ("WHSK", 18)}
RESOURCES = {
    "/v1/hsk/address-profile": {"endpointId": 1, "param": "address", "re": P.ADDR_RE,
                                "desc": "HashKey Chain address profile: HSK, USDC.e, USDT, WHSK balances, nonce, contract or EOA."},
    "/v1/hsk/tx-receipt": {"endpointId": 2, "param": "hash", "re": P.B32_RE,
                           "desc": "HashKey Chain transaction check: status, fee, ERC-20 transfers. Unknown hashes are refunded."},
}
MAX_SERVES = 3
SERVE_WINDOW = 3600
MAX_TIMEOUT = 300


class NotFound(Exception):
    pass


def _read_env(path):
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _ensure_secret_file(path, var, nbytes=32):
    """Create a random key file (0600, O_EXCL) once; return the value."""
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"{var}={secrets.token_hex(nbytes)}\n")
    return _read_env(path)[var]


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
    def __init__(self, cfg):
        self.cfg = cfg
        self.rpc = C.Rpc(cfg["rpc"], allow_local=cfg["network"] == "local")
        keys = _read_env(cfg["provider_key_file"])
        self.provider = Account.from_key(keys["HKPW_PROVIDER_PK"])
        del keys
        self.adapter = Account.from_key("0x" + _ensure_secret_file(f"{SECRETS}/hashkey_paywall_adapter.env", "HKPW_ADAPTER_PK"))
        self.token_key = bytes.fromhex(_ensure_secret_file(f"{SECRETS}/hashkey_paywall_server.env", "PAYWALL_TOKEN_KEY"))
        self.sender = C.Sender(self.rpc, self.provider, cfg["chain_id"])
        self.contract = cfg["contract"].lower()
        self.quote_rl, self.verify_rl = RateLimiter(60, 60), RateLimiter(30, 60)
        self.settle_rl = RateLimiter(cfg.get("settle_per_min", 12), 60, max_keys=1)
        self.served, self.settled = {}, {}
        self.lock = threading.Lock()
        self.cache = {}
        self.journal = os.path.join(ROOT, "ops", "state", "seller_settlements.jsonl")

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

    def _journal(self, entry):
        try:
            fd = os.open(self.journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}) + "\n")
        except OSError:
            pass

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
        except C.RpcError as err:
            log["err"] = str(err)[:120]
            return 502, {"ok": False, "error": "chain_unavailable"}, {"Retry-After": "2"}
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
                   "mandate": mandate, "optional": True,
                   "how": "sign the mandate (EIP-712) and send {mandate, signature} in PaymentPayload.extensions['hsp-draft']"}
        body = P.payment_required(url=path + "?" + resource.split("?", 1)[1], description=RESOURCES[path]["desc"],
                                  resource=resource, contract=self.contract, endpoint_id=eid, price=e["price"],
                                  payer=payer, token=token, request_id=rid, nonce=nonce, max_timeout=MAX_TIMEOUT,
                                  explorer="https://hsk.blockscout.com", hsp_ext=hsp_ext)
        return 402, body, {"PAYMENT-REQUIRED": P.b64json(body)}

    def _serve_count_ok(self, rid):
        with self.lock:
            return self.served.get(rid, 0) < MAX_SERVES

    def _count_serve(self, rid):
        with self.lock:
            self.served[rid] = self.served.get(rid, 0) + 1
            if len(self.served) > 20000:
                self.served.pop(next(iter(self.served)))

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
            (str(acc.get("payTo", "")).lower() == self.contract and au["to"] == self.contract, "payTo/to must be the settlement contract"),
            (str(acc.get("asset", "")).lower() == P.USDCE.lower(), "asset must be USDC.e"),
            (self.token_ok(eid, payer, resource, x["token"].lower()), "token was not issued by this server for this payer and resource"),
        ]
        for ok, why in checks:
            if not ok:
                return 402, {"ok": False, "error": "payment_mismatch", "message": why}, {}
        _, rid = P.compute_request_id(self.cfg["chain_id"], self.contract, eid, payer, resource, x["token"].lower())
        log["rid"] = rid[:18]
        nonce = P.authorization_nonce(self.contract, eid, rid)
        now = int(time.time())
        if rid != x["requestId"].lower() or au["nonce"] != nonce:
            return 402, {"ok": False, "error": "payment_mismatch", "message": "requestId/nonce do not match this quote"}, {}
        if not (au["validAfter"] <= now and now + 5 < au["validBefore"] <= now + 3600):
            return 402, {"ok": False, "error": "bad_validity_window", "message": "need validAfter <= now < now+5 < validBefore <= now+3600"}, {}
        if not self._serve_count_ok(rid):
            return 429, {"ok": False, "error": "already_served", "message": f"served {MAX_SERVES} times already"}, {}
        try:
            e = self.endpoint(eid)
        except C.RpcError as err:
            log["err"] = str(err)[:120]
            return 502, {"ok": False, "error": "chain_unavailable"}, {"Retry-After": "2"}
        if au["value"] != e["price"] or int(acc.get("amount", "-1") if str(acc.get("amount", "")).isdigit() else -1) != e["price"]:
            return 402, {"ok": False, "error": "price_changed", "message": f"price is now {e['price']}; get a new quote"}, {}
        mandate = None
        hx = ext.get("hsp-draft") if isinstance(ext, dict) else None
        if hx is not None:
            try:
                mandate, msig = HSP.mandate_from_json(hx.get("mandate")), str(hx.get("signature", ""))
                if not P.SIG_RE.match(msig):
                    raise ValueError("bad mandate signature encoding")
                want = HSP.mandate_message(payer=payer, payee=e["payee"], token=P.USDCE, amount=e["price"], reference=rid,
                                           required_caps=HSP.caps_list(mandate["requiredCapabilities"]), deadline=mandate["deadline"])
                if mandate != want or mandate["deadline"] < now:
                    raise ValueError("mandate terms differ from this payment")
                if HSP.recover_mandate(mandate, msig, self.cfg["chain_id"]).lower() != payer:
                    raise ValueError("mandate not signed by the payer")
            except (ValueError, KeyError, TypeError) as err:
                return 400, {"ok": False, "error": "hsp_mandate_invalid", "message": str(err)[:160]}, {}
        # settle (or recognise an earlier settlement of the same request)
        with self.lock:
            prior = self.settled.get(rid)
        if prior is None:
            if self.settle_rl.hit("all"):
                return 503, {"ok": False, "error": "busy", "message": "too many settlements right now", "retryable": True}, {"Retry-After": "5"}
            data = C.selector("payWithAuthorization(uint256,bytes32,address,uint256,uint256,uint256,bytes)") + encode(
                ["uint256", "bytes32", "address", "uint256", "uint256", "uint256", "bytes"],
                [eid, bytes.fromhex(rid[2:]), payer, au["value"], au["validAfter"], au["validBefore"], bytes.fromhex(sig[2:])])
            try:
                rc = self.sender.send(self.contract, data)
            except C.RpcError as err:
                reason = C.revert_reason(err)
                log["err"] = reason
                status = 402 if "RPC unavailable" not in reason and "budget" not in reason else 503
                return status, {"ok": False, "error": "settlement_refused", "message": reason}, {}
            if rc.get("status") != "0x1":
                return 402, {"ok": False, "error": "settlement_failed", "tx": rc.get("transactionHash")}, {}
            logs = C.paid_logs(rc, self.contract, rid)
            if len(logs) != 1 or logs[0]["payer"] != payer or logs[0]["amount"] != au["value"] or logs[0]["endpointId"] != eid:
                return 502, {"ok": False, "error": "settlement_unexpected", "tx": rc.get("transactionHash")}, {}
            prior = {"tx": rc["transactionHash"].lower(), "block": int(rc["blockNumber"], 16), "logIndex": logs[0]["logIndex"],
                     "payee": logs[0]["payee"], "amount": logs[0]["amount"], "feeWei": rc.get("_fee_wei", 0)}
            with self.lock:
                self.settled[rid] = prior
            self._journal({"event": "settled", "rid": rid, "payer": payer, "endpointId": eid, "tx": prior["tx"],
                           "amount": prior["amount"], "feeWei": prior["feeWei"]})
        log["tx"] = prior["tx"]
        return self.deliver(path, eid, rid, payer, arg, prior, mandate, hx, log)

    def verify_tx(self, path, eid, resource, arg, payer, headers, ip, log):
        wait = self.verify_rl.hit(ip)
        if wait:
            return 429, {"ok": False, "error": "rate_limited"}, {"Retry-After": str(wait)}
        token, txh = headers.get("X-Paywall-Token", ""), headers.get("X-Paywall-Tx", "")
        if payer is None or not P.B32_RE.match(token) or not P.B32_RE.match(txh):
            return 400, {"ok": False, "error": "bad_payment_headers", "message": "need X-Payer, X-Paywall-Token, X-Paywall-Tx"}, {}
        token, txh = token.lower(), txh.lower()
        if not self.token_ok(eid, payer, resource, token):
            return 402, {"ok": False, "error": "unknown_token"}, {}
        _, rid = P.compute_request_id(self.cfg["chain_id"], self.contract, eid, payer, resource, token)
        log["rid"], log["tx"], log["payer"] = rid[:18], txh, payer
        if not self._serve_count_ok(rid):
            return 429, {"ok": False, "error": "already_served"}, {}
        try:
            rc = self.rpc("eth_getTransactionReceipt", [txh], tries=2)
            if not rc:
                return 402, {"ok": False, "error": "payment_not_found", "retryable": True}, {"Retry-After": "2"}
            if rc.get("status") != "0x1":
                return 402, {"ok": False, "error": "payment_failed"}, {}
            logs = C.paid_logs(rc, self.contract, rid)
            if len(logs) != 1 or logs[0]["endpointId"] != eid or logs[0]["payer"] != payer:
                return 402, {"ok": False, "error": "no_matching_payment"}, {}
            pay = C.read_payment(self.rpc, self.contract, rid)
        except C.RpcError as err:
            log["err"] = str(err)[:120]
            return 502, {"ok": False, "error": "chain_unavailable"}, {"Retry-After": "2"}
        if pay["payer"] == P.ZERO:
            return 402, {"ok": False, "error": "payment_not_visible_yet", "retryable": True}, {"Retry-After": "2"}
        if pay["payer"] != payer or pay["amount"] != logs[0]["amount"] or pay["refunded"]:
            return 402, {"ok": False, "error": "payment_refunded" if pay["refunded"] else "inconsistent_payment"}, {}
        if pay["paidAt"] + SERVE_WINDOW < time.time():
            return 410, {"ok": False, "error": "payment_expired"}, {}
        prior = {"tx": txh, "block": int(rc["blockNumber"], 16), "logIndex": logs[0]["logIndex"], "payee": logs[0]["payee"],
                 "amount": logs[0]["amount"], "feeWei": 0}
        return self.deliver(path, eid, rid, payer, arg, prior, None, None, log)

    def deliver(self, path, eid, rid, payer, arg, s, mandate, hx, log):
        payment = {"requestId": rid, "tx": s["tx"], "block": s["block"], "payer": payer, "amount": str(s["amount"]),
                   "asset": P.USDCE, "contract": self.contract, "endpointId": eid, "network": P.NETWORK,
                   "explorer": "https://hsk.blockscout.com/tx/" + s["tx"]}
        settle_resp = {"success": True, "transaction": s["tx"], "network": P.NETWORK, "payer": payer}
        try:
            data = WORK[path](self.rpc, arg)
        except NotFound as nf:
            return self.refund(rid, payer, payment, settle_resp, str(nf), log)
        except C.RpcError as err:
            log["err"] = str(err)[:120]
            return self.refund(rid, payer, payment, settle_resp, "upstream data unavailable", log)
        body = {"ok": True, "data": data, "payment": payment}
        if mandate is not None:
            caps = [HSP.CAP_SETTLED, HSP.CAP_REFUNDABLE]
            rec = HSP.receipt_message(mandate_hash_=HSP.mandate_hash(mandate, self.cfg["chain_id"]), tx_hash=s["tx"],
                                      log_index=s["logIndex"], contract=self.contract, payer=payer, payee=s["payee"],
                                      token=P.USDCE, amount=s["amount"], block=s["block"], caps=caps,
                                      observed_at=int(time.time()))
            rsig = HSP.sign_receipt(self.adapter, rec, self.cfg["chain_id"])
            decision = HSP.verify(mandate=mandate, mandate_sig=hx["signature"], receipt=rec, receipt_sig=rsig,
                                  chain_id=self.cfg["chain_id"], pinned_adapter=self.adapter.address,
                                  onchain=lambda r: self._onchain_check(rid, r))
            log["hsp"] = decision["outcomeClass"]
            body["hsp"] = {"draft": HSP.DRAFT_ID, "paymentId": decision["paymentId"], "mandate": HSP.mandate_to_json(mandate),
                           "mandateSignature": hx["signature"], "receipt": HSP.receipt_to_json(rec), "receiptSignature": rsig,
                           "adapter": self.adapter.address, "decision": decision}
            if not decision["ok"]:  # ACCEPT means ship; anything else is not shipped
                body = {"ok": False, "error": "hsp_not_accepted", "payment": payment, "hsp": body["hsp"]}
                return 409, body, {"PAYMENT-RESPONSE": P.b64json(settle_resp)}
        self._count_serve(rid)
        log["served"] = True
        return 200, body, {"PAYMENT-RESPONSE": P.b64json(settle_resp)}

    def _onchain_check(self, rid, receipt):
        try:
            pay = C.read_payment(self.rpc, self.contract, rid)
        except C.RpcError:
            return False, True, "chain unavailable"
        if pay["payer"] == P.ZERO:
            return False, True, "payment not visible yet"
        if pay["refunded"]:
            return False, False, "payment was refunded"
        if pay["payer"] != receipt["payer"].lower() or pay["amount"] != receipt["amount"]:
            return False, False, "getPayment disagrees with the receipt"
        return True, False, ""

    def refund(self, rid, payer, payment, settle_resp, why, log):
        data = C.selector("refund(bytes32)") + encode(["bytes32"], [bytes.fromhex(rid[2:])])
        try:
            rc = self.sender.send(self.contract, data, lag_retries=3)
            ok = rc.get("status") == "0x1"
        except C.RpcError as err:
            rc, ok = {}, False
            log["refund_err"] = C.revert_reason(err)
        rtx = (rc.get("transactionHash") or "").lower() or None
        self._journal({"event": "refund" if ok else "refund_failed", "rid": rid, "payer": payer, "tx": rtx, "why": why})
        log["refund"] = rtx if ok else "FAILED"
        settle_resp = {**settle_resp, "refund": {"success": ok, "transaction": rtx}}
        body = {"ok": False, "error": "not_delivered", "message": why, "payment": payment,
                "refund": {"refunded": ok, "tx": rtx, "explorer": ("https://hsk.blockscout.com/tx/" + rtx) if rtx else None,
                           "note": None if ok else "refund will be retried by the operator"}}
        return (404 if ok else 500), body, {"PAYMENT-RESPONSE": P.b64json(settle_resp)}

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
        timeout = 30

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
            t0 = time.time()
            ip = self.client_address[0]
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
                                            "settler": seller.provider.address, "hspAdapter": seller.adapter.address}, {}
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
    daemon_threads = True
    request_queue_size = 64


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8402)
    ap.add_argument("--contract", default=None)
    ap.add_argument("--network", choices=["mainnet", "local"], default="mainnet")
    ap.add_argument("--rpc", default="https://mainnet.hsk.xyz")
    ap.add_argument("--chain-id", type=int, default=P.CHAIN_ID)
    ap.add_argument("--provider-key-file", default=f"{SECRETS}/hashkey_paywall_keys.env")
    a = ap.parse_args()
    contract = a.contract or json.load(open(os.path.join(ROOT, "ops", "state", "deployment.json")))["address"]
    if not P.ADDR_RE.match(contract):
        sys.exit("bad contract address")
    if a.network == "mainnet" and (a.chain_id != P.CHAIN_ID or a.rpc != "https://mainnet.hsk.xyz"):
        sys.exit("mainnet uses chain 177 and the official RPC")
    cfg = {"network": a.network, "rpc": a.rpc, "chain_id": a.chain_id, "contract": contract,
           "provider_key_file": a.provider_key_file}
    seller = Seller(cfg)
    chain_id = int(seller.rpc("eth_chainId", [], tries=3), 16)
    if chain_id != a.chain_id:
        sys.exit(f"RPC chain id {chain_id} != {a.chain_id}")
    if len(seller.rpc("eth_getCode", [contract, "latest"], tries=3)) <= 2:
        sys.exit("no contract code at the configured address")
    if C.eth_call(seller.rpc, contract, "token()", [], [], ["address"])[0].lower() != P.USDCE.lower():
        sys.exit("contract does not settle in USDC.e")
    for path, spec in RESOURCES.items():
        e = seller.endpoint(spec["endpointId"])
        if e["provider"] != seller.provider.address.lower():
            sys.exit(f"endpoint {spec['endpointId']} is not provided by this server's provider key")
    srv = Server((a.host, a.port), make_handler(seller))
    print(json.dumps({"listening": f"http://{a.host}:{a.port}", "contract": contract, "network": P.NETWORK,
                      "settler": seller.provider.address, "hspAdapter": seller.adapter.address,
                      "endpoints": seller.info()}), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
