#!/usr/bin/env python3
"""Tests for the MCP server (agent/mcp_server.py). Offline: the REAL seller (server/seller.py) runs over HTTP on a
random loopback port on top of server/testkit.FakeChain, which executes the real signed transactions; the MCP server
pays it through agent/buyer.py's guard exactly as on mainnet.

  test_mcp.py [-v]
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, HERE)
import buyer as B          # noqa: E402
import mcp_server as M     # noqa: E402
import seller as S         # noqa: E402
import testkit as T        # noqa: E402
from eth_account import Account  # noqa: E402

CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"
PROFILE = "/v1/hsk/address-profile?address=0x" + "01" * 20
DEAD = "0x000000000000000000000000000000000000dead"


class Env:
    def __init__(self, cap_units=200_000, max_units=50_000, smoke=None, pin=True, pays_per_minute=6, fund=10**7):
        base = os.path.join(ROOT, "tmp")
        self.d = tempfile.mkdtemp(dir=base if os.path.isdir(base) else None)
        self.provider, self.me, self.adapter = Account.create(), Account.create(), Account.create()
        self.payee = "0x" + "aa" * 20
        self.ch = T.FakeChain(CONTRACT, self.provider.address, self.payee)
        self.ch.fund(self.me.address, fund)
        self.seller = S.Seller({"contract": CONTRACT, "tx_wait_s": 0.3, "smoke_refund_address": smoke,
                                "bind_host": "127.0.0.1"}, rpc=self.ch, provider=self.provider, adapter=self.adapter,
                               token_key=os.urandom(32))
        self.httpd = S.Server(("127.0.0.1", 0), S.make_handler(self.seller))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.session = os.path.join(self.d, "mcp_session.json")
        self.mcp = M.McpServer(account=self.me, rpc=self.ch, contract=CONTRACT, expect_provider=self.provider.address,
                               expect_payee=self.payee, allow_origins=[self.base], max_units=max_units,
                               cap_units=cap_units, session_file=self.session,
                               pin_adapter=self.adapter.address if pin else None, pays_per_minute=pays_per_minute)
        self.n = 0

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.d)

    def rpc(self, method, params=None, note=False):
        self.n += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not note:
            msg["id"] = self.n
        return self.mcp.handle(msg)

    def tool(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        self.last = r
        res = r["result"]
        return res, (res.get("structuredContent") if not res["isError"] else res["content"][0]["text"])

    def sent(self, sel_name):
        return sum(1 for s in self.ch.sent if s == {"pwa": T.S_PWA, "refund": T.S_REFUND}[sel_name])


class Protocol(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def test_initialize_and_list(self):
        r = self.e.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertIsNone(self.e.rpc("notifications/initialized", note=True), "notifications get no answer")
        names = [t["name"] for t in self.e.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["hsk_meter_quote", "hsk_meter_pay", "hsk_meter_status"])
        old = self.e.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(old["result"]["protocolVersion"], M.PROTOCOL_VERSIONS[0], "unknown version -> our latest")
        self.assertEqual(self.e.rpc("ping")["result"], {})

    def test_bad_messages(self):
        self.assertEqual(self.e.rpc("nope")["error"]["code"], -32601)
        self.assertEqual(self.e.mcp.handle({"id": 1, "method": "ping"})["error"]["code"], -32600)
        self.assertEqual(self.e.mcp.handle([1, 2])["error"]["code"], -32600)
        self.assertEqual(self.e.rpc("tools/call", {"name": "rm_rf", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.e.rpc("tools/call", {"name": "hsk_meter_pay", "arguments": "x"})["error"]["code"], -32602)
        self.assertEqual(self.e.rpc("tools/call", "x")["error"]["code"], -32602)

    def test_stdio_framing(self):
        msgs = [b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n', b"not json\n", b"\n", b"[1]\n",
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
                b'{"jsonrpc":"2.0","id":2,"method":"x"' + b" " * (M.MAX_LINE + 10) + b"}\n",
                b'{"jsonrpc":"2.0","id":3,"method":"tools/list"}\n']
        out = io.BytesIO()
        self.e.mcp.serve(io.BytesIO(b"".join(msgs)), out)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([x.get("id") for x in lines], [1, None, None, None, 3])
        self.assertEqual([x.get("error", {}).get("code") for x in lines], [None, -32700, -32600, -32700, None])

    def test_cli_needs_operator_limits(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "mcp_server.py")], capture_output=True, text=True,
                           env={"PATH": os.environ.get("PATH", "")}, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("required", r.stderr)


class Paying(unittest.TestCase):
    def tearDown(self):
        self.e.close()

    def test_quote_signs_and_reserves_nothing(self):
        self.e = Env()
        res, q = self.e.tool("hsk_meter_quote", url=self.e.base + PROFILE)
        self.assertFalse(res["isError"], q)
        self.assertEqual((q["endpointId"], q["amountUnits"], q["guard"]), (1, 30000, "pass"))
        self.assertEqual(B.Session(self.e.session).total(), 0)
        self.assertEqual(self.e.ch.sent, [])

    def test_pay_with_hsp_end_to_end(self):
        self.e = Env()
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)
        self.assertFalse(res["isError"], r)
        self.assertEqual(r["httpStatus"], 200)
        self.assertTrue(r["settlement"]["verifiedOnChain"])
        self.assertEqual(r["hsp"]["decision"], "ACCEPT")
        self.assertIn("untrusted", r["dataNote"])
        self.assertEqual(r["sessionSpentUnits"], 30000)
        self.assertEqual(self.e.sent("pwa"), 1)
        self.assertEqual(self.e.ch.usdce[self.e.me.address.lower()], 10**7 - 30000)
        st = self.e.tool("hsk_meter_status")[1]
        self.assertEqual((st["spentUnits"], st["remainingUnits"]), (30000, 170000))

    def test_refund_path_gives_the_cap_back(self):
        self.e = Env(smoke=DEAD)
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + "/v1/hsk/address-profile?address=" + DEAD, hsp=False)
        self.assertFalse(res["isError"], r)
        self.assertTrue(r["settlement"]["verifiedOnChain"])
        self.assertTrue(r["refund"]["verifiedOnChain"], r)
        self.assertEqual(self.e.ch.usdce[self.e.me.address.lower()], 10**7, "refunded on-chain")
        self.assertEqual(B.Session(self.e.session).total(), 0, "a verified refund releases the cap")

    def test_session_cap_stops_spending_before_signing(self):
        self.e = Env(cap_units=50_000)
        self.assertFalse(self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE)[0]["isError"])
        res, text = self.e.tool("hsk_meter_pay", url=self.e.base + "/v1/hsk/address-profile?address=0x" + "02" * 20)
        self.assertTrue(res["isError"])
        self.assertIn("cap", text)
        self.assertEqual(self.e.sent("pwa"), 1, "no second settlement")

    def test_client_cannot_raise_the_price_limit(self):
        self.e = Env(max_units=20_000)
        res, text = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, max_price="1.0")
        self.assertTrue(res["isError"])
        self.assertIn("operator", text)
        res, text = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE)          # 0.03 > operator's 0.02
        self.assertTrue(res["isError"])
        self.assertIn("price", text)
        self.assertEqual(self.e.ch.sent, [])
        self.assertEqual(B.Session(self.e.session).total(), 0)

    def test_lower_client_limit_applies(self):
        self.e = Env()
        res, text = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, max_price="0.02")
        self.assertTrue(res["isError"])
        self.assertEqual(self.e.ch.sent, [])

    def test_urls_outside_the_allowlist_are_refused_without_a_request(self):
        self.e = Env()
        calls = []
        self.e.mcp.http = lambda url, headers, timeout=120: calls.append(url) or (500, {}, {})
        port = int(self.e.base.rsplit(":", 1)[1])
        for bad in ["http://127.0.0.1:%d%s" % (port + 1, PROFILE), "https://evil.example" + PROFILE,
                    "http://user:pw@127.0.0.1:%d%s" % (port, PROFILE), self.e.base + "/admin", self.e.base + PROFILE + "#x",
                    "file:///etc/passwd", "x" * 500, self.e.base + "/v1/hsk/address-profile?address=0x 01", 42]:
            res, text = self.e.tool("hsk_meter_pay", url=bad)
            self.assertTrue(res["isError"], bad)
        res, text = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, extra="x")
        self.assertTrue(res["isError"])
        self.assertIn("unknown argument", text)
        self.assertEqual(calls, [], "nothing was fetched")

    def test_hsp_needs_a_pinned_adapter(self):
        self.e = Env(pin=False)
        res, text = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)
        self.assertTrue(res["isError"])
        self.assertIn("adapter", text)

    def test_wrong_pinned_provider_is_refused(self):
        self.e = Env()
        self.e.mcp.provider = "0x" + "12" * 20
        res, text = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE)
        self.assertTrue(res["isError"])
        self.assertIn("pinned provider", text)
        self.assertEqual(self.e.ch.sent, [])

    def test_rate_limit(self):
        self.e = Env(pays_per_minute=2, cap_units=10**6)
        for i in range(2):
            self.assertFalse(self.e.tool("hsk_meter_pay", url=self.e.base + "/v1/hsk/address-profile?address=0x" + ("%02x" % (i + 1)) * 20)[0]["isError"])
        res, text = self.e.tool("hsk_meter_pay", url=self.e.base + "/v1/hsk/address-profile?address=0x" + "09" * 20)
        self.assertTrue(res["isError"])
        self.assertIn("rate limit", text)

    def test_seller_errors_are_reported_without_payment(self):
        self.e = Env()
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + "/v1/hsk/tx-receipt?hash=0x" + "ab" * 32)
        self.assertFalse(res["isError"], r)
        self.assertEqual((r["httpStatus"], r["paid"]), (404, False))
        self.assertEqual(self.e.ch.sent, [])


class Laggy:
    """RPC wrapper for the buyer side: the first `lag` getPayment reads come from a node one block behind (the
    payment still shows refunded=False), as seen live on mainnet on 2026-10-23."""

    def __init__(self, ch, lag):
        self.ch, self.lag, self.stale_reads = ch, lag, 0

    def __call__(self, method, params, tries=1):
        out = self.ch(method, params, tries)
        if method == "eth_call" and params[0]["data"].startswith(T.S_GETPAY) and self.lag > 0:
            from eth_abi import decode, encode
            p = list(decode(T.PAYMENT_T, bytes.fromhex(out[2:]))[0])
            if p[2]:
                self.lag -= 1
                self.stale_reads += 1
                p[2] = False
                return "0x" + encode(T.PAYMENT_T, [tuple(p)]).hex()
        return out


class RefundOnLaggingNode(unittest.TestCase):
    def tearDown(self):
        self.e.close()

    def test_refund_verified_despite_a_lagging_node(self):
        self.e = Env(smoke=DEAD)
        lag = Laggy(self.e.ch, lag=2)
        self.e.mcp.rpc = lag
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + "/v1/hsk/address-profile?address=" + DEAD, hsp=False)
        self.assertFalse(res["isError"], r)
        self.assertEqual(lag.stale_reads, 2, "the stale answers were really served")
        self.assertTrue(r["refund"]["verifiedOnChain"], r)
        self.assertEqual(B.Session(self.e.session).total(), 0, "the refunded amount is not counted as spent")

    def test_positive_control_single_read_fails_on_a_lagging_node(self):
        self.e = Env(smoke=DEAD)
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + "/v1/hsk/address-profile?address=" + DEAD, hsp=False)
        tx, rid = r["refund"]["tx"], r["requestId"]
        ok, why = B.verify_refund(Laggy(self.e.ch, lag=1), CONTRACT, tx, rid, self.e.me.address, 30000, state_tries=1, wait_s=0)
        self.assertFalse(ok, "one read on a lagging node misses the refund (the bug seen live)")
        ok, why = B.verify_refund(Laggy(self.e.ch, lag=1), CONTRACT, tx, rid, self.e.me.address, 30000, wait_s=0)
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main()
