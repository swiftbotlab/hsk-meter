#!/usr/bin/env python3
"""Hostile-input tests against a RUNNING seller (default http://127.0.0.1:8402). Every case except `--spend` is refused
before any settlement, so nothing is paid. --spend adds one real 0.005 USDC.e payment and replays its header to prove
the serve cap (3) and that a replay never settles twice.

  test_live.py [--base URL] [--spend]
"""
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chain as C        # noqa: E402
import hsp_draft as HSP  # noqa: E402
import protocol as P     # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402

BASE = "http://127.0.0.1:8402"
SPEND = "--spend" in sys.argv
if "--base" in sys.argv:
    BASE = sys.argv[sys.argv.index("--base") + 1]
    del sys.argv[sys.argv.index("--base"):sys.argv.index("--base") + 2]
if SPEND:
    sys.argv.remove("--spend")
ENV = {}
with open("secrets/hashkey_paywall_keys.env") as f:
    for line in f:
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            ENV[k] = v
BUYER = Account.from_key(ENV["HKPW_BUYER_PK"])
PROFILE = "/v1/hsk/address-profile?address=0x0000000000000000000000000000000000000001"
TXR = "/v1/hsk/tx-receipt?hash=0x98526a6b276b3763cde68297c29f89f194a5fed99a0add55794f57ece123da79"


def req(path, headers=None, method="GET"):
    r = urllib.request.Request(BASE + path, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, dict(e.headers), json.loads(raw or b"{}")
        except ValueError:
            return e.code, dict(e.headers), {"raw": raw[:200].decode("utf-8", "replace")}


def quote(path, payer=BUYER.address):
    st, h, body = req(path, {"X-Payer": payer})
    assert st == 402, (st, body)
    return body


def payload(body, signer=BUYER, value=None, valid_before=None, frm=None, nonce=None):
    acc = body["accepts"][0]
    x = acc["extra"]
    vb = valid_before or int(time.time()) + 120
    v = int(acc["amount"]) if value is None else value
    n = nonce or x["nonce"]
    f = frm or BUYER.address
    td = P.receive_auth_typed_data(f, acc["payTo"], v, 0, vb, n)
    sig = "0x" + signer.sign_message(encode_typed_data(full_message=td)).signature.hex()
    return {"x402Version": 2, "accepted": acc, "payload": {"signature": sig, "authorization": {
        "from": f, "to": acc["payTo"], "value": str(v), "validAfter": "0", "validBefore": str(vb), "nonce": n}}}


class Live(unittest.TestCase):
    def test_free_endpoints(self):
        st, _, b = req("/health")
        self.assertEqual(st, 200)
        self.assertTrue(b["ok"])
        st, _, b = req("/.well-known/x402")
        self.assertEqual((st, b["network"]), (200, P.NETWORK))
        self.assertEqual(req("/nope")[0], 404)
        self.assertEqual(req("/v1/hsk/address-profile?address=" + "a" * 500)[0], 414)

    def test_methods(self):
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(req(PROFILE, method=m)[0], 405, m)

    def test_bad_query_and_payer(self):
        self.assertEqual(req(PROFILE)[2]["error"], "payer_required")
        self.assertEqual(req(PROFILE, {"X-Payer": "0x123"})[2]["error"], "bad_payer")
        for q in ["/v1/hsk/address-profile?address=xyz", "/v1/hsk/address-profile",
                  "/v1/hsk/address-profile?address=0x0000000000000000000000000000000000000001&x=1",
                  "/v1/hsk/address-profile?addr=0x0000000000000000000000000000000000000001",
                  "/v1/hsk/tx-receipt?hash=0x12", "/v1/hsk/address-profile?address=0x0000000000000000000000000000000000000001&address=0x0000000000000000000000000000000000000002"]:
            st, _, b = req(q, {"X-Payer": BUYER.address})
            self.assertEqual((st, b["error"]), (400, "bad_request"), q)

    def test_garbage_payment_headers(self):
        for h in ["!!!", "a" * 9000, P.b64json({"x402Version": 1}), P.b64json([1]), "e30="]:
            st, _, b = req(PROFILE, {"X-Payer": BUYER.address, "PAYMENT-SIGNATURE": h})
            self.assertEqual((st, b["error"]), (400, "bad_payment_payload"), h[:20])

    def test_forged_token_rejected(self):
        body = quote(PROFILE)
        p = payload(body)
        p["accepted"] = json.loads(json.dumps(p["accepted"]))
        p["accepted"]["extra"]["token"] = "0x" + "ab" * 32
        st, _, b = req(PROFILE, {"PAYMENT-SIGNATURE": P.b64json(p)})
        self.assertEqual((st, b["error"]), (402, "payment_mismatch"))
        self.assertIn("token", b["message"])

    def test_payment_for_another_resource_rejected(self):
        body = quote(PROFILE)
        st, _, b = req(TXR, {"PAYMENT-SIGNATURE": P.b64json(payload(body))})
        self.assertEqual((st, b["error"]), (402, "payment_mismatch"))

    def test_wrong_amount_window_nonce_payer(self):
        body = quote(PROFILE)
        price = int(body["accepts"][0]["amount"])
        st, _, b = req(PROFILE, {"PAYMENT-SIGNATURE": P.b64json(payload(body, value=price - 1))})
        self.assertEqual((st, b["error"]), (402, "price_changed"))
        st, _, b = req(PROFILE, {"PAYMENT-SIGNATURE": P.b64json(payload(body, valid_before=int(time.time()) + 7200))})
        self.assertEqual((st, b["error"]), (402, "bad_validity_window"))
        st, _, b = req(PROFILE, {"PAYMENT-SIGNATURE": P.b64json(payload(body, nonce="0x" + "11" * 32))})
        self.assertEqual((st, b["error"]), (402, "payment_mismatch"))
        st, _, b = req(PROFILE, {"X-Payer": "0x" + "22" * 20, "PAYMENT-SIGNATURE": P.b64json(payload(body))})
        self.assertEqual((st, b["error"]), (402, "payment_mismatch"))
        other = Account.create()   # authorization "from" someone the quote was not bound to
        st, _, b = req(PROFILE, {"PAYMENT-SIGNATURE": P.b64json(payload(body, signer=other, frm=other.address))})
        self.assertEqual((st, b["error"]), (402, "payment_mismatch"))

    def test_forged_signature_refused_by_simulation_without_spending(self):
        provider = json.load(open(os.path.join(os.path.dirname(HERE), "ops", "state", "addresses.json")))["provider"]
        rpc = C.Rpc("https://mainnet.hsk.xyz")
        n0 = int(rpc("eth_getTransactionCount", [provider, "latest"], tries=3), 16)
        body = quote(PROFILE)
        st, _, b = req(PROFILE, {"PAYMENT-SIGNATURE": P.b64json(payload(body, signer=Account.create()))})
        self.assertEqual((st, b["error"]), (402, "settlement_refused"))
        self.assertEqual(b["message"], "FiatTokenV2: invalid signature")
        self.assertEqual(int(rpc("eth_getTransactionCount", [provider, "latest"], tries=3), 16), n0, "no tx was sent")

    def test_bad_hsp_mandate_refused_before_settlement(self):
        body = quote(PROFILE)
        p = payload(body)
        hx = body["extensions"]["hsp-draft"]
        m = HSP.mandate_from_json({**hx["mandate"], "amount": "1"})
        p["extensions"] = {"hsp-draft": {"mandate": HSP.mandate_to_json(m), "signature": HSP.sign_mandate(BUYER, m, 177)}}
        st, _, b = req(PROFILE, {"PAYMENT-SIGNATURE": P.b64json(p)})
        self.assertEqual((st, b["error"]), (400, "hsp_mandate_invalid"))
        m = HSP.mandate_from_json(hx["mandate"])
        p["extensions"] = {"hsp-draft": {"mandate": HSP.mandate_to_json(m), "signature": HSP.sign_mandate(Account.create(), m, 177)}}
        st, _, b = req(PROFILE, {"PAYMENT-SIGNATURE": P.b64json(p)})
        self.assertEqual((st, b["error"]), (400, "hsp_mandate_invalid"))

    @unittest.skipUnless(SPEND, "spends 0.005 USDC.e: run with --spend")
    def test_replay_serves_at_most_3_and_settles_once(self):
        body = quote(TXR)
        h = {"PAYMENT-SIGNATURE": P.b64json(payload(body))}
        txs = set()
        for i in range(3):
            st, hdr, b = req(TXR, h)
            self.assertEqual(st, 200, b)
            txs.add(b["payment"]["tx"])
        self.assertEqual(len(txs), 1, "a replayed header never settles twice")
        st, _, b = req(TXR, h)
        self.assertEqual((st, b["error"]), (429, "already_served"))
        print("\nreplay test settlement tx:", txs.pop())


if __name__ == "__main__":
    unittest.main()
