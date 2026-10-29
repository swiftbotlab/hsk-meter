#!/usr/bin/env python3
"""The unchanged buyer client pays the Worker's code end to end, offline (Swift Bot Labs, HSK Meter).

  python3 worker/test/test_buyer_compat.py          (needs node >= 20 and `npm ci` in worker/)

An offline chain model (server/testkit.FakeChain, extended here with the contract's pay() and USDC.e approve/allowance)
executes the REAL signed transactions the buyer sends. It is served as JSON-RPC on 127.0.0.1; the Worker's request
handler (worker/src/app.js, unchanged) runs under Node on 127.0.0.1 and reads that chain. agent/buyer.py --scheme tx
then pays the Worker exactly as it pays the mainnet endpoint. Checked: the 402 passes the buyer's spend guard, the
paid answer is byte-for-byte the data the Python seller computes for the same chain state, a replay of the paid
request is refused without a second charge, scheme exact is refused before anything is signed, and the free receipt
verifier ACCEPTs the payment. Nothing here touches the network (run it under tools/ci_offline.py).
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "agent"))
import buyer as B          # noqa: E402
import chain as C          # noqa: E402
import protocol as P       # noqa: E402
import seller as S         # noqa: E402
import testkit as T        # noqa: E402
from eth_abi import decode, encode  # noqa: E402
from eth_account import Account     # noqa: E402

CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"
PROVIDER = "0x30ccc6d816854d1a66fbfece15b1515a3e941160"
PAYEE = "0xa8a49a6ca872d54a8fd5fb121139de9bff62a48d"
S_PAY = T.sel("pay(uint256,bytes32,uint256)")
S_APPROVE = T.sel("approve(address,uint256)")
S_ALLOW = T.sel("allowance(address,address)")
USDCE = P.USDCE.lower()


class PayChain(T.FakeChain):
    """FakeChain plus the self-paid path: USDC.e approve/allowance and HskPayPerRequest.pay() with the contract's checks
    (UnknownEndpoint, EndpointInactive, RequestNotForPayer, AlreadyPaid, WrongAmount, allowance and balance)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.allowances = {}
        self.lock = threading.RLock()

    def __call__(self, method, params, tries=1):
        with self.lock:
            return super().__call__(method, params, tries)

    def _call(self, call):
        to, data = call["to"].lower(), call["data"]
        s, args = data[:10], bytes.fromhex(data[10:])
        if to == USDCE and s == S_ALLOW:
            owner, spender = decode(["address", "address"], args)
            return "0x" + encode(["uint256"], [self.allowances.get((owner.lower(), spender.lower()), 0)]).hex()
        if to == USDCE and s == S_APPROVE:
            return "0x" + encode(["bool"], [True]).hex()
        if to == self.contract and s == S_PAY:
            self._simulate(call)
            return "0x"
        return super()._call(call)

    def _simulate(self, call, apply=False, tx_hash=None):
        to = call["to"].lower()
        data = call["data"] if isinstance(call["data"], str) else "0x" + bytes(call["data"]).hex()
        frm = call.get("from", "0x" + "00" * 20).lower()
        s, args = data[:10], bytes.fromhex(data[10:])
        if to == USDCE and s == S_APPROVE:
            spender, amount = decode(["address", "uint256"], args)
            if apply:
                self.allowances[(frm, spender.lower())] = amount
                return "other", []
            return "other"
        if to == self.contract and s == S_PAY:
            eid, rid_b, amount = decode(["uint256", "bytes32", "uint256"], args)
            rid = "0x" + rid_b.hex()
            e = self.endpoints.get(eid)
            if not e:
                raise T.revert("UnknownEndpoint(uint256)", ["uint256"], [eid])
            if not e["active"]:
                raise T.revert("EndpointInactive(uint256)", ["uint256"], [eid])
            bound = "0x" + rid[2:42]
            if bound != "0x" + "00" * 20 and bound != frm:
                raise T.revert("RequestNotForPayer(address)", ["address"], [bound])
            if rid in self.payments:
                raise T.revert("AlreadyPaid(bytes32)", ["bytes32"], [rid_b])
            if amount != e["price"]:
                raise T.revert("WrongAmount(uint256,uint256)", ["uint256", "uint256"], [amount, e["price"]])
            if self.allowances.get((frm, self.contract), 0) < amount or self.usdce.get(frm, 0) < amount:
                raise T.revert(message="ERC20: transfer amount exceeds allowance or balance")
            if apply:
                self.allowances[(frm, self.contract)] -= amount
                self.usdce[frm] -= amount
                self.credit += amount
                self.payments[rid] = {"payer": frm, "endpointId": eid, "refunded": False, "amount": amount,
                                      "paidAt": int(time.time()), "paidBlock": self.block}
                lg = {"address": self.contract, "logIndex": "0x0", "blockNumber": hex(self.block), "transactionHash": tx_hash,
                      "topics": [C.PAID_TOPIC, "0x%064x" % eid, rid, "0x" + "00" * 12 + frm[2:]],
                      "data": "0x" + encode(["address", "uint256"], [self.payee, amount]).hex()}
                self.logs.append(lg)
                return "pay", [lg]
            return "pay"
        return super()._simulate(call, apply=apply, tx_hash=tx_hash)


def rpc_server(chain):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                out = {"jsonrpc": "2.0", "id": req.get("id"), "result": chain(req["method"], req["params"])}
            except C.RpcError as e:
                out = {"jsonrpc": "2.0", "id": req.get("id"),
                       "error": {"code": 3, "message": str(e).replace("RPC error: ", ""), "data": getattr(e, "data", None)}}
            raw = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


WORKER_READY = bool(shutil.which("node")) and os.path.isdir(os.path.join(ROOT, "worker", "node_modules", "@noble"))
if os.environ.get("HSK_REQUIRE_WORKER") and not WORKER_READY:      # CI: a missing toolchain is a failure, not a skip
    sys.exit("node and worker/node_modules are required (run `npm ci` in worker/)")


@unittest.skipUnless(WORKER_READY, "needs node and `npm ci` in worker/")
class BuyerPaysWorker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chain = PayChain(CONTRACT, PROVIDER, PAYEE)
        cls.me = Account.create()
        cls.chain.fund(cls.me.address, 200_000)
        cls.chain.native[cls.me.address.lower()] = 10**18
        cls.target = "0x" + "5a" * 20
        cls.chain.usdce[cls.target] = 1_234_567
        cls.chain.native[cls.target] = 7_812_500_000_000_000          # 0.0078125 HSK: a rounding tie, as Python prints it
        cls.chain.codes[cls.target] = "0x6080"
        cls.chain.nonces[cls.target] = 4
        cls.rpc_srv = rpc_server(cls.chain)
        cls.node = subprocess.Popen(["node", os.path.join(HERE, "serve_local.mjs"), "http://127.0.0.1:%d" % cls.rpc_srv.server_port],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = cls.node.stdout.readline()
        cls.base = "http://127.0.0.1:%d" % json.loads(line)["port"]
        cls.tmp = tempfile.mkdtemp(prefix="hskw-")

    @classmethod
    def tearDownClass(cls):
        cls.node.terminate()
        cls.node.wait(5)
        cls.node.stdout.close()
        cls.node.stderr.close()
        cls.rpc_srv.shutdown()
        cls.rpc_srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def buy(self, url, **kw):
        args = dict(me=self.me, rpc=self.chain, contract=CONTRACT, expect_provider=PROVIDER, expect_payee=PAYEE,
                    max_units=50_000, cap_units=200_000, scheme="tx", session_file=os.path.join(self.tmp, "session.json"),
                    journal_file=os.path.join(self.tmp, "runs.jsonl"), budget_s=60)
        args.update(kw)
        return B.buy(url, **args)

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, dict(r.headers), json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), json.loads(e.read())

    def test_1_buyer_pays_and_gets_the_python_sellers_data(self):
        url = self.base + "/v1/hsk/address-profile?address=" + self.target
        before = self.chain.usdce[self.me.address.lower()]
        out = self.buy(url)
        self.assertTrue(out.get("delivered"), out)
        self.assertEqual(out["payment"], "settled")
        self.assertTrue(out["settlement_verified"], out.get("settlement_check"))
        self.assertEqual(out["amount"], 30000)
        self.assertEqual(self.chain.usdce[self.me.address.lower()], before - 30000)
        want = S.address_profile(self.chain, self.target)              # the Python seller's own function, same chain
        self.assertEqual(json.dumps(out["data"], sort_keys=True), json.dumps(want, sort_keys=True))
        self.assertEqual(out["data"]["balances"]["HSK"], "0.007812")
        type(self).paid_tx = out["settlement_tx"]
        type(self).paid_rid = out["requestId"]

    def test_2_replay_is_refused_and_not_charged_again(self):
        url = self.base + "/v1/hsk/address-profile?address=" + self.target
        before = self.chain.usdce[self.me.address.lower()]
        out = self.buy(url)                      # within 10 min of a delivery the buyer resends the SAME paid request
        self.assertTrue(out.get("resent"), out)
        self.assertFalse(out.get("delivered"))
        self.assertEqual(out["error"]["error"], "already_served")
        self.assertEqual(self.chain.usdce[self.me.address.lower()], before, "a replay never charges again")
        with open(os.path.join(self.tmp, "runs.jsonl")) as f:
            j = [json.loads(x) for x in f]
        self.assertEqual(j[-1].get("error", {}).get("error"), "already_served")
        out2 = self.buy(url, new_purchase=True)  # positive control: a new purchase pays and is served
        self.assertTrue(out2.get("delivered"), out2)
        self.assertNotEqual(out2["requestId"], self.paid_rid)
        self.assertEqual(self.chain.usdce[self.me.address.lower()], before - 30000)

    def test_3_tx_check_matches_the_python_seller(self):
        h = self.paid_tx
        out = self.buy(self.base + "/v1/hsk/tx-receipt?hash=" + h)
        self.assertTrue(out.get("delivered"), out)
        self.assertEqual(json.dumps(out["data"], sort_keys=True), json.dumps(S.tx_receipt(self.chain, h), sort_keys=True))
        st, _, body = self.get("/v1/hsk/tx-receipt?hash=0x" + "99" * 32, {"X-Payer": self.me.address})
        self.assertEqual((st, body["error"]), (404, "not_found"))     # free 404, nothing to pay for

    def test_4_scheme_exact_is_refused_before_signing(self):
        with self.assertRaises(B.Refused) as cm:
            self.buy(self.base + "/v1/hsk/address-profile?address=" + PAYEE, scheme="exact")
        self.assertIn("no exact option offered", str(cm.exception))

    def test_5_receipt_verifier_accepts_the_payment(self):
        st, _, body = self.get("/v1/receipt/" + self.paid_rid)
        self.assertEqual((st, body["verdict"]), (200, "ACCEPT"), body)
        self.assertEqual(body["servedByThisEndpoint"]["state"], "served")
        st, _, body = self.get("/v1/receipt/" + self.paid_tx + "?payer=" + self.me.address.lower() + "&endpoint=1&amount=30000")
        self.assertEqual(body["verdict"], "ACCEPT", body)
        st, _, body = self.get("/v1/receipt/" + self.paid_tx + "?endpoint=2")
        self.assertEqual(body["verdict"], "REJECT")

    def test_6_raw_replay_of_the_paid_headers(self):
        st, hdr, q = self.get("/v1/hsk/address-profile?address=" + CONTRACT, {"X-Payer": self.me.address})
        self.assertEqual(st, 402)
        self.assertEqual(P.parse_b64json({k.lower(): v for k, v in hdr.items()}["payment-required"], max_chars=16384), q)
        x = q["accepts"][0]["extra"]
        sender = C.Sender(self.chain, self.me, P.CHAIN_ID, budgets={"settle": 10**18})
        sender.send(P.USDCE, C.selector("approve(address,uint256)") + encode(["address", "uint256"], [CONTRACT, 30000]))
        rc = sender.send(CONTRACT, C.selector("pay(uint256,bytes32,uint256)") + encode(
            ["uint256", "bytes32", "uint256"], [1, bytes.fromhex(x["requestId"][2:]), 30000]))
        h = {"X-Payer": self.me.address, "X-Paywall-Token": x["token"], "X-Paywall-Tx": rc["transactionHash"]}
        st, hdr, body = self.get("/v1/hsk/address-profile?address=" + CONTRACT, h)
        self.assertEqual(st, 200, body)
        self.assertEqual(P.parse_b64json({k.lower(): v for k, v in hdr.items()}["payment-response"])["transaction"], rc["transactionHash"])
        for _ in range(2):
            st, _, body = self.get("/v1/hsk/address-profile?address=" + CONTRACT, h)
            self.assertEqual((st, body["error"]), (429, "already_served"))
            self.assertNotIn("data", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
