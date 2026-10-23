#!/usr/bin/env python3
"""Tests for the MCP server (agent/mcp_server.py). Offline: the REAL seller (server/seller.py) runs over HTTP on a
random loopback port on top of server/testkit.FakeChain, which executes the real signed transactions; the MCP server
pays it through agent/buyer.py's guard exactly as on mainnet. Every run journals into its own temp dir: the tests
never write ops/state/*.jsonl (checked by Journal).

  test_mcp.py [-v]
"""
import copy
import importlib
import io
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, HERE)
import buyer as B          # noqa: E402
import mcp_server as M     # noqa: E402
import seller as S         # noqa: E402
import chain as C          # noqa: E402
import hsp_draft as HSP    # noqa: E402
import protocol as P       # noqa: E402
import testkit as T        # noqa: E402
from eth_account import Account  # noqa: E402

CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"
PROFILE = "/v1/hsk/address-profile?address=0x" + "01" * 20
DEAD = "0x000000000000000000000000000000000000dead"
BAL = "0x70a08231"          # balanceOf(address)


def profile(i):
    return "/v1/hsk/address-profile?address=0x" + ("%02x" % i) * 20


class Env:
    def __init__(self, cap_units=200_000, max_units=50_000, smoke=None, pin=True, pays_per_minute=6, fund=10**7,
                 **mcp_kw):
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
        self.journal = os.path.join(self.d, "mcp_runs.jsonl")
        self.mcp = self.server(max_units=max_units, cap_units=cap_units, pin=pin, pays_per_minute=pays_per_minute, **mcp_kw)
        self.n = 0

    def server(self, max_units=50_000, cap_units=200_000, pin=True, pays_per_minute=6, **kw):
        """Another MCP server process on the same ledger (same operator config)."""
        return M.McpServer(account=self.me, rpc=self.ch, contract=CONTRACT, expect_provider=self.provider.address,
                           expect_payee=self.payee, allow_origins=[self.base], max_units=max_units,
                           cap_units=cap_units, session_file=self.session,
                           pin_adapter=self.adapter.address if pin else None, pays_per_minute=pays_per_minute,
                           journal_file=self.journal, **kw)

    def wrap_paid(self, fn):
        """Route the paid request (PAYMENT-SIGNATURE / X-Paywall-Tx) through fn(st, hdr, body) after the real seller
        answered it; quotes pass through unchanged."""
        real = self.mcp.http

        def http(url, headers, timeout=120):
            st, hdr, body = real(url, headers, timeout)
            if "PAYMENT-SIGNATURE" in headers or "X-Paywall-Tx" in headers:
                return fn(st, hdr, body)
            return st, hdr, body
        self.mcp.http = http
        return real

    def charged(self):
        return 10**7 - self.ch.usdce[self.me.address.lower()]

    def ledger(self):
        """(spent, reserved) units in the session ledger."""
        with B.Session(self.session)._ledger() as d:
            return d["spent_units"], sum(r["amount"] for r in d["reserved"].values())

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
        assert "result" in r, f"JSON-RPC error instead of a tool result: {r}"
        res = r["result"]
        return res, (res["structuredContent"] if "structuredContent" in res else res["content"][0]["text"])

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
        self.assertEqual((r["outcome"], r["delivered"], r["payment"]), ("delivered", True, "settled"))
        self.assertTrue(r["settlement"]["verifiedOnChain"])
        self.assertEqual(r["hsp"]["decision"], "ACCEPT")
        self.assertTrue(r["hspVerified"])
        self.assertIn("untrusted", r["dataNote"])
        self.assertEqual(r["sessionSpentUnits"], 30000)
        self.assertEqual(self.e.sent("pwa"), 1)
        self.assertEqual(self.e.ch.usdce[self.e.me.address.lower()], 10**7 - 30000)
        st = self.e.tool("hsk_meter_status")[1]
        self.assertEqual((st["spentUnits"], st["remainingUnits"]), (30000, 170000))

    def test_refund_path_gives_the_cap_back(self):
        self.e = Env(smoke=DEAD)
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + "/v1/hsk/address-profile?address=" + DEAD, hsp=False)
        self.assertTrue(res["isError"], "not delivered")
        self.assertEqual((r["outcome"], r["delivered"], r["payment"]), ("refunded_not_delivered", False, "refunded"))
        self.assertTrue(r["settlement"]["verifiedOnChain"])
        self.assertTrue(r["refund"]["verifiedOnChain"], r)
        self.assertEqual(r["refund"]["status"], "verified")
        self.assertIn("nothing is spent", r["advice"])
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
        self.assertEqual(r["outcome"], "refunded_not_delivered", r)
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



class Stdio(unittest.TestCase):
    """MCP-5: one line can never end the stdio loop; notifications never pay."""

    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def run_lines(self, *lines):
        out = io.BytesIO()
        self.e.mcp.serve(io.BytesIO(b"".join(lines)), out)
        return [json.loads(x) for x in out.getvalue().splitlines()]

    def test_null_and_scalar_lines(self):
        ping = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        got = self.run_lines(b"null\n", ping)                           # was: UnboundLocalError, server exits
        self.assertEqual([(x.get("id"), x.get("error", {}).get("code")) for x in got], [(None, -32600), (1, None)])
        got = self.run_lines(b'{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n', b"null\n", ping)
        self.assertEqual([x.get("id") for x in got], [7, None, 1], "the previous response is never re-sent")
        got = self.run_lines(b"42\n", b'"x"\n', b"true\n", ping)
        self.assertEqual([x.get("error", {}).get("code") for x in got], [-32600, -32600, -32600, None])

    def test_deep_nesting(self):
        ping = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        deep = (b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"hsk_meter_pay","arguments":'
                b'{"url":"x","max_price":' + b"[" * 200_000 + b"]" * 200_000 + b"}}}\n")
        self.assertLess(len(deep), M.MAX_LINE)
        got = self.run_lines(ping, deep, ping)                          # was: RecursionError, server exits
        self.assertEqual([(x.get("id"), x.get("error", {}).get("code")) for x in got], [(1, None), (None, -32600), (1, None)])
        within = b'{"jsonrpc":"2.0","id":3,"method":"ping","params":{"_meta":' + b"[" * 60 + b"]" * 60 + b"}}\n"
        self.assertEqual(self.run_lines(within)[0], {"jsonrpc": "2.0", "id": 3, "result": {}})
        self.assertEqual(M.nesting_depth(b'{"a":"[[[[{{{{","b":[1,{"c":"\\"]["}]}'), 3, "brackets in strings ignored")
        self.assertEqual(self.e.ch.sent, [])

    def test_bad_ids_are_invalid_requests(self):
        got = self.run_lines(b'{"jsonrpc":"2.0","id":null,"method":"ping"}\n',
                             b'{"jsonrpc":"2.0","id":[1],"method":"ping"}\n',
                             b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n',
                             b'{"jsonrpc":"2.0","id":"a","method":"ping"}\n')
        self.assertEqual([(x.get("id"), x.get("error", {}).get("code")) for x in got],
                         [(None, -32600), (None, -32600), (None, -32600), ("a", None)])

    def test_tools_call_as_notification_never_pays(self):
        r = self.e.mcp.handle({"jsonrpc": "2.0", "method": "tools/call",
                               "params": {"name": "hsk_meter_pay", "arguments": {"url": self.e.base + PROFILE}}})
        self.assertIsNone(r)
        self.assertEqual(self.e.ch.sent, [], "no settlement for a call that has no response")
        self.assertEqual(self.e.charged(), 0)
        self.assertEqual(B.Session(self.e.session).total(), 0)


class FlakyBalance:
    """Buyer-side RPC whose n-th USDC.e balanceOf read fails (1st = before paying, 2nd = after settlement)."""

    def __init__(self, ch, fail_at=2):
        self.ch, self.n, self.fail_at = ch, 0, fail_at

    def __call__(self, method, params, tries=1):
        if method == "eth_call" and params[0]["data"].startswith(BAL):
            self.n += 1
            if self.n == self.fail_at:
                raise C.RpcError("RPC unavailable: <urlopen error timed out>")
        return self.ch(method, params, tries)


class FailingReceipts:
    """Buyer-side RPC: the first `fails` receipt reads answer a JSON-RPC error (e.g. a public node's rate limit)."""

    def __init__(self, ch, fails=1, msg="RPC error: rate limit exceeded"):
        self.ch, self.fails, self.msg, self.receipt_params = ch, fails, msg, []

    def __call__(self, method, params, tries=1):
        if method == "eth_getTransactionReceipt":
            self.receipt_params.append(params[0])
            if self.fails > 0:
                self.fails -= 1
                raise C.RpcError(self.msg)
        return self.ch(method, params, tries)


class AfterPayment(unittest.TestCase):
    """MCP-1 / B2B-1: nothing that fails after the payment may be reported as 'try again' or lose the proof, and a
    retry of an unconfirmed payment resends the same signature instead of paying twice."""

    def setUp(self):
        self.saved = (getattr(B, "RECEIPT_POLLS", None), getattr(B, "RECEIPT_WAIT_S", None))
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = 3, 0          # no 40 s receipt polling in tests

    def tearDown(self):
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = self.saved
        self.e.close()

    def test_rpc_error_after_a_served_payment_returns_the_data(self):
        self.e = Env()
        self.e.mcp.rpc = FlakyBalance(self.e.ch)
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)
        self.assertFalse(res["isError"], r)
        self.assertEqual((r["outcome"], r["payment"]), ("delivered", "settled"))
        self.assertIn("data", r)
        self.assertIsNone(r["usdceAfter"], "the failed read is reported as unknown")
        self.assertEqual((self.e.sent("pwa"), self.e.charged(), self.e.ledger()), (1, 30000, (30000, 0)))

    def test_rate_limited_receipt_read_is_retried(self):
        self.e = Env(pin=False)
        self.e.mcp.rpc = FailingReceipts(self.e.ch, fails=1)
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE)
        self.assertFalse(res["isError"], r)
        self.assertTrue(r["settlement"]["verifiedOnChain"])
        self.assertEqual((self.e.sent("pwa"), self.e.ledger()), (1, (30000, 0)))

    def lost_answer(self, exc):
        """The seller settles and serves, then the answer is lost (exc raised on our side); the agent retries."""
        self.e = Env()
        real = self.e.wrap_paid(lambda st, hdr, body: (_ for _ in ()).throw(exc))
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)
        self.assertTrue(res["isError"])
        self.assertIsInstance(r, dict, "a structured result, not a refusal or a JSON-RPC error")
        self.assertEqual((r["outcome"], r["payment"], r["delivered"]), ("payment_unconfirmed", "signed_unconfirmed", False))
        self.assertIn("Do NOT start another purchase", r["advice"])
        self.assertEqual(self.e.ledger(), (0, 30000), "still reserved against the cap")
        self.assertEqual((self.e.sent("pwa"), self.e.charged()), (1, 30000))
        self.e.mcp.http = real                                       # the network is back; the agent follows the advice
        res, r2 = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)
        self.assertFalse(res["isError"], r2)
        self.assertTrue(r2.get("resent"))
        self.assertEqual((r2["outcome"], r2["requestId"], r2["hsp"]["decision"]), ("delivered", r["requestId"], "ACCEPT"))
        self.assertEqual((self.e.sent("pwa"), self.e.charged(), self.e.ledger()), (1, 30000, (30000, 0)), "paid once")
        res, r3 = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)
        self.assertEqual((r3.get("resent"), r3.get("redelivery"), r3["outcome"]), (True, True, "delivered"),
                         "right after delivery, a same-url call is a free re-serve")
        self.assertEqual(self.e.sent("pwa"), 1)
        res, r4 = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True, new_purchase=True)
        self.assertFalse(r4.get("resent"), "new_purchase=true is a new purchase")
        self.assertEqual(self.e.sent("pwa"), 2)

    def test_lost_answer_is_resent_not_bought_twice(self):
        self.lost_answer(urllib.error.URLError(TimeoutError("timed out")))

    def test_redirect_after_settlement_is_not_a_refusal(self):
        self.lost_answer(B.Refused("server tried to redirect; refused"))

    def test_malformed_hsp_block_after_payment(self):
        for mutate in (lambda h: h.pop("receiptSignature"), lambda h: [h.pop(k) for k in list(h) if k != "adapter"]):
            self.e = Env()

            def strip(st, hdr, body, mutate=mutate):
                body = copy.deepcopy(body)
                mutate(body["hsp"])
                return st, hdr, body
            self.e.wrap_paid(strip)
            res, r = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)   # was: -32602 "Unknown tool"
            self.assertTrue(res["isError"], "the requested HSP check did not pass")
            self.assertEqual((r["outcome"], r["hsp"]["decision"], r["hspVerified"]), ("delivered", "PERMANENT", False))
            self.assertIn("data", r, "paid data is still handed over")
            self.assertEqual(self.e.ledger(), (30000, 0))
            self.e.close()
        self.e = Env()

    def test_bad_payment_response_header_is_ignored(self):
        self.e = Env()
        self.e.wrap_paid(lambda st, hdr, body: (st, {**hdr, "PAYMENT-RESPONSE": "!!"}, body))
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)          # was: -32602
        self.assertFalse(res["isError"], r)
        self.assertEqual((r["outcome"], self.e.ledger()), ("delivered", (30000, 0)))

    def test_chain_unreadable_during_verification(self):
        self.e = Env(pin=False)
        self.e.mcp.rpc = FailingReceipts(self.e.ch, fails=10**6)
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE)                   # was: "try again"
        self.assertFalse(res["isError"], r)
        self.assertEqual((r["outcome"], r["payment"]), ("delivered", "signed_unconfirmed"))
        self.assertIsNone(r["settlement"]["verifiedOnChain"], "unknown, not false")
        self.assertEqual(self.e.ledger(), (0, 30000), "kept reserved until the reconciler books it")

    def test_malformed_seller_tx_hash_is_not_looked_up(self):
        self.e = Env(pin=False)
        spy = FailingReceipts(self.e.ch, fails=0)
        self.e.mcp.rpc = spy

        def bad_tx(st, hdr, body):
            body = copy.deepcopy(body)
            body["payment"]["tx"] = "0x" + "zz" * 32
            return st, hdr, body
        self.e.wrap_paid(bad_tx)
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE)
        self.assertEqual(spy.receipt_params, [], "no RPC call with a seller-supplied malformed hash")
        self.assertIn("malformed", r["settlement"]["check"])
        self.assertEqual((r["outcome"], self.e.ledger()), ("delivered", (0, 30000)))


class UnknownOutcome(unittest.TestCase):
    """MCP-1 round 2: after signing, any answer without the data that is not a definitive seller refusal is resent,
    never re-bought (variant B: a 200 without data or with a body that is not JSON); the pending resend lives in the
    session ledger, so it survives a server restart (variant C) and a crash while the request is in flight; httpStatus
    is null when the paid request got no answer; the generic advice names the request id and says not to pay again."""

    def setUp(self):
        self.saved = (B.RECEIPT_POLLS, B.RECEIPT_WAIT_S)
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = 3, 0          # no 40 s receipt polling in tests

    def tearDown(self):
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = self.saved
        self.e.close()

    def pay(self):
        return self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)

    def assert_paid_once(self):
        self.assertEqual((self.e.sent("pwa"), self.e.charged(), self.e.ledger()), (1, 30000, (30000, 0)), "paid once")

    def restart(self, http):
        """A new MCP server process: module state starts empty (importlib.reload), only files survive."""
        importlib.reload(B)
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = 3, 0
        self.e.mcp = self.e.server()
        self.e.mcp.http = http

    def no_data_then_retry(self, mutate):
        self.e = Env()
        real = self.e.wrap_paid(mutate)
        res, r = self.pay()
        self.assertTrue(res["isError"])
        self.assertIsInstance(r, dict, "a structured result")
        self.assertFalse(r["delivered"])
        self.assertTrue(r.get("resendOnRetry"), r)
        self.assertIn("Do NOT start another purchase", r["advice"])
        self.assertEqual((self.e.sent("pwa"), self.e.charged()), (1, 30000), "the seller settled and served it")
        self.e.mcp.http = real                                       # the agent follows the advice
        res, r2 = self.pay()
        self.assertFalse(res["isError"], r2)
        self.assertEqual((r2.get("resent"), r2["outcome"], r2["requestId"], r2["hsp"]["decision"]),
                         (True, "delivered", r["requestId"], "ACCEPT"))
        self.assert_paid_once()
        return r

    def test_proxy_page_200_without_data_is_resent(self):
        r = self.no_data_then_retry(lambda st, hdr, body: (200, {}, {"raw": "<html>upstream page</html>"}))
        self.assertEqual((r["outcome"], r["payment"], r["httpStatus"]), ("payment_unconfirmed", "signed_unconfirmed", 200))

    def test_oversized_200_is_resent_and_the_payment_response_tx_is_verified(self):
        def oversized(st, hdr, body):       # what http_get makes of a body over 200 KB: cut JSON -> {"raw": ...}
            return st, hdr, B._json_or_raw(json.dumps({**body, "pad": "x" * 300_000}).encode()[:200_000])
        r = self.no_data_then_retry(oversized)
        self.assertEqual((r["outcome"], r["payment"]), ("paid_not_delivered", "settled"))
        self.assertTrue(r["settlement"]["verifiedOnChain"], "settlement tx from PAYMENT-RESPONSE, verified on-chain")
        self.assertIn("settled on-chain", r["advice"])

    def test_restart_between_unknown_and_retry_pays_once(self):
        self.e = Env()
        real = self.e.wrap_paid(lambda st, hdr, body: (_ for _ in ()).throw(B.Refused("server tried to redirect; refused")))
        res, r = self.pay()
        self.assertEqual((r["outcome"], self.e.sent("pwa")), ("payment_unconfirmed", 1))
        self.restart(real)
        res, r2 = self.pay()
        self.assertFalse(res["isError"], r2)
        self.assertEqual((r2.get("resent"), r2["outcome"], r2["requestId"]), (True, "delivered", r["requestId"]))
        self.assert_paid_once()

    def test_crash_while_the_paid_request_is_in_flight_resends_after_restart(self):
        self.e = Env()
        real = self.e.wrap_paid(lambda st, hdr, body: (_ for _ in ()).throw(KeyboardInterrupt()))   # process dies
        with self.assertRaises(KeyboardInterrupt):
            self.pay()
        self.assertEqual((self.e.sent("pwa"), self.e.charged()), (1, 30000), "the seller settled it")
        self.restart(real)
        res, r2 = self.pay()
        self.assertFalse(res["isError"], r2)
        self.assertEqual((r2.get("resent"), r2["outcome"]), (True, "delivered"))
        self.assertEqual(list(self.e.seller.state.recs), [r2["requestId"]])
        self.assert_paid_once()

    def test_status_lists_pending_resends_without_headers(self):
        self.e = Env()
        real = self.e.wrap_paid(lambda st, hdr, body: (_ for _ in ()).throw(urllib.error.URLError(TimeoutError("timed out"))))
        res, r = self.pay()
        self.restart(real)
        st = self.e.tool("hsk_meter_status")[1]
        self.assertEqual([(x["url"], x["requestId"]) for x in st["pendingResends"]], [(self.e.base + PROFILE, r["requestId"])])
        self.assertNotIn("PAYMENT-SIGNATURE", json.dumps(st))
        self.assertEqual(oct(os.stat(self.e.session).st_mode & 0o777), "0o600", "the ledger holds signed headers")
        self.assertFalse(self.pay()[0]["isError"])
        self.assertEqual(self.e.tool("hsk_meter_status")[1]["pendingResends"], [], "done once delivered")

    def test_lost_answer_reports_no_http_status(self):
        self.e = Env()
        self.e.wrap_paid(lambda st, hdr, body: (_ for _ in ()).throw(urllib.error.URLError(TimeoutError("timed out"))))
        res, r = self.pay()
        self.assertEqual(r["outcome"], "payment_unconfirmed")
        self.assertIsNone(r["httpStatus"], "the paid request got no answer: not the quote's 402")

    def test_definitive_refusal_is_not_resent_and_the_advice_names_the_request(self):
        self.e = Env()
        self.e.wrap_paid(lambda st, hdr, body: (402, {}, {"ok": False, "error": "bad_signature", "message": "no"}))
        res, r = self.pay()
        self.assertTrue(res["isError"])
        self.assertEqual((r["outcome"], r["httpStatus"], r.get("resendOnRetry")), ("payment_unconfirmed", 402, None))
        self.assertIn("do NOT pay again for this URL", r["advice"])
        self.assertIn(r["requestId"], r["advice"], "the full request id")


class GatedInput:
    """stdin for serve(): hands out the messages in order, and line i only once gates[i] is set (10 s at most), so a
    test can deliver notifications/cancelled exactly while a call is running."""

    def __init__(self, msgs, gates=None):
        self.lines = [json.dumps(m).encode() + b"\n" for m in msgs]
        self.gates, self.i = gates or {}, 0

    def readline(self, size=-1):
        if self.i >= len(self.lines):
            return b""
        if self.i in self.gates:
            self.gates[self.i].wait(10)
        self.i += 1
        return self.lines[self.i - 1]


class RetryAfterTimeout(unittest.TestCase):
    """Round 3 (new_variants.py): (1) an MCP client times out or cancels call 1 and the agent calls again with the
    same url: call 1 is re-served or resent, never bought twice (delivered payments are kept for a free re-serve unless
    new_purchase=true; buy() is bounded by a time budget; notifications/cancelled reaches the running call); (2) a
    re-serve that meets an upstream blip (502 "already served; not refunded", Retry-After) keeps the resend."""
    PATH = "/v1/hsk/address-profile"

    def setUp(self):
        self.saved = (B.RECEIPT_POLLS, B.RECEIPT_WAIT_S, getattr(B, "BUY_BUDGET_S", None),
                      getattr(B, "MIN_SIGN_LEFT_S", None))
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = 3, 0          # no 40 s receipt polling in tests
        self.work = S.WORK[self.PATH]

    def tearDown(self):
        S.WORK[self.PATH] = self.work
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = self.saved[:2]
        for name, v in (("BUY_BUDGET_S", self.saved[2]), ("MIN_SIGN_LEFT_S", self.saved[3])):
            if v is not None:
                setattr(B, name, v)
        self.e.close()

    def slow(self, seconds, started=None, done=None):
        """The seller's data source takes `seconds` (after settlement); `started` / `done` are set around it."""
        work = self.work

        def slow_work(rpc, arg):
            if started:
                started.set()
            time.sleep(seconds)
            try:
                return work(rpc, arg)
            finally:
                if done:
                    done.set()
        S.WORK[self.PATH] = slow_work

    def pay(self, path=PROFILE, **kw):
        return self.e.tool("hsk_meter_pay", url=self.e.base + path, hsp=True, **kw)

    def call(self, i, path=PROFILE):
        return {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                "params": {"name": "hsk_meter_pay", "arguments": {"url": self.e.base + path, "hsp": True}}}

    @staticmethod
    def cancel(i):
        return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": i, "reason": "timeout"}}

    def serve(self, fin):
        out = io.BytesIO()
        self.e.mcp.serve(fin, out)
        return [json.loads(x) for x in out.getvalue().splitlines()]

    def assert_paid_once(self):
        self.assertEqual((self.e.sent("pwa"), self.e.charged(), self.e.ledger()), (1, 30000, (30000, 0)), "paid once")

    # ---- variant 1: client timeout / cancel, then the same url again
    def test_delivered_url_is_re_served_unless_new_purchase(self):
        """new_variants part 3 as it ran: serve() finished call 1 (delivered) before it read the cancel."""
        self.e = Env()
        res, r1 = self.pay()
        self.assertFalse(res["isError"], r1)
        self.assertIn("re-serves this same payment for free", r1["advice"])
        self.assertGreater(r1["reServeUntil"], time.time() + B.DELIVERED_KEEP_S - 60)
        res, r2 = self.pay()                                     # the agent calls again: its client gave up on call 1
        self.assert_paid_once()
        self.assertFalse(res["isError"], r2)
        self.assertEqual((r2["outcome"], r2.get("resent"), r2.get("redelivery"), r2["requestId"], r2["hsp"]["decision"]),
                         ("delivered", True, True, r1["requestId"], "ACCEPT"))
        self.assertIn("data", r2)
        self.assertIn("nothing new was signed or charged", r2["note"])
        st = self.e.tool("hsk_meter_status")[1]
        self.assertEqual((st["pendingResends"], [x["requestId"] for x in st["reServable"]]), ([], [r1["requestId"]]))
        self.assertNotIn("PAYMENT-SIGNATURE", json.dumps(st))
        res, r3 = self.pay(new_purchase=True)                    # explicitly a new response
        self.assertFalse(res["isError"], r3)
        self.assertEqual((self.e.sent("pwa"), self.e.charged()), (2, 60000))
        self.assertFalse(r3.get("resent"))
        self.assertNotEqual(r3["requestId"], r1["requestId"])

    def test_cancel_while_running_then_same_url_pays_once(self):
        """The cancel arrives while call 1 waits for the seller: its wait stops, no response is sent for it, and the
        agent's second call resends the same payment (409 in progress -> re-served)."""
        self.e = Env()
        started = threading.Event()
        self.slow(1.5, started)
        outs = self.serve(GatedInput([self.call(1), self.cancel(1), self.call(2)], gates={1: started}))
        self.assert_paid_once()
        self.assertEqual([o.get("id") for o in outs], [2], "no response for the cancelled call")
        r = outs[0]["result"]["structuredContent"]
        self.assertEqual((r["outcome"], r.get("resent"), r["hsp"]["decision"]), ("delivered", True, "ACCEPT"))

    def test_new_variants_part3_replay_pays_once(self):
        """new_variants.py part 3 verbatim: call 1, its cancel and call 2 arrive at once on stdin."""
        self.e = Env()
        self.slow(1.0)
        outs = self.serve(io.BytesIO(b"".join(json.dumps(m).encode() + b"\n"
                                              for m in (self.call(1), self.cancel(1), self.call(2)))))
        self.assert_paid_once()
        self.assertEqual([o.get("id") for o in outs], [2], "no response for the cancelled call")
        self.assertEqual(outs[0]["result"]["structuredContent"]["outcome"], "delivered")

    def test_call_cancelled_while_queued_never_runs(self):
        self.e = Env(cap_units=10**6)
        started = threading.Event()
        self.slow(1.0, started)
        outs = self.serve(GatedInput([self.call(1), self.call(2, profile(2)), self.cancel(2)], gates={2: started}))
        self.assertEqual((self.e.sent("pwa"), [o.get("id") for o in outs]), (1, [1]), "call 2 never ran")

    def test_cancels_ignore_unknown_and_finished_ids(self):
        self.e = Env()
        c = M.Cancels()
        c.cancel(7)                                  # never seen: ignored
        c.queued(7)
        ev = c.start(7)
        self.assertFalse(ev.is_set())
        c.cancel("7")                                # another id (string): not this call
        self.assertFalse(ev.is_set())
        c.cancel(7)
        self.assertTrue(ev.is_set())
        c.finish()
        c.cancel(7)                                  # finished: ignored, a later call with a new id runs
        c.queued(8)
        self.assertIsNotNone(c.start(8))

    def test_pay_returns_within_the_time_budget_and_the_retry_resends(self):
        self.e = Env()
        B.BUY_BUDGET_S, B.MIN_SIGN_LEFT_S = 3, 1
        done = threading.Event()
        self.slow(6, done=done)                      # settled at once, then the seller's data source hangs for 6 s
        t0 = time.time()
        res, r1 = self.pay()
        self.assertLess(time.time() - t0, 4.5, "returned at the budget, not when the seller answered")
        self.assertTrue(res["isError"])
        self.assertEqual((r1["outcome"], r1.get("resendOnRetry")), ("payment_unconfirmed", True))
        self.assertIn("Do NOT start another purchase", r1["advice"])
        self.assertTrue(done.wait(10))
        S.WORK[self.PATH] = self.work
        res, r2 = self.pay()
        self.assertFalse(res["isError"], r2)
        self.assertEqual((r2.get("resent"), r2["requestId"]), (True, r1["requestId"]))
        self.assert_paid_once()

    def test_still_settling_retries_stop_at_the_budget(self):
        self.e = Env()
        B.BUY_BUDGET_S, B.MIN_SIGN_LEFT_S = 4, 1
        real, timeouts = self.e.mcp.http, []

        def settling(url, headers, timeout=120):     # the seller keeps answering "still settling"
            if "PAYMENT-SIGNATURE" in headers:
                timeouts.append(timeout)
                return 503, {"Retry-After": "3"}, {"ok": False, "error": "settlement_pending", "retryable": True}
            return real(url, headers, timeout)
        self.e.mcp.http = settling
        t0 = time.time()
        res, r1 = self.pay()
        self.assertLess(time.time() - t0, 5.5, "8 x Retry-After 3 s would block for 24 s")
        self.assertEqual((r1["outcome"], r1.get("resendOnRetry"), r1.get("retryAfterS")), ("payment_unconfirmed", True, 3))
        self.assertIn("after 3 s", r1["advice"])
        self.assertTrue(timeouts and max(timeouts) <= 4, timeouts)
        self.e.mcp.http = real
        res, r2 = self.pay()
        self.assertEqual((res["isError"], r2.get("resent"), r2["outcome"]), (False, True, "delivered"))
        self.assert_paid_once()

    def test_new_purchase_never_skips_an_unconfirmed_payment(self):
        self.e = Env()
        real = self.e.wrap_paid(lambda st, hdr, body: (_ for _ in ()).throw(urllib.error.URLError(TimeoutError("timed out"))))
        res, r1 = self.pay()
        self.e.mcp.http = real
        res, r2 = self.pay(new_purchase=True)
        self.assertIsInstance(r2, dict, r2)
        self.assertFalse(res["isError"], r2)
        self.assertEqual((r2.get("resent"), r2["requestId"]), (True, r1["requestId"]))
        self.assertIn("no final outcome", r2["newPurchaseIgnored"])
        self.assert_paid_once()

    def test_re_serve_limit_then_a_new_purchase(self):
        self.e = Env()
        rid = self.pay()[1]["requestId"]
        for _ in range(2):                           # the seller serves a payment 3 times within 1 h
            res, r = self.pay()
            self.assertEqual((res["isError"], r.get("redelivery")), (False, True))
        self.assert_paid_once()
        res, r4 = self.pay()                         # 429 already_served: nothing new is charged, the entry is done
        self.assertTrue(res["isError"])
        self.assertEqual((r4.get("redelivery"), r4.get("resendOnRetry"), r4["requestId"]), (True, None, rid))
        self.assertIn("will not serve it again", r4["advice"])
        self.assert_paid_once()
        res, r5 = self.pay()
        self.assertEqual((res["isError"], r5.get("resent"), self.e.sent("pwa")), (False, None, 2))

    def test_new_purchase_is_in_the_schema_and_must_be_a_boolean(self):
        self.e = Env()
        tool = next(t for t in self.e.rpc("tools/list")["result"]["tools"] if t["name"] == "hsk_meter_pay")
        self.assertEqual(tool["inputSchema"]["properties"]["new_purchase"]["type"], "boolean")
        self.assertIn("never pays twice", tool["description"])
        self.assertIn("re-served", tool["description"])
        res, text = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, new_purchase="yes")
        self.assertTrue(res["isError"])
        self.assertIn("new_purchase must be true or false", text)
        self.assertEqual(self.e.ch.sent, [])

    # ---- variant 2: a re-serve meets a transient upstream failure
    def test_already_served_blip_keeps_the_resend(self):
        """new_variants part 2: answer 1 lost (504); on the resend the seller's data source fails once -> 502 'this
        payment was already served; it is not refunded' (Retry-After 5). The entry stays; the retry resends."""
        self.e = Env()
        calls = {"n": 0}
        work = self.work

        def flaky(rpc, arg):
            calls["n"] += 1
            if calls["n"] == 2:
                raise C.RpcError("RPC unavailable: <urlopen error timed out>")
            return work(rpc, arg)
        S.WORK[self.PATH] = flaky
        real = self.e.wrap_paid(lambda st, hdr, body: (504, {}, {"raw": "<html>504 Gateway Time-out</html>"}))
        res, r1 = self.pay()
        self.assertTrue(r1.get("resendOnRetry"), r1)
        self.e.mcp.http = real
        res, r2 = self.pay()
        self.assertTrue(res["isError"])
        self.assertEqual((r2["outcome"], r2["refund"]["status"], r2.get("resendOnRetry"), r2.get("retryAfterS")),
                         ("paid_not_delivered", "none", True, 5))
        self.assertNotIn("new payment", r2["advice"].lower())
        self.assertIn("Do NOT start another purchase", r2["advice"])
        self.assertIn("after 5 s", r2["advice"])
        st = self.e.tool("hsk_meter_status")[1]
        self.assertEqual([x["requestId"] for x in st["pendingResends"]], [r1["requestId"]])
        res, r3 = self.pay()
        self.assertFalse(res["isError"], r3)
        self.assertEqual((r3.get("resent"), r3["outcome"], r3["requestId"]), (True, "delivered", r1["requestId"]))
        self.assert_paid_once()


class HspBinding(unittest.TestCase):
    """MCP-2: the HSP receipt must be for OUR mandate and the settlement we verified; a missing one is visible."""

    def tearDown(self):
        self.e.close()

    def test_receipt_of_another_payment_is_rejected(self):
        self.e = Env(cap_units=10**6)
        saved = {}

        def replay(st, hdr, body):
            if "first" not in saved:
                saved["first"] = copy.deepcopy(body["hsp"])
                return st, hdr, body
            return st, hdr, {**body, "hsp": saved["first"]}            # payment #1's mandate + receipt on payment #2
        self.e.wrap_paid(replay)
        res1, r1 = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)
        self.assertEqual(r1["hsp"]["decision"], "ACCEPT")
        res2, r2 = self.e.tool("hsk_meter_pay", url=self.e.base + profile(2), hsp=True)
        self.assertTrue(res2["isError"])
        self.assertEqual((r2["hsp"]["decision"], r2["hspVerified"]), ("PERMANENT", False))
        self.assertTrue(any("another mandate" in x for x in r2["hsp"]["reasons"]), r2["hsp"])
        self.assertIn("Do not pay again", r2["advice"])

    def test_missing_receipt_is_visible(self):
        self.e = Env()
        self.e.wrap_paid(lambda st, hdr, body: (st, hdr, {k: v for k, v in body.items() if k != "hsp"}))
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)
        self.assertTrue(res["isError"])
        self.assertEqual((r["hsp"]["decision"], r["hspVerified"], r["outcome"]), ("MISSING", False, "delivered"))

    def test_fresh_receipt_for_another_tx_is_rejected(self):
        self.e = Env()
        adapter = self.e.adapter

        def resign(st, hdr, body):          # a seller holding the adapter key signs a receipt for OUR mandate, wrong tx
            body = copy.deepcopy(body)
            j = body["hsp"]["receipt"]
            j["txHash"] = "0x" + "77" * 32
            body["hsp"]["receiptSignature"] = HSP.sign_receipt(adapter, HSP.receipt_from_json(j), P.CHAIN_ID)
            return st, hdr, body
        self.e.wrap_paid(resign)
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)
        self.assertEqual((r["hsp"]["decision"], res["isError"]), ("PERMANENT", True))
        self.assertTrue(any("another settlement transaction" in x for x in r["hsp"]["reasons"]), r["hsp"])

    def test_receipt_from_a_non_pinned_adapter_is_an_error(self):
        self.e = Env()
        self.e.mcp.pin_adapter = Account.create().address
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=True)
        self.assertTrue(res["isError"], "a requested HSP check that did not ACCEPT is an error")
        self.assertEqual((r["hsp"]["decision"], r["hspVerified"]), ("PERMANENT", False))

    def test_operator_can_require_hsp(self):
        self.e = Env(require_hsp=True)
        res, text = self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE, hsp=False)
        self.assertTrue(res["isError"])
        self.assertIn("requires HSP", text)
        self.assertEqual(self.e.ch.sent, [])


class Refunds(unittest.TestCase):
    """MCP-3 / B2B-2: a paid but undelivered request is an error with the refund state, and a refund that lands
    later gives the cap back."""

    def tearDown(self):
        self.e.close()

    def test_queued_refund_is_reported_and_credited_back_later(self):
        self.e = Env(smoke=DEAD, pin=False)
        self.e.seller.cfg["refunds_per_payer_day"] = 0              # the payer's daily refund quota is used up
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + "/v1/hsk/address-profile?address=" + DEAD)
        self.assertTrue(res["isError"], r)
        self.assertEqual((r["outcome"], r["delivered"], r["payment"]), ("paid_not_delivered", False, "settled"))
        self.assertEqual(r["refund"]["status"], "queued")
        self.assertIn("untrusted", r["sellerErrorNote"])
        self.assertEqual(r["sellerError"]["error"], "not_delivered")
        self.assertIn("NEW payment", r["advice"])
        self.assertEqual(r["sessionSpentUnits"], 30000)
        self.e.seller.cfg["refunds_per_payer_day"] = 2              # next UTC day: the seller's reconciler refunds it
        self.e.seller.reconcile_once()
        self.assertEqual(self.e.charged(), 0, "refunded on-chain")
        st = self.e.tool("hsk_meter_status")[1]
        self.assertEqual((st["spentUnits"], st["remainingUnits"]), (0, 200_000), "the cap is given back")

    def test_seller_text_is_capped(self):
        self.e = Env(smoke=DEAD, pin=False)

        def inflate(st, hdr, body):
            body = copy.deepcopy(body)
            body["message"] = "m" * 100_000
            body["refund"]["note"] = "n" * 100_000
            return st, hdr, body
        self.e.wrap_paid(inflate)
        res, r = self.e.tool("hsk_meter_pay", url=self.e.base + "/v1/hsk/address-profile?address=" + DEAD)
        self.assertLess(len(json.dumps(r)), 10_000, "seller text is capped")
        self.assertEqual(r["refund"]["status"], "verified")
        self.assertIn("untrusted", r["refund"]["noteIs"])


class SharedLimits(unittest.TestCase):
    """MCP-4: the payments-per-minute limit lives in the shared ledger (a 2nd server or a restart shares it)."""

    def tearDown(self):
        self.e.close()

    def test_pays_per_minute_is_shared_by_servers_on_one_ledger(self):
        self.e = Env(pays_per_minute=1, cap_units=10**6, pin=False)
        self.assertFalse(self.e.tool("hsk_meter_pay", url=self.e.base + profile(1))[0]["isError"])
        for i in (2, 3):                                 # a second process, then a restarted first one
            other = self.e.server(pays_per_minute=1, cap_units=10**6, pin=False)
            r = other.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                              "params": {"name": "hsk_meter_pay", "arguments": {"url": self.e.base + profile(i)}}})
            self.assertTrue(r["result"]["isError"])
            self.assertIn("rate limit", r["result"]["content"][0]["text"])
        self.assertEqual(self.e.sent("pwa"), 1)


class Journal(unittest.TestCase):
    """MCP-6 / B2B-3: test runs journal into their own dir, never into ops/state/*.jsonl."""

    def tearDown(self):
        self.e.close()

    def test_runs_go_to_the_configured_journal_only(self):
        live = [os.path.join(ROOT, "ops", "state", f) for f in ("buyer_runs.jsonl", "mcp_runs.jsonl")]
        before = [os.path.getsize(f) if os.path.exists(f) else None for f in live]
        self.e = Env(pin=False)
        self.assertFalse(self.e.tool("hsk_meter_pay", url=self.e.base + PROFILE)[0]["isError"])
        self.assertEqual([os.path.getsize(f) if os.path.exists(f) else None for f in live], before)
        with open(self.e.journal) as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual([(x["payer"], x["payment"]) for x in rows], [(self.e.me.address, "settled")])


if __name__ == "__main__":
    unittest.main()
