#!/usr/bin/env python3
"""Paying client for HSK Meter (Swift Bot Labs): fetches a paid URL, answers the x402 v2 402 with a signed EIP-3009
authorization (or pays the contract itself), and verifies everything it is told against HashKey Chain.

  buyer.py URL --key-file F --expect-provider 0x... --expect-payee 0x... [--scheme exact|tx] [--hsp]
           [--max-price 0.05] [--session-cap 0.20] [--contract 0x...] [--pin-adapter 0x...] [--session-file F]
  (--key-file: a file with HKPW_BUYER_PK=0x...; defaults: $HSK_METER_BUYER_KEY_FILE, $HSK_METER_PROVIDER,
   $HSK_METER_PAYEE)

Spend guard (checked BEFORE signing anything): network eip155:177 only; asset USDC.e only; payTo must be the
expected contract (--contract, default ops/state/deployment.json) whose token() is USDC.e; the endpoint must belong to
the pinned provider and pay the pinned payee (anyone can register endpoints on the contract); the quoted amount must
equal the contract's on-chain price for that endpoint and be <= --max-price; the session ledger (spent + reserved)
must stay <= --session-cap, and the amount is RESERVED in the ledger before the authorization is signed or the pay
transaction sent; the requestId and nonce are recomputed locally from the resource we asked for; the authorization is
valid for 120 s only. A reservation becomes spent when the payment is verified, and is released only when the chain
proves nothing can be charged any more (getPayment unpaid and, after validBefore, authorizationState == false).
After paying: the Paid event in the settlement tx (payer = us, amount, request), getPayment(requestId), and for
refunds the Refunded event and our balance. Redirects are refused.
"""
import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
import chain as C        # noqa: E402
import hsp_draft as HSP  # noqa: E402
import protocol as P     # noqa: E402
from eth_abi import encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402

STATE = os.path.join(ROOT, "ops", "state")
SESSION = os.path.join(STATE, "buyer_session.json")
JOURNAL = os.path.join(STATE, "buyer_runs.jsonl")
AUTH_VALID_S = 120
TX_RESERVE_S = 600
REFUNDED_TOPIC = C.REFUNDED_TOPIC


class Refused(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise Refused("server tried to redirect; refused")


OPENER = urllib.request.build_opener(_NoRedirect)


def http_get(url, headers, timeout=120):
    req = urllib.request.Request(url, headers={"user-agent": "hsk-meter-buyer/0.1", **headers})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), json.loads(r.read(200_001)[:200_000] or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read(200_001)[:200_000]
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"raw": raw[:300].decode("utf-8", "replace")}
        return e.code, dict(e.headers), body


def load_key(path):
    """Only HKPW_BUYER_PK is taken from the file."""
    with open(path) as f:
        for line in f:
            if line.startswith("HKPW_BUYER_PK="):
                return Account.from_key(line.strip().split("=", 1)[1])
    raise Refused("key file has no HKPW_BUYER_PK")


def journal(entry):
    os.makedirs(STATE, exist_ok=True)
    fd = os.open(JOURNAL, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}) + "\n")


class Session:
    """The spend-cap ledger: {"spent_units", "reserved": {rid: {amount, scheme, nonce, expires}}, "paid": {rid: amount}}
    in one JSON file, read-modify-written under an exclusive flock (concurrent buyer runs share the cap)."""

    def __init__(self, path):
        self.path = path

    @contextlib.contextmanager
    def _ledger(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                d = json.loads(f.read() or "{}")
            except ValueError:
                raise Refused("session ledger is corrupt; refusing to spend")
            d.setdefault("spent_units", 0)
            d.setdefault("reserved", {})
            d.setdefault("paid", {})
            yield d
            f.seek(0)
            f.truncate()
            f.write(json.dumps(d, sort_keys=True))

    def total(self):
        with self._ledger() as d:
            return d["spent_units"] + sum(r["amount"] for r in d["reserved"].values())

    def reserve(self, rid, amount, cap, scheme, nonce, expires):
        with self._ledger() as d:
            if rid in d["reserved"] or rid in d["paid"]:
                raise Refused("this request id is already reserved or paid")
            used = d["spent_units"] + sum(r["amount"] for r in d["reserved"].values())
            if used + amount > cap:
                raise Refused(f"session spend cap reached ({used} spent or reserved + {amount} > {cap})")
            d["reserved"][rid] = {"amount": amount, "scheme": scheme, "nonce": nonce, "expires": int(expires)}

    def commit(self, rid):
        """The payment is verified on-chain: reserved -> spent."""
        with self._ledger() as d:
            r = d["reserved"].pop(rid, None)
            if r is not None:
                d["spent_units"] += r["amount"]
                d["paid"][rid] = r["amount"]
                while len(d["paid"]) > 1000:
                    d["paid"].pop(next(iter(d["paid"])))
            return d["spent_units"]

    def refunded(self, rid):
        """The refund is verified on-chain: nothing was spent for rid."""
        with self._ledger() as d:
            if d["reserved"].pop(rid, None) is None and rid in d["paid"]:
                d["spent_units"] -= d["paid"].pop(rid)
            return d["spent_units"]

    def reconcile(self, rpc, contract, me, now=None):
        """Settle expired reservations from the chain. Returns [(rid, outcome)]."""
        now = now or time.time()
        me = me.lower()
        out = []
        with self._ledger() as d:
            for rid, r in list(d["reserved"].items()):
                if now < r["expires"] + 30:
                    continue
                pay = C.read_payment(rpc, contract, rid)
                if pay["payer"] == me:
                    d["reserved"].pop(rid)
                    if not pay["refunded"]:
                        d["spent_units"] += pay["amount"]
                        d["paid"][rid] = pay["amount"]
                    out.append((rid, "refunded" if pay["refunded"] else "paid"))
                    continue
                if r["scheme"] == "exact" and r.get("nonce"):
                    if C.authorization_state(rpc, P.USDCE, me, r["nonce"]):
                        out.append((rid, "kept: authorization used but no payment visible yet"))
                        continue
                d["reserved"].pop(rid)
                out.append((rid, "released"))
        return out


def check_quote(rpc, body, url, me, contract, max_units, cap_units, scheme, session, expect_provider, expect_payee):
    if body.get("x402Version") != 2 or not isinstance(body.get("accepts"), list):
        raise Refused("not an x402 v2 PaymentRequired")
    want = "exact" if scheme == "exact" else "hsk-paywall-tx"
    acc = next((a for a in body["accepts"] if isinstance(a, dict) and a.get("scheme") == want), None)
    if acc is None:
        raise Refused(f"no {want} option offered")
    x = acc.get("extra") or {}
    if acc.get("network") != P.NETWORK:
        raise Refused("wrong network")
    if str(acc.get("asset", "")).lower() != P.USDCE.lower():
        raise Refused("asset is not USDC.e")
    if str(acc.get("payTo", "")).lower() != contract.lower():
        raise Refused("payTo is not the expected settlement contract")
    if str(x.get("payer", "")).lower() != me.lower():
        raise Refused("quote is bound to another payer")
    u = urllib.parse.urlsplit(url)
    if x.get("resource") != f"GET {u.path}?{u.query.lower()}":
        raise Refused("quote is for another resource")
    eid = int(x["endpointId"])
    amount = int(acc["amount"])
    e = C.read_endpoint(rpc, contract, eid)               # independent of the server
    if e["provider"] != expect_provider.lower():
        raise Refused(f"endpoint {eid} is not run by the pinned provider")
    if e["payee"] != expect_payee.lower():
        raise Refused(f"endpoint {eid} does not pay the pinned payee")
    if not e["active"] or e["price"] != amount:
        raise Refused(f"on-chain price {e['price']} != quoted {amount} (or inactive)")
    if amount > max_units:
        raise Refused(f"price {amount} above --max-price")
    if session.total() + amount > cap_units:
        raise Refused("session spend cap reached")
    _, rid = P.compute_request_id(P.CHAIN_ID, contract.lower(), eid, me.lower(), x["resource"], x["token"].lower())
    if rid != x["requestId"].lower():
        raise Refused("requestId does not match our own computation")
    if scheme == "exact" and P.authorization_nonce(contract.lower(), eid, rid) != str(x.get("nonce", "")).lower():
        raise Refused("nonce does not match authorizationNonce(endpointId, requestId)")
    return acc, eid, amount, rid, e


def verify_paid(rpc, contract, tx, rid, me, amount, eid):
    rc = None
    for _ in range(20):
        rc = rpc("eth_getTransactionReceipt", [tx], tries=2)
        if rc:
            break
        time.sleep(2)
    if not rc or rc.get("status") != "0x1":
        return False, "settlement tx missing or reverted"
    logs = C.paid_logs(rc, contract, rid)
    if len(logs) != 1 or logs[0]["payer"] != me.lower() or logs[0]["amount"] != amount or logs[0]["endpointId"] != eid:
        return False, "no matching Paid event"
    return True, "Paid event matches (payer, amount, endpoint, request)"


def verify_refund(rpc, contract, tx, rid, me, amount):
    rc = rpc("eth_getTransactionReceipt", [tx], tries=3)
    if not rc or rc.get("status") != "0x1":
        return False, "refund tx missing or reverted"
    for lg in rc.get("logs") or []:
        t = lg.get("topics") or []
        if (str(lg.get("address", "")).lower() == contract.lower() and len(t) == 4 and t[0].lower() == REFUNDED_TOPIC
                and t[2].lower() == rid.lower() and t[3][-40:].lower() == me.lower()[2:] and int(lg["data"], 16) == amount):
            pay = C.read_payment(rpc, contract, rid)
            return pay["refunded"], "Refunded event matches; getPayment().refunded = " + str(pay["refunded"])
    return False, "no matching Refunded event"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--scheme", choices=["exact", "tx"], default="exact")
    ap.add_argument("--hsp", action="store_true", help="also sign the HSP-draft mandate and verify the receipt")
    ap.add_argument("--max-price", default="0.05")
    ap.add_argument("--session-cap", default="0.20")
    ap.add_argument("--contract", default=None)
    ap.add_argument("--pin-adapter", default=None, help="HSP adapter address pinned out-of-band")
    ap.add_argument("--key-file", default=os.environ.get("HSK_METER_BUYER_KEY_FILE"),
                    help="file with HKPW_BUYER_PK=0x... (or $HSK_METER_BUYER_KEY_FILE)")
    ap.add_argument("--expect-provider", default=os.environ.get("HSK_METER_PROVIDER"),
                    help="the provider address the endpoint must belong to (or $HSK_METER_PROVIDER)")
    ap.add_argument("--expect-payee", default=os.environ.get("HSK_METER_PAYEE"),
                    help="the payee address the endpoint must pay (or $HSK_METER_PAYEE)")
    ap.add_argument("--session-file", default=SESSION)
    a = ap.parse_args()
    for flag, v in (("--key-file", a.key_file), ("--expect-provider", a.expect_provider), ("--expect-payee", a.expect_payee)):
        if not v:
            sys.exit(f"{flag} is required")
    for flag, v in (("--expect-provider", a.expect_provider), ("--expect-payee", a.expect_payee)):
        if not P.ADDR_RE.match(v):
            sys.exit(f"{flag} must be an address")
    u = urllib.parse.urlsplit(a.url)
    if u.scheme not in ("http", "https") or (u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost")):
        sys.exit("URL must be https (or http on localhost)")
    contract = a.contract or json.load(open(os.path.join(STATE, "deployment.json")))["address"]
    rpc = C.Rpc("https://mainnet.hsk.xyz")
    if C.eth_call(rpc, contract, "token()", [], [], ["address"])[0].lower() != P.USDCE.lower():
        sys.exit("contract does not settle in USDC.e; refusing")
    me = load_key(a.key_file)
    max_units = int(Decimal(a.max_price) * 10**6)
    cap_units = int(Decimal(a.session_cap) * 10**6)
    session = Session(a.session_file)
    out = {"url": a.url, "scheme": a.scheme, "payer": me.address}
    rec = session.reconcile(rpc, contract, me.address)
    if rec:
        out["ledger_reconciled"] = rec
    t0 = time.time()
    st, hdr, body = http_get(a.url, {"X-Payer": me.address})
    out["quote_status"] = st
    if st != 402:
        print(json.dumps({**out, "body": body}, indent=1))
        sys.exit("expected 402")
    hreq = {k.lower(): v for k, v in hdr.items()}.get("payment-required")
    if not hreq or P.parse_b64json(hreq, max_chars=16384) != body:
        sys.exit("PAYMENT-REQUIRED header missing or different from the body")
    try:
        acc, eid, amount, rid, e = check_quote(rpc, body, a.url, me.address, contract, max_units, cap_units, a.scheme,
                                               session, a.expect_provider, a.expect_payee)
    except (Refused, KeyError, ValueError) as err:
        journal({**out, "refused": str(err)})
        sys.exit(f"REFUSED: {err}")
    x = acc["extra"]
    out.update({"endpointId": eid, "amount": amount, "requestId": rid})
    bal_before = C.eth_call(rpc, P.USDCE, "balanceOf(address)", ["address"], [me.address], ["uint256"])[0]
    if a.scheme == "exact":
        now = int(time.time())
        try:   # counted against the cap BEFORE the signature exists
            session.reserve(rid, amount, cap_units, "exact", x["nonce"].lower(), now + AUTH_VALID_S)
        except Refused as err:
            journal({**out, "refused": str(err)})
            sys.exit(f"REFUSED: {err}")
        td = P.receive_auth_typed_data(me.address, contract, amount, 0, now + AUTH_VALID_S, x["nonce"])
        sig = "0x" + me.sign_message(encode_typed_data(full_message=td)).signature.hex()
        payload = {"x402Version": 2, "resource": body.get("resource"), "accepted": acc,
                   "payload": {"signature": sig, "authorization": {
                       "from": me.address, "to": contract, "value": str(amount), "validAfter": "0",
                       "validBefore": str(now + AUTH_VALID_S), "nonce": x["nonce"]}}}
        mandate = None
        if a.hsp:
            hx = (body.get("extensions") or {}).get("hsp-draft")
            if not hx:
                sys.exit("server offered no hsp-draft extension")
            mandate = HSP.mandate_message(payer=me.address, payee=e["payee"], token=P.USDCE, amount=amount, reference=rid,
                                          required_caps=[HSP.CAP_SETTLED], deadline=now + 900)
            msig = HSP.sign_mandate(me, mandate, P.CHAIN_ID)
            payload["extensions"] = {"hsp-draft": {"mandate": HSP.mandate_to_json(mandate), "signature": msig}}
            out["paymentId"] = HSP.mandate_hash(mandate, P.CHAIN_ID)
        h = {"X-Payer": me.address, "PAYMENT-SIGNATURE": P.b64json(payload)}
        for _ in range(8):     # the same header is safe to resend: the server settles a request id at most once
            st, hdr, res = http_get(a.url, h)
            if st not in (409, 503) or not (res or {}).get("retryable"):
                break
            ra = str({k.lower(): v for k, v in hdr.items()}.get("retry-after", "3"))
            time.sleep(min(int(ra), 10) if ra.isascii() and ra.isdigit() else 3)
    else:
        try:
            session.reserve(rid, amount, cap_units, "tx", None, time.time() + TX_RESERVE_S)
        except Refused as err:
            journal({**out, "refused": str(err)})
            sys.exit(f"REFUSED: {err}")
        allowance = C.eth_call(rpc, P.USDCE, "allowance(address,address)", ["address", "address"], [me.address, contract], ["uint256"])[0]
        sender = C.Sender(rpc, me, P.CHAIN_ID, budgets={"settle": 5 * 10**17})
        if allowance < amount:
            rc = sender.send(P.USDCE, C.selector("approve(address,uint256)") + encode(["address", "uint256"], [contract, amount]))
            out["approve_tx"] = rc["transactionHash"]
            for _ in range(15):   # wait until the RPC node we read from sees the allowance (nodes can lag a block)
                if C.eth_call(rpc, P.USDCE, "allowance(address,address)", ["address", "address"], [me.address, contract], ["uint256"])[0] >= amount:
                    break
                time.sleep(2)
        rc = sender.send(contract, C.selector("pay(uint256,bytes32,uint256)") + encode(
            ["uint256", "bytes32", "uint256"], [eid, bytes.fromhex(rid[2:]), amount]), lag_retries=2)
        if rc.get("status") != "0x1":
            sys.exit("pay() reverted")
        out["pay_tx"] = rc["transactionHash"]
        res = None
        for _ in range(5):
            st, hdr, res = http_get(a.url, {"X-Payer": me.address, "X-Paywall-Token": x["token"], "X-Paywall-Tx": rc["transactionHash"]})
            if st not in (402,) or not res.get("retryable"):
                break
            time.sleep(2)
    out["status"] = st
    out["ms"] = int((time.time() - t0) * 1000)
    pr = {k.lower(): v for k, v in hdr.items()}.get("payment-response")
    if pr:
        out["payment_response"] = P.parse_b64json(pr, max_chars=4096)
    pay = (res or {}).get("payment") or {}
    tx = pay.get("tx") or out.get("pay_tx")
    if tx:
        ok, why = verify_paid(rpc, contract, tx, rid, me.address, amount, eid)
        out["settlement_tx"], out["settlement_verified"], out["settlement_check"] = tx, ok, why
        if ok:
            out["session_spent_units"] = session.commit(rid)
    if st == 200:
        out["data"] = res.get("data")
        if a.hsp and res.get("hsp"):
            h = res["hsp"]
            pinned = a.pin_adapter or h.get("adapter")
            dec = HSP.verify(mandate=HSP.mandate_from_json(h["mandate"]), mandate_sig=h["mandateSignature"],
                             receipt=HSP.receipt_from_json(h["receipt"]), receipt_sig=h["receiptSignature"],
                             chain_id=P.CHAIN_ID, pinned_adapter=pinned)
            out["hsp_buyer_decision"] = dec["outcomeClass"]
            out["hsp_adapter_pinned"] = pinned if a.pin_adapter else f"{pinned} (from server: pass --pin-adapter to pin)"
    elif (res or {}).get("refund"):
        r = res["refund"]
        out["refund_tx"] = r.get("tx")
        if r.get("tx"):
            ok, why = verify_refund(rpc, contract, r["tx"], rid, me.address, amount)
            out["refund_verified"], out["refund_check"] = ok, why
            if ok:
                out["session_spent_units"] = session.refunded(rid)
        out["message"] = res.get("message")
    else:
        out["error"] = res
    bal_after = C.eth_call(rpc, P.USDCE, "balanceOf(address)", ["address"], [me.address], ["uint256"])[0]
    for _ in range(5):   # the RPC node we read from can be a block behind the settlement
        if not out.get("settlement_verified") or out.get("refund_verified") or bal_after != bal_before:
            break
        time.sleep(1.5)
        bal_after = C.eth_call(rpc, P.USDCE, "balanceOf(address)", ["address"], [me.address], ["uint256"])[0]
    out["usdce_before"], out["usdce_after"] = bal_before / 1e6, bal_after / 1e6
    journal({k: v for k, v in out.items() if k != "data"})
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
