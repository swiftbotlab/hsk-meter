#!/usr/bin/env python3
"""Offline tests of the paid AI endpoint (GET /v1/ai/ask, answered through Otari) and of the Otari client.

The seller runs against testkit.FakeChain (real signed transactions, the contract's rules) and a scripted stand-in for
the Otari gateway: no network. The HTTP client in server/otari.py is tested against a local loopback server that
answers the way otari.ai does (headers, error codes), so header parsing and error mapping are covered too.

  test_ai.py [-v]
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "agent"))
import ai_ask as AI      # noqa: E402
import otari as OT       # noqa: E402
import protocol as P     # noqa: E402
import seller as S       # noqa: E402
import testkit as T      # noqa: E402
from eth_account import Account  # noqa: E402

CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"
AI_EID = 4
PRICE = 30000
WHO = "0x" + "ab" * 20
Q_TEXT = f"what is the usdc.e balance of {WHO} on hashkey chain?"
Q = "q=" + urllib.parse.quote_plus(Q_TEXT)


class FakeOtari:
    """Scripted gateway: answers by policy name, records every call (which key, model, idempotency key, tools)."""

    def __init__(self):
        self.calls = []
        self.guard = "Safety: Safe\nCategories: None"
        self.route = {"in_scope": True, "needs_web": False, "needs_math": False}
        self.answer = f"{WHO} holds 0.5 USDC.e."
        self.verify = {"verdict": "pass", "answer": "", "issues": []}
        self.fail = {}            # policy -> OtariError to raise
        self.fallback = {}        # policy -> (provider, attempts)
        self.host, self.port = "otari.ai", 443

    def chat(self, key, body, idempotency_key=None, timeout=None):
        pol = body["model"]
        self.calls.append({"key": key, "model": pol, "idem": idempotency_key, "body": body})
        if pol in self.fail:
            raise self.fail[pol]
        content = {"hsk-guard": lambda: self.guard, "hsk-route": lambda: json.dumps(self.route),
                   "hsk-answer": lambda: self.answer, "hsk-answer-drill": lambda: self.answer,
                   "hsk-verify": lambda: json.dumps(self.verify)}[pol]()
        prov, att = self.fallback.get(pol, ("deepinfra", 1))
        tools = []
        if any(t.get("type") == "otari_web_search" for t in body.get("tools") or []):
            tools.append({"type": "web_search", "status": "completed", "query": "x", "sources": ["https://docs.hskchain.net/x"]})
        return {"content": content, "model": pol, "provider": prov, "servedModel": "m-" + pol, "fallback": att > 1,
                "attempts": att, "requestId": f"req-{len(self.calls)}", "costUsd": 0.0001, "usage": {},
                "toolCalls": tools, "ms": 5}

    def models(self):
        return [c["model"] for c in self.calls]


class Env:
    def __init__(self, dedicated=True, answer_policy="hsk-answer"):
        self.provider, self.adapter, self.buyer = Account.create(), Account.create(), Account.create()
        self.payee = "0x" + "aa" * 20
        self.ch = T.FakeChain(CONTRACT, self.provider.address, self.payee, {1: 30000, 2: 25000, AI_EID: PRICE})
        self.ch.fund(self.buyer.address, 10**7)
        self.otari = FakeOtari()
        buyers = {self.buyer.address.lower(): "KEY-BUYER-0123456789"} if dedicated else {}
        self.keys = OT.KeyRing(screen="KEY-SCREEN-0123456789", pool="KEY-POOL-0123456789", buyers=buyers)
        self.ai = AI.AiAsk(self.otari, self.keys, profile=S.address_profile, receipt=S.tx_receipt,
                           not_found_exc=S.NotFound, chain_error=S.C.RpcError, mcp_server_ids=["mcp-blockscout"],
                           answer_policy=answer_policy)
        S.install_ai(self.ai, AI_EID)
        self.s = S.Seller({"contract": CONTRACT, "state_dir": None, "tx_wait_s": 0.3}, rpc=self.ch,
                          provider=self.provider, adapter=self.adapter, token_key=os.urandom(32))

    def quote(self, q=Q, payer=None):
        return self.s.handle_paid(AI.AI_PATH, q, {"X-Payer": (payer or self.buyer).address}, "1.1.1.1", {})

    def header(self, body):
        acc = body["accepts"][0]
        x = acc["extra"]
        vb = int(time.time()) + 300
        sig = T.sign_auth(self.buyer, acc["payTo"], int(acc["amount"]), 0, vb, x["nonce"])
        return P.b64json({"x402Version": 2, "accepted": acc, "payload": {"signature": sig, "authorization": {
            "from": self.buyer.address, "to": acc["payTo"], "value": acc["amount"], "validAfter": "0",
            "validBefore": str(vb), "nonce": x["nonce"]}}})

    def buy(self, q=Q):
        st, body, _ = self.quote(q)
        assert st == 402, (st, body)
        hdr = self.header(body)
        return hdr, body, self.s.handle_paid(AI.AI_PATH, q, {"PAYMENT-SIGNATURE": hdr}, "1.1.1.1", {})

    def refunds_sent(self):
        return self.ch.sent.count(T.S_REFUND)


def tearDownModule():
    S.RESOURCES.pop(AI.AI_PATH, None)
    S.WORK.pop(AI.AI_PATH, None)
    S.PRECHECK.pop(AI.AI_PATH, None)


class Screening(unittest.TestCase):
    def test_quote_runs_guard_and_router_on_the_screening_key_only(self):
        e = Env()
        st, body, hdr = e.quote()
        self.assertEqual(st, 402, body)
        self.assertEqual(e.otari.models(), ["hsk-guard", "hsk-route"])
        self.assertTrue(all(c["key"] == "KEY-SCREEN-0123456789" for c in e.otari.calls))
        self.assertEqual(int(body["accepts"][0]["extra"]["endpointId"]), AI_EID)
        self.assertEqual(body["accepts"][0]["amount"], str(PRICE))

    def test_screen_is_cached_per_question(self):
        e = Env()
        e.quote()
        e.quote()
        self.assertEqual(e.otari.models(), ["hsk-guard", "hsk-route"])

    def test_unsafe_question_is_never_quoted(self):
        e = Env()
        e.otari.guard = "Safety: Unsafe\nCategories: Violent"
        st, body, _ = e.quote()
        self.assertEqual((st, body["error"]), (422, "question_refused"))
        self.assertIn("nothing was charged", body["message"])
        self.assertNotIn("accepts", body)
        self.assertEqual(e.otari.models(), ["hsk-guard"])       # the router is not even asked
        self.assertEqual(e.ch.sent, [])

    def test_out_of_scope_question_is_never_quoted(self):
        e = Env()
        e.otari.route = {"in_scope": False, "needs_web": False, "needs_math": False}
        st, body, _ = e.quote("q=" + urllib.parse.quote_plus("write me a poem about the sea please"))
        self.assertEqual((st, body["error"]), (422, "out_of_scope"))

    def test_bad_question_text_refused_before_any_model_call(self):
        e = Env()
        for q in ("q=hi", "q=" + urllib.parse.quote_plus("x" * 241), "q=" + urllib.parse.quote_plus("tell me {secret}"),
                  "q=" + urllib.parse.quote_plus("line\nbreak in question")):
            st, body, _ = e.quote(q)
            self.assertEqual(st, 400, (q, body))
        self.assertEqual(e.otari.calls, [])

    def test_screening_outage_is_retryable_and_not_quoted(self):
        e = Env()
        e.otari.fail["hsk-guard"] = OT.OtariError("unavailable", 502)
        st, body, hdr = e.quote()
        self.assertEqual((st, body["error"], body["retryable"]), (503, "screening_unavailable", True))

    def test_resource_is_the_raw_query_the_buyer_computes(self):
        e = Env()
        st, body, _ = e.quote()
        self.assertEqual(body["accepts"][0]["extra"]["resource"], f"GET {AI.AI_PATH}?{Q.lower()}")
        # a different encoding of the same question is simply a different resource (and request id)
        st2, body2, _ = e.quote("q=" + urllib.parse.quote(Q_TEXT))
        self.assertEqual(st2, 402)
        self.assertNotEqual(body["accepts"][0]["extra"]["requestId"], body2["accepts"][0]["extra"]["requestId"])


class PaidAnswers(unittest.TestCase):
    def test_paid_question_is_answered_through_otari_with_the_buyers_key(self):
        e = Env()
        hdr, quote, (st, body, h) = e.buy()
        self.assertEqual(st, 200, body)
        d = body["data"]
        self.assertEqual(d["answer"], e.otari.answer)
        self.assertEqual(d["verdict"], "pass")
        self.assertEqual([c["role"] for c in d["otari"]["calls"]], ["guard", "route", "answer", "verify"])
        rid = quote["accepts"][0]["extra"]["requestId"]
        paid = [c for c in e.otari.calls if c["model"] in ("hsk-answer", "hsk-verify")]
        self.assertTrue(all(c["key"] == "KEY-BUYER-0123456789" for c in paid))
        self.assertEqual([c["idem"] for c in paid], [f"{rid}:answer", f"{rid}:verify"])
        ans = paid[0]["body"]
        self.assertEqual(ans["mcp_server_ids"], ["mcp-blockscout"])
        self.assertNotIn("tools", ans)                           # router said no paid tools
        self.assertEqual(ans["metadata"]["buyer"], e.buyer.address.lower())
        self.assertEqual(ans["metadata"]["rid"], rid[:18])
        self.assertIn("address:" + WHO, d["facts"])               # the seller read the address itself
        self.assertIn(WHO, ans["messages"][1]["content"])
        self.assertEqual(d["otari"]["tier"], "dedicated")
        self.assertEqual(e.refunds_sent(), 0)
        self.assertEqual(e.ch.payments[rid]["refunded"] if rid in e.ch.payments else False, False)

    def test_buyer_without_own_key_uses_the_pool_key(self):
        e = Env(dedicated=False)
        _, _, (st, body, _) = e.buy()
        self.assertEqual(st, 200, body)
        paid = [c for c in e.otari.calls if c["model"] in ("hsk-answer", "hsk-verify")]
        self.assertTrue(all(c["key"] == "KEY-POOL-0123456789" for c in paid))
        self.assertEqual(body["data"]["otari"]["tier"], "pool")

    def test_router_decides_paid_tools(self):
        e = Env()
        e.otari.route = {"in_scope": True, "needs_web": True, "needs_math": True}
        _, _, (st, body, _) = e.buy()
        self.assertEqual(st, 200, body)
        ans = next(c for c in e.otari.calls if c["model"] == "hsk-answer")["body"]
        self.assertEqual([t["type"] for t in ans["tools"]], ["otari_web_search", "otari_code_execution"])
        self.assertEqual(body["data"]["sources"], ["https://docs.hskchain.net/x"])

    def test_fallback_and_cost_are_reported_to_the_buyer(self):
        e = Env()
        e.otari.fallback["hsk-answer"] = ("nebius", 2)
        _, _, (st, body, _) = e.buy()
        a = next(c for c in body["data"]["otari"]["calls"] if c["role"] == "answer")
        self.assertEqual((a["provider"], a["attempts"], a["fallback"]), ("nebius", 2, True))
        self.assertAlmostEqual(body["data"]["otari"]["buyerCostUsd"], 0.0002)

    def test_verifier_fix_replaces_the_draft(self):
        e = Env()
        e.otari.answer = f"{WHO} holds 500000 USDC.e."
        e.otari.verify = {"verdict": "fixed", "answer": f"{WHO} holds 0.5 USDC.e.", "issues": ["decimals"]}
        _, _, (st, body, _) = e.buy()
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["answer"], f"{WHO} holds 0.5 USDC.e.")
        self.assertEqual(body["data"]["draft"], f"{WHO} holds 500000 USDC.e.")

    def test_answer_is_plain_text_without_hidden_characters(self):
        e = Env()
        e.otari.answer = f"{WHO} holds **0.5**\u202fUSDC.e `now`\u200b\u202e."
        _, _, (st, body, _) = e.buy()
        self.assertEqual(body["data"]["answer"], f"{WHO} holds 0.5 USDC.e now.")

    def test_reserve_of_a_paid_request_makes_no_new_model_calls(self):
        e = Env()
        hdr, _, (st, body, _) = e.buy()
        n = len(e.otari.calls)
        st2, body2, _ = e.s.handle_paid(AI.AI_PATH, Q, {"PAYMENT-SIGNATURE": hdr}, "1.1.1.1", {})
        self.assertEqual(st2, 200)
        self.assertEqual(body2["data"]["answer"], body["data"]["answer"])
        self.assertTrue(body2["data"]["reserved"])
        self.assertEqual(len(e.otari.calls), n)


class Refunds(unittest.TestCase):
    def _refunded(self, e, quote, body, status, reason):
        self.assertEqual(body["status"] if "status" in body else status, status)
        rid = quote["accepts"][0]["extra"]["requestId"]
        self.assertEqual(e.refunds_sent(), 1)
        self.assertTrue(e.ch.payments[rid]["refunded"])
        self.assertTrue(body["refund"]["refunded"])
        self.assertEqual(body["reason"], reason)

    def test_budget_exceeded_is_refunded_on_chain_and_not_quoted_again_today(self):
        e = Env()
        e.otari.fail["hsk-answer"] = OT.OtariError("budget", 403, "budget_exceeded", "API key has exceeded request limit")
        before = e.ch.usdce[e.buyer.address.lower()]
        _, quote, (st, body, _) = e.buy()
        self.assertEqual(st, 429, body)
        self._refunded(e, quote, body, 429, "ai_budget_exhausted")
        self.assertIn("spend ceiling", body["message"])
        self.assertEqual(e.ch.usdce[e.buyer.address.lower()], before)          # every unit back
        n = len(e.otari.calls)
        st2, body2, _ = e.quote()                                             # same day: refused, nothing charged
        self.assertEqual((st2, body2["error"]), (429, "ai_budget_exhausted"))
        self.assertEqual(len(e.otari.calls), n)

    def test_budget_on_the_verify_step_also_refunds(self):
        e = Env()
        e.otari.fail["hsk-verify"] = OT.OtariError("budget", 403, "budget_exceeded")
        _, quote, (st, body, _) = e.buy()
        self._refunded(e, quote, body, 429, "ai_budget_exhausted")

    def test_gateway_outage_after_payment_is_refunded(self):
        for kind, reason in (("unavailable", "ai_unavailable"), ("rate_limited", "ai_rate_limited"),
                             ("bad_response", "ai_unavailable"), ("guardrail", "ai_guardrail_blocked")):
            e = Env()
            e.otari.fail["hsk-answer"] = OT.OtariError(kind, 502)
            _, quote, (st, body, _) = e.buy()
            self.assertNotEqual(st, 200)
            self._refunded(e, quote, body, st, reason)

    def test_answer_that_fails_verification_is_refunded_not_sold(self):
        e = Env()
        e.otari.verify = {"verdict": "fail", "answer": "", "issues": ["balance contradicts FACTS"]}
        _, quote, (st, body, _) = e.buy()
        self.assertEqual(st, 422)
        self._refunded(e, quote, body, 422, "ai_unverified")
        self.assertIn("contradicted", body["message"])

    def test_empty_answer_is_refunded(self):
        e = Env()
        e.otari.answer = "   "
        _, quote, (st, body, _) = e.buy()
        self._refunded(e, quote, body, st, "ai_empty_answer")

    def test_fault_drill_policy_is_used_when_configured(self):
        e = Env(answer_policy="hsk-answer-drill")
        e.otari.fallback["hsk-answer-drill"] = ("deepinfra", 2)
        _, _, (st, body, _) = e.buy()
        a = next(c for c in body["data"]["otari"]["calls"] if c["role"] == "answer")
        self.assertEqual((a["policy"], a["fallback"]), ("hsk-answer-drill", True))


# ---------------------------------------------------------------------------- the HTTP client against loopback
class _Gateway(http.server.BaseHTTPRequestHandler):
    script = {}
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n))
        _Gateway.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                              "idem": self.headers.get("Idempotency-Key"), "body": body})
        status, hdrs, payload = _Gateway.script[body["model"]]
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/api/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _Gateway.seen.clear()
        ok = {"choices": [{"message": {"content": "hello"}}], "model": "hsk-answer",
              "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost_usd": "0.000123"},
              "otari_tool_calls": [{"type": "web_search", "status": "completed", "query": "q",
                                    "sources": [{"url": "https://docs.hskchain.net/a", "title": "t"}]}]}
        _Gateway.script = {
            "ok": (200, {"Otari-Provider": "nebius", "Otari-Model": "openai/gpt-oss-120b", "Otari-Fallback": "true",
                         "Otari-Attempt-Count": "2", "Otari-Request-ID": "r-1"}, ok),
            "budget": (403, {"Otari-Error-Code": "budget_exceeded"},
                       {"detail": "API key has exceeded request limit", "code": "budget_exceeded"}),
            "busy": (429, {"Otari-Error-Code": "upstream_rate_limited", "Retry-After": "3"}, {"detail": "Model busy"}),
            "redirect": (302, {"Location": "http://127.0.0.1:1/evil"}, {}),
            "garbage": (200, {}, b"<html>not json</html>"),
            "huge": (200, {}, b"{" + b" " * 2_000_100 + b"}"),
            "unknown": (400, {"Otari-Error-Code": "invalid_model"}, {"detail": "Unknown model", "code": "invalid_model"}),
        }
        self.c = OT.Otari(base=self.base, allow_local=True, timeout=5)

    def test_success_parses_routing_headers_cost_and_tool_reports(self):
        r = self.c.chat("KEY-0123456789abcdef", {"model": "ok", "messages": []}, idempotency_key="0xrid:answer")
        self.assertEqual((r["provider"], r["servedModel"], r["fallback"], r["attempts"]), ("nebius", "openai/gpt-oss-120b", True, 2))
        self.assertAlmostEqual(r["costUsd"], 0.000123)
        self.assertEqual(r["toolCalls"][0]["sources"], ["https://docs.hskchain.net/a"])
        s = _Gateway.seen[-1]
        self.assertEqual((s["path"], s["auth"], s["idem"]), ("/api/v1/chat/completions", "Bearer KEY-0123456789abcdef", "0xrid:answer"))

    def test_errors_are_typed_and_never_carry_the_key(self):
        for model, kind in (("budget", "budget"), ("busy", "rate_limited"), ("unknown", "rejected"),
                            ("redirect", "unavailable"), ("garbage", "bad_response"), ("huge", "bad_response")):
            with self.assertRaises(OT.OtariError) as cm:
                self.c.chat("KEY-0123456789abcdef", {"model": model, "messages": []})
            self.assertEqual(cm.exception.kind, kind, model)
            self.assertNotIn("KEY-0123456789abcdef", str(cm.exception))
        self.assertEqual([s["body"]["model"] for s in _Gateway.seen].count("redirect"), 1)   # never followed

    def test_unreachable_gateway_is_unavailable(self):
        c = OT.Otari(base="http://127.0.0.1:9/api/v1", allow_local=True, timeout=2)
        with self.assertRaises(OT.OtariError) as cm:
            c.chat("KEY-0123456789abcdef", {"model": "ok", "messages": []})
        self.assertEqual(cm.exception.kind, "unavailable")

    def test_base_url_allowlist(self):
        for bad in ("https://otari.ai.evil.example/api/v1", "http://otari.ai/api/v1", "https://user@otari.ai/api/v1",
                    "https://otari.ai/v1", "http://127.0.0.1:8000/api/v1", "https://otari.ai:8443/api/v1"):
            with self.assertRaises(ValueError, msg=bad):
                OT.Otari(base=bad)
        OT.Otari(base="https://otari.ai/api/v1")
        OT.Otari(base="https://api.otari.ai/api/v1")


class Keys(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "buyers"))

    def tearDown(self):
        shutil.rmtree(self.d)

    def put(self, rel, text, mode=0o600):
        p = os.path.join(self.d, rel)
        with open(p, "w") as f:
            f.write(text)
        os.chmod(p, mode)
        return p

    def test_keyring_loads_one_variable_files_and_picks_the_buyers_key(self):
        self.put("screen.env", "OTARI_API_KEY=otk_screen_0123456789\n")
        self.put("pool.env", "OTARI_API_KEY=otk_pool_0123456789\n")
        self.put("buyers/" + WHO + ".env", "OTARI_API_KEY=otk_buyer_0123456789\n")
        self.put("buyers/not-an-address.env", "OTARI_API_KEY=otk_other_0123456789\n")
        kr = OT.KeyRing(self.d)
        self.assertEqual(kr.for_buyer(WHO.upper().replace("0X", "0x")), ("otk_buyer_0123456789", "dedicated"))
        self.assertEqual(kr.for_buyer("0x" + "cd" * 20), ("otk_pool_0123456789", "pool"))
        self.assertNotIn("otk_", repr(kr))

    def test_bad_key_files_are_refused(self):
        self.put("pool.env", "OTARI_API_KEY=otk_pool_0123456789\n")
        for text, mode in (("OTARI_API_KEY=otk_screen_0123456789\n", 0o644),
                           ("OTARI_API_KEY=otk_screen_0123456789\nOTHER=1\n", 0o600),
                           ("OTARI_API_KEY=short\n", 0o600), ("", 0o600)):
            self.put("screen.env", text, mode)
            with self.assertRaises(OT.KeyFileError):
                OT.KeyRing(self.d)
        os.remove(os.path.join(self.d, "screen.env"))
        target = self.put("real.env", "OTARI_API_KEY=otk_screen_0123456789\n")
        os.symlink(target, os.path.join(self.d, "screen.env"))
        with self.assertRaises(OT.KeyFileError):
            OT.KeyRing(self.d)


if __name__ == "__main__":
    unittest.main()
