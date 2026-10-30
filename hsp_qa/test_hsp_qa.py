"""Offline tests for hsp_qa (no network: everything runs against the local mock or in memory).

Run: ./run_tests.sh   (or wallet-venv python -I test_hsp_qa.py)
"""
import atexit
import contextlib
import shutil
import copy
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from decimal import Decimal

TMP = tempfile.mkdtemp(prefix="hsp_qa_test_")
atexit.register(shutil.rmtree, TMP, True)     # test keys are ephemeral; remove them
os.environ["HSP_QA_SECRETS_DIR"] = os.path.join(TMP, "secrets")
os.environ["HSP_QA_STATE_DIR"] = os.path.join(TMP, "state")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config  # noqa: E402

config.PROBE_LOG = os.path.join(TMP, "probe.log")
config.RECEIPTS_LIVE = os.path.join(TMP, "receipts.live.jsonl")
config.RECEIPTS_MOCK = os.path.join(TMP, "receipts.mock.jsonl")
config.LIVE_TOOLS_FILE = os.path.join(TMP, "live_tools.json")
config.LIVE_CHAIN_CONFIG_FILE = os.path.join(TMP, "live_chain_config.json")

import firewall  # noqa: E402
import flow  # noqa: E402
import mcp_client  # noqa: E402
import mock_server  # noqa: E402
import pay  # noqa: E402
import wallets  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402

ATTACKER = "0x000000000000000000000000000000000000dEaD"
USDC133 = firewall.TokenRow("hashkey-testnet", 133, "USDC", "0x8FE3cB719Ee4410E236Cd6b72ab1fCDC06eF53c6", 6, "eip3009",
                            "USD Coin", "2")
USDT133 = firewall.TokenRow("hashkey-testnet", 133, "USDT", "0x372325443233fEbaC1F6998aC750276468c83CC6", 6, "permit2",
                            spenders=(mock_server.MOCK_PERMIT2_SPENDER,))


def new_acct():
    return Account.from_key(os.urandom(32))


class MockEnv:
    """A mock server + Hsp + Ctx wired together."""

    def __init__(self, **state_kw):
        self.st = mock_server.MockState()
        for k, v in state_kw.items():
            setattr(self.st, k, v)
        self.srv, _, self.url = mock_server.make_server(0, self.st)
        self.mcp = mcp_client.McpClient(self.url, allow_loopback=True, total_timeout=10)
        self.hsp = flow.Hsp(self.mcp)
        self.hsp.discover()
        rows, _ = flow.parse_chain_config(self.hsp.call("get_chain_config"))
        self.rows = rows
        self.payer = new_acct()
        self.pay_to = new_acct().address
        fd, self.rpath = tempfile.mkstemp(dir=TMP, suffix=".jsonl")
        os.close(fd)
        self.receipts = flow.Receipts(self.rpath, "mock")
        self.out = []
        self.ctx = flow.Ctx(hsp=self.hsp, account=self.payer, pay_to=self.pay_to, receipts=self.receipts,
                            approve=lambda stage, s: True, balance_fn=lambda c, t, o: 10 ** 12,
                            allowance_fn=lambda c, t, o, s: 10 ** 12, sleep=lambda s: None,
                            poll_interval=0, out=self.out.append)

    def calls(self, name):
        return [a for n, a in self.st.calls if n == name]

    def steps(self):
        return [r["step"] for r in self.receipts.rows()]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# =============================================================== firewall
class FirewallBase(unittest.TestCase):
    """Builds a VALID prepare_payment output with the mock's own builder, so every tamper test starts from a
    payload that is proven to pass (positive control in setUp)."""
    token = USDC133
    coin = "USDC"

    def setUp(self):
        self.env = MockEnv()
        tok = flow.select_token(self.env.rows, coin=self.coin)
        self.assertEqual(tok.protocol, self.token.protocol)
        self.tokrow = tok
        order = self.env.hsp.call("create_order", merchant_name=config.MERCHANT_NAME, pay_to=self.env.pay_to,
                                  cart_mandate=flow.build_cart(tok, self.env.pay_to, Decimal("0.01"), time.time())[0])
        self.pr_id = order["payment_request_id"]
        self.prep = self.env.hsp.call("prepare_payment", flow_id=order["flow_id"], payer=self.env.payer.address,
                                      protocol=tok.protocol)
        self.exp = firewall.Expectation(payer=self.env.payer.address, pay_to=self.env.pay_to, token=tok,
                                        order_usd=Decimal("0.01"), gas_fee_advanced=True,
                                        payment_request_id=self.pr_id)
        firewall.check(self.prep, self.exp)          # positive control: the untampered payload passes

    def tearDown(self):
        self.env.close()

    def refuse(self, mutate, rule, exp=None):
        p = copy.deepcopy(self.prep)
        mutate(p)
        with self.assertRaises(firewall.Refused) as cm:
            firewall.check(p, exp or self.exp)
        self.assertEqual(cm.exception.rule, rule, str(cm.exception))


def mirror(p, **fields):
    """Tamper the signed message AND the x402 payload consistently, so only the guard under test can catch it."""
    p["eip712"]["message"].update(fields)
    p["payment_payload"]["payload"]["authorization"].update(fields)
    if "value" in fields:
        p["payment_payload"]["accepted"]["amount"] = fields["value"]


class TestFirewallEip3009(FirewallBase):
    def test_positive_control_signs_and_recovers(self):
        v = firewall.check(self.prep, self.exp)
        sig = flow.sign_typed(self.env.payer, v.typed_data)
        self.assertRegex(sig, r"^0x[0-9a-f]{130}$")
        # the normalised copy hashes identically to the gateway's original
        a = encode_typed_data(full_message=self.prep["eip712"])
        self.assertEqual(Account.recover_message(a, signature=sig), self.env.payer.address)
        self.assertEqual(v.token_total_raw, 10000)

    def test_recipient_swapped(self):
        def m(p):
            p["eip712"]["message"]["to"] = ATTACKER     # signed message only: the message.to guard must catch it
        self.refuse(m, "recipient")

    def test_recipient_swapped_everywhere(self):
        def m(p):
            mirror(p, to=ATTACKER)
            p["payment_payload"]["accepted"]["payTo"] = ATTACKER
            p["pay_to"] = ATTACKER
        with self.assertRaises(firewall.Refused):
            q = copy.deepcopy(self.prep)
            m(q)
            firewall.check(q, self.exp)

    def test_from_swapped(self):
        def m(p):
            p["eip712"]["message"]["from"] = ATTACKER
        self.refuse(m, "payer")

    def test_value_differs_from_total(self):
        def m(p):
            mirror(p, value="10001")              # token_total_raw stays 10000
            p["payment_payload"]["accepted"]["amount"] = "10000"
        self.refuse(m, "amount")

    def test_inflated_amount_over_order(self):
        def m(p):
            mirror(p, value="500000")             # 0.50 for a 0.01 order, consistent everywhere
            p["token_total_raw"] = "500000"
        self.refuse(m, "amount")

    def test_over_per_payment_cap(self):
        exp = firewall.Expectation(payer=self.exp.payer, pay_to=self.exp.pay_to, token=self.tokrow,
                                   order_usd=Decimal("1"), gas_fee_advanced=False)

        def m(p):
            p["eip712"]["message"]["value"] = p["token_total_raw"] = "1000001"
        self.refuse(m, "cap", exp)

    def test_order_over_cap_refused(self):
        exp = firewall.Expectation(payer=self.exp.payer, pay_to=self.exp.pay_to, token=self.tokrow,
                                   order_usd=Decimal("1.01"))
        self.refuse(lambda p: None, "cap", exp)

    def test_mainnet_chain_in_domain(self):
        def m(p):
            p["eip712"]["domain"]["chainId"] = 177
        self.refuse(m, "chain")

    def test_mainnet_token_row(self):
        tok = firewall.TokenRow("hashkey", 177, "USDC", self.tokrow.address, 6, "eip3009")
        exp = firewall.Expectation(payer=self.exp.payer, pay_to=self.exp.pay_to, token=tok, order_usd=Decimal("0.01"))
        self.refuse(lambda p: None, "mainnet", exp)

    def test_other_token_contract(self):
        def m(p):
            p["eip712"]["domain"]["verifyingContract"] = ATTACKER
        self.refuse(m, "token")

    def test_primary_type_swapped(self):
        def m(p):
            p["eip712"]["primaryType"] = "ReceiveWithAuthorization"
        self.refuse(m, "primaryType")

    def test_types_reordered(self):
        def m(p):
            t = p["eip712"]["types"]["TransferWithAuthorization"]
            t[0], t[1] = t[1], t[0]
        self.refuse(m, "types")

    def test_extra_struct_type(self):
        def m(p):
            p["eip712"]["types"]["Evil"] = [{"name": "x", "type": "uint256"}]
        self.refuse(m, "types")

    def test_extra_message_field(self):
        def m(p):
            p["eip712"]["message"]["memo"] = "x"
        self.refuse(m, "message")

    def test_valid_before_far_future(self):
        def m(p):
            mirror(p, validBefore=str(int(time.time()) + 86400))
        self.refuse(m, "expiry")

    def test_valid_before_too_soon(self):
        def m(p):
            mirror(p, validBefore=str(int(time.time()) + 5))
        self.refuse(m, "expiry")

    def test_valid_after_future(self):
        def m(p):
            p["eip712"]["message"]["validAfter"] = str(int(time.time()) + 3600)
        self.refuse(m, "expiry")

    def test_domain_salt(self):
        def m(p):
            p["eip712"]["domain"]["salt"] = "0x" + "11" * 32
        self.refuse(m, "domain")

    def test_domain_name_mismatch(self):
        def m(p):
            p["eip712"]["domain"]["name"] = "USD Coin (fake)"
        self.refuse(m, "domain")

    def test_echo_pay_to_differs(self):
        def m(p):
            p["pay_to"] = ATTACKER
        self.refuse(m, "pay_to")

    def test_echo_payer_differs(self):
        def m(p):
            p["payer"] = ATTACKER
        self.refuse(m, "payer")

    def test_payment_request_id_differs(self):
        def m(p):
            p["payment_request_id"] = "PRother"
        self.refuse(m, "payment_request_id")

    def test_x402_accepted_payto_differs(self):
        def m(p):
            p["payment_payload"]["accepted"]["payTo"] = ATTACKER
        self.refuse(m, "recipient")

    def test_x402_accepted_network_differs(self):
        def m(p):
            p["payment_payload"]["accepted"]["network"] = "eip155:177"
        self.refuse(m, "chain")

    def test_x402_authorization_to_differs(self):
        def m(p):
            p["payment_payload"]["payload"]["authorization"]["to"] = ATTACKER
        self.refuse(m, "recipient")

    def test_x402_authorization_value_differs(self):
        def m(p):
            p["payment_payload"]["payload"]["authorization"]["value"] = "999999"
        self.refuse(m, "amount")

    def test_hex_and_bool_values_rejected(self):
        for bad in ("0x2710", True, -1, "1e4", 10000.0):
            def m(p, bad=bad):
                p["eip712"]["message"]["value"] = bad
            self.refuse(m, "malformed")

    def test_bad_nonce(self):
        def m(p):
            p["eip712"]["message"]["nonce"] = "0x1234"
        self.refuse(m, "message")

    def test_extra_top_level_key(self):
        def m(p):
            p["eip712"]["extra"] = {}
        self.refuse(m, "malformed")

    def test_non_stablecoin(self):
        tok = firewall.TokenRow("hashkey-testnet", 133, "HSK", self.tokrow.address, 18, "eip3009")
        exp = firewall.Expectation(payer=self.exp.payer, pay_to=self.exp.pay_to, token=tok, order_usd=Decimal("0.01"))
        self.refuse(lambda p: None, "token", exp)

    def test_payer_equals_pay_to(self):
        exp = firewall.Expectation(payer=self.exp.payer, pay_to=self.exp.payer, token=self.tokrow,
                                   order_usd=Decimal("0.01"))
        self.refuse(lambda p: None, "payer_is_payee", exp)

    def test_missing_total(self):
        self.refuse(lambda p: p.pop("token_total_raw"), "amount")


class TestFirewallPermit2(FirewallBase):
    token = USDT133
    coin = "USDT"

    def test_positive_control(self):
        v = firewall.check(self.prep, self.exp)
        self.assertEqual(v.summary["protocol"], "permit2")
        flow.sign_typed(self.env.payer, v.typed_data)

    def test_spender_swapped(self):
        def m(p):
            p["eip712"]["message"]["spender"] = ATTACKER
        self.refuse(m, "recipient")

    def test_undeclared_spender_needs_pin(self):
        tok = firewall.TokenRow("hashkey-testnet", 133, "USDT", self.tokrow.address, 6, "permit2")
        exp = firewall.Expectation(payer=self.exp.payer, pay_to=self.exp.pay_to, token=tok, order_usd=Decimal("0.01"),
                                   gas_fee_advanced=True)
        self.refuse(lambda p: None, "recipient", exp)
        exp2 = firewall.Expectation(payer=self.exp.payer, pay_to=self.exp.pay_to, token=tok,
                                    order_usd=Decimal("0.01"), gas_fee_advanced=True,
                                    extra_permit2_spenders=(mock_server.MOCK_PERMIT2_SPENDER,))
        firewall.check(self.prep, exp2)

    def test_permitted_token_swapped(self):
        def m(p):
            p["eip712"]["message"]["permitted"]["token"] = ATTACKER
        self.refuse(m, "token")

    def test_amount_mismatch(self):
        def m(p):
            p["eip712"]["message"]["permitted"]["amount"] = "20000"
        self.refuse(m, "amount")

    def test_not_permit2_contract(self):
        def m(p):
            p["eip712"]["domain"]["verifyingContract"] = ATTACKER
        self.refuse(m, "token")

    def test_deadline_far(self):
        def m(p):
            p["eip712"]["message"]["deadline"] = str(int(time.time()) + 7 * 86400)
        self.refuse(m, "expiry")

    def test_witness_variant_refused(self):
        def m(p):
            p["eip712"]["primaryType"] = "PermitWitnessTransferFrom"
        self.refuse(m, "primaryType")


# =============================================================== MCP client
class TestMcpClient(unittest.TestCase):
    def test_url_pinning(self):
        ok = mcp_client.check_url("https://mcp-qa.hashkeymerchant.com/")
        self.assertEqual(ok[1], "mcp-qa.hashkeymerchant.com")
        for bad in ("https://evil.example/", "http://mcp-qa.hashkeymerchant.com/",
                    "https://mcp-qa.hashkeymerchant.com.evil.example/", "https://user:pw@mcp-qa.hashkeymerchant.com/",
                    "https://mcp-qa.hashkeymerchant.com:8443/", "http://127.0.0.1:9222/",
                    "https://mcp-qa.hashkeymerchant.com/?x=1", "file:///etc/passwd", "http://192.168.5.2:9222/"):
            with self.assertRaises(mcp_client.McpError, msg=bad):
                mcp_client.check_url(bad)
        mcp_client.check_url("http://127.0.0.1:8765/", allow_loopback=True)
        with self.assertRaises(mcp_client.McpError):
            mcp_client.check_url("http://192.168.5.2:9222/", allow_loopback=True)

    def _env(self, **kw):
        e = MockEnv(**kw)
        self.addCleanup(e.close)
        return e

    def test_json_and_sse_and_session(self):
        for sse in (False, True):
            e = self._env(sse=sse)
            self.assertTrue(e.mcp.session_id.startswith("mock-"))
            self.assertEqual(sorted(e.hsp.tools), sorted(config.REQUIRED_TOOLS))
            self.assertIn("networks", e.hsp.call("get_chain_config"))

    def test_session_required(self):
        e = self._env()
        e.mcp.session_id = "mock-forged"
        with self.assertRaises(mcp_client.McpHttpError) as cm:
            e.mcp.call_tool("get_chain_config", {})
        self.assertEqual(cm.exception.status, 404)

    def test_403_is_blocked(self):
        e = self._env()
        e.st.force_http = 403
        c = mcp_client.McpClient(e.url, allow_loopback=True)
        with self.assertRaises(mcp_client.McpBlocked):
            c.initialize()

    def test_redirect_refused(self):
        e = self._env()
        e.st.redirect = True
        c = mcp_client.McpClient(e.url, allow_loopback=True)
        with self.assertRaises(mcp_client.McpHttpError) as cm:
            c.initialize()
        self.assertEqual(cm.exception.status, 302)

    def test_oversize_refused(self):
        e = self._env()
        e.st.oversize = True
        with self.assertRaises(mcp_client.McpProtocolError):
            e.mcp.call_tool("get_chain_config", {})

    def test_timeout(self):
        e = self._env()
        e.st.delay["get_chain_config"] = 3
        t0 = time.monotonic()
        with self.assertRaises(mcp_client.McpTimeout):
            e.mcp.call_tool("get_chain_config", {}, total_timeout=1)
        self.assertLess(time.monotonic() - t0, 2.5)

    def test_missing_tool_stops(self):
        e = self._env()
        e.st.hide_tools = {"finalize_payment"}
        with self.assertRaises(flow.MissingTools):
            flow.Hsp(mcp_client.McpClient(e.url, allow_loopback=True)).discover()

    def test_health_mock(self):
        e = self._env()
        st, body = mcp_client.health(e.url + "health", allow_loopback=True)
        self.assertEqual((st, body), (200, '{"status":"ok"}'))


# =============================================================== flow (mock end to end)
class TestFlow(unittest.TestCase):
    def env(self, **kw):
        e = MockEnv(**kw)
        self.addCleanup(e.close)
        return e

    def pay(self, e, amount="0.01", coin=None, **kw):
        tok = flow.select_token(e.rows, coin=coin)
        return flow.run_payment(e.ctx, tok, Decimal(amount), **kw)

    def test_e2e_finalized_and_attribution(self):
        e = self.env()
        r = self.pay(e)
        self.assertEqual(r["status"], "payment-finalized")
        self.assertIs(r["attribution_verified"], True)
        self.assertRegex(r["tx_signature"], r"^0x[0-9a-f]{64}$")
        self.assertEqual(r["to_pay_address"], e.pay_to)
        # merchant_name is the single constant everywhere
        co = e.calls("create_order")[0]
        self.assertEqual(co["merchant_name"], "Swift Bot Labs (HSK Meter)")
        self.assertEqual(co["cart_mandate"]["contents"]["merchant_name"], config.MERCHANT_NAME)
        self.assertNotIn("merchant_authorization", co["cart_mandate"])
        self.assertTrue(all(q["expected_merchant_name"] == config.MERCHANT_NAME for q in e.calls("query_payment")))
        steps = e.steps()
        for s in ("start", "create_order", "get_order", "estimate_fee", "prepare_payment", "firewall_ok", "balance",
                  "signed", "finalize_payment", "submit_payment", "query_payment"):
            self.assertIn(s, steps)
        rows = e.receipts.rows()
        self.assertTrue(all(r.get("flow_id") for r in rows if r["step"] not in ("start",)))
        final = [x for x in rows if x["step"] == "query_payment"][-1]
        self.assertEqual(final["status"], "payment-finalized")
        self.assertIs(final["attribution_verified"], True)
        # the signature and the mandate JWT never reach the receipts
        sig = e.calls("finalize_payment")[0]["signature"]
        with open(e.rpath) as f:
            text = f.read()
        self.assertNotIn(sig[2:], text)
        self.assertNotIn("eyJhbGci", text)
        self.assertEqual(len(e.calls("submit_payment")), 1)
        self.assertIn("The payment has reached final confirmation.", flow.report(r))

    def test_e2e_sse_and_alt_schema(self):
        e = self.env(sse=True, schema_variant="alt", structured=False)
        r = self.pay(e)
        self.assertEqual(r["status"], "payment-finalized")
        fa = e.calls("finalize_payment")[0]
        self.assertIsInstance(fa["payment_payload"], str)          # coerced to the live schema's type
        self.assertIn("payer_address", e.calls("prepare_payment")[0])

    def test_e2e_permit2(self):
        e = self.env()
        r = self.pay(e, coin="USDT")
        self.assertEqual(r["status"], "payment-finalized")
        self.assertIn("permit2_allowance", e.steps())

    def test_30043_on_submit_reprepares_and_resigns(self):
        e = self.env(submit_30043=1)
        r = self.pay(e)
        self.assertEqual(r["status"], "payment-finalized")
        self.assertEqual(len(e.calls("prepare_payment")), 2)
        sigs = [c["signature"] for c in e.calls("finalize_payment")]
        self.assertEqual(len(sigs), 2)
        self.assertNotEqual(sigs[0], sigs[1])                      # never the old signature
        mands = [c["payment_mandate_json"] for c in e.calls("submit_payment")]
        self.assertEqual(len(mands), 2)
        self.assertNotEqual(mands[0], mands[1])

    def test_30043_envelope_style(self):
        e = self.env(submit_30043=1, error_style="envelope")
        self.assertEqual(self.pay(e)["status"], "payment-finalized")
        self.assertEqual(len(e.calls("prepare_payment")), 2)

    def test_30043_on_finalize(self):
        e = self.env(finalize_30043=1)
        self.assertEqual(self.pay(e)["status"], "payment-finalized")
        self.assertEqual(len(e.calls("prepare_payment")), 2)

    def test_30043_forever_gives_up(self):
        e = self.env(submit_30043=99)
        with self.assertRaises(flow.FlowStop) as cm:
            self.pay(e)
        self.assertEqual(cm.exception.reason, "quote_retries_exhausted")
        self.assertEqual(len(e.calls("prepare_payment")), config.MAX_PREPARE_ATTEMPTS)

    def test_short_quote_never_signed(self):
        e = self.env(quote_ttl=10)          # below QUOTE_REPREPARE_MARGIN_S
        with self.assertRaises(flow.FlowStop):
            self.pay(e)
        self.assertEqual(e.calls("finalize_payment"), [])
        self.assertIn("quote_expired_before_sign", e.steps())

    def test_quote_expires_between_sign_and_finalize(self):
        e = self.env()
        state = {"n": 0}

        def approve(stage, s):
            if stage == "sign" and state["n"] == 0:
                state["n"] = 1
                e.st.quotes[s["flow_id"]]["expires"] = 0   # the gateway's 5-minute quote lapses while we sign
            return True
        e.ctx.approve = approve
        r = self.pay(e)
        self.assertEqual(r["status"], "payment-finalized")
        self.assertEqual(len(e.calls("prepare_payment")), 2)
        self.assertIn("finalize_error", e.steps())

    def test_prepare_timeout_propagates_without_signing(self):
        e = self.env()
        e.st.delay["prepare_payment"] = 3
        e.mcp.total_timeout = 1
        with self.assertRaises(mcp_client.McpTimeout):
            self.pay(e)
        self.assertEqual(e.calls("finalize_payment"), [])

    def test_submit_timeout_queries_instead_of_resubmitting(self):
        e = self.env()
        e.st.delay["submit_payment"] = 2
        old = config.SUBMIT_TIMEOUT_S
        config.SUBMIT_TIMEOUT_S = 1
        try:
            e.ctx.sleep = lambda s: time.sleep(0.6)
            r = self.pay(e)
        finally:
            config.SUBMIT_TIMEOUT_S = old
        self.assertEqual(len(e.calls("submit_payment")), 1)
        self.assertIn("submit_uncertain", e.steps())
        self.assertEqual(r["status"], "payment-finalized")

    def test_payment_safe_stops_polling(self):
        seq = ["payment-submitted", "payment-included", "payment-safe", "payment-finalized"]
        e = self.env(status_seq=list(seq))
        r = self.pay(e)
        self.assertEqual(r["status"], "payment-safe")
        self.assertIn("not yet finalized", flow.report(r))
        e2 = self.env(status_seq=list(seq))
        self.assertEqual(self.pay(e2, wait_finalized=True)["status"], "payment-finalized")

    def test_timeout_status_keeps_polling(self):
        e = self.env(status_seq=["payment-submitted", "payment-timeout", "payment-timeout", "payment-finalized"])
        self.assertEqual(self.pay(e)["status"], "payment-finalized")
        self.assertEqual(len(e.calls("create_order")), 1)

    def test_payment_failed_reports_reason(self):
        e = self.env(status_seq=["payment-submitted", "payment-failed"])
        r = self.pay(e)
        self.assertEqual(r["status"], "payment-failed")
        self.assertIn("insufficient balance", flow.report(r))

    def test_query_errors_then_recovers(self):
        e = self.env(query_errors=2)
        self.assertEqual(self.pay(e)["status"], "payment-finalized")

    def test_pending_confirmation_and_stop(self):
        e = self.env(status_seq=["payment-submitted", "payment-processing"])
        t = {"now": time.time()}

        def sleep(s):
            t["now"] += 15
        e.ctx.sleep = sleep
        e.ctx.clock = lambda: t["now"]
        e.ctx.max_wait, e.ctx.pending_report_s = 120, 60
        r = self.pay(e)
        self.assertTrue(r.get("stopped_polling"))
        self.assertTrue(any("Pending confirmation" in o for o in e.out))
        self.assertIn("pending_confirmation", e.steps())

    def test_funding_needed_never_signs(self):
        e = self.env()
        e.ctx.balance_fn = lambda c, t, o: 9999
        with self.assertRaises(flow.FlowStop) as cm:
            self.pay(e)
        self.assertEqual(cm.exception.reason, "funding_needed")
        self.assertEqual(cm.exception.info["need_raw"], 10000)
        self.assertEqual(e.calls("finalize_payment"), [])

    def test_permit2_approval_needed(self):
        e = self.env()
        e.ctx.allowance_fn = lambda c, t, o, s: 0
        with self.assertRaises(flow.FlowStop) as cm:
            self.pay(e, coin="USDT")
        self.assertEqual(cm.exception.reason, "permit2_approval_needed")

    def test_no_balance_check_refuses_live_sign(self):
        e = self.env()
        e.ctx.balance_fn = None
        with self.assertRaises(flow.FlowStop) as cm:
            self.pay(e)
        self.assertEqual(cm.exception.reason, "no_balance_check")

    def test_dry_stops_before_signing(self):
        e = self.env()
        e.ctx.balance_fn = lambda c, t, o: 0
        r = self.pay(e, dry=True)
        self.assertEqual(r["status"], "dry")
        self.assertIs(r["funding"]["funded"], False)
        self.assertEqual(e.calls("finalize_payment"), [])
        self.assertEqual(e.calls("submit_payment"), [])

    def test_not_approved(self):
        e = self.env()
        e.ctx.approve = lambda stage, s: stage != "submit"
        with self.assertRaises(flow.FlowStop):
            self.pay(e)
        self.assertEqual(e.calls("submit_payment"), [])

    def test_gateway_tamper_refused_in_flow(self):
        e = self.env()
        e.st.tamper = lambda p: p["eip712"]["message"].update(to=ATTACKER)
        with self.assertRaises(flow.FlowStop) as cm:
            self.pay(e)
        self.assertEqual(cm.exception.reason, "firewall_refused")
        self.assertEqual(cm.exception.info["rule"], "recipient")
        self.assertEqual(e.calls("finalize_payment"), [])

    def test_amount_and_lifetime_caps(self):
        e = self.env()
        with self.assertRaises(flow.FlowStop):
            self.pay(e, amount="1.01")
        e.receipts.log("submit_payment", amount_usd="4.995")
        with self.assertRaises(flow.FlowStop) as cm:
            self.pay(e, amount="0.01")
        self.assertIn("lifetime", cm.exception.info["detail"])
        self.assertEqual(e.calls("create_order"), [])

    def test_wrapped_results(self):
        """A server that wraps every tool result as {"data": {...}} (no code envelope) still works."""
        e = self.env()
        orig = dict(mock_server.TOOLS)
        try:
            for k, fn in orig.items():
                mock_server.TOOLS[k] = (lambda f: lambda st, a: {"data": f(st, a), "trace": "t"})(fn)
            self.assertEqual(self.pay(e)["status"], "payment-finalized")
        finally:
            mock_server.TOOLS.clear()
            mock_server.TOOLS.update(orig)

    def test_gas_not_advanced_allows_fee(self):
        e = self.env(gas_fee_advanced=False)
        self.assertEqual(self.pay(e)["status"], "payment-finalized")


class TestChainConfig(unittest.TestCase):
    def test_variants(self):
        cfg = {"code": 0, "data": {"chains": [{"name": "hashkey-testnet", "chainId": "eip155:133", "tokens": [
            {"symbol": "USDC", "contract_address": USDC133.address, "decimals": 6, "protocol": "EIP3009"},
            {"symbol": "BAD", "contract_address": "0x12", "decimals": 6, "protocol": "eip3009"},
            {"symbol": "X", "contract_address": USDC133.address, "decimals": True, "protocol": "eip3009"},
            {"symbol": "Y", "contract_address": USDC133.address, "decimals": 6, "protocol": "transfer"}]}]}}
        rows, rejects = flow.parse_chain_config(flow.unwrap(cfg))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].chain_id, rows[0].protocol), (133, "eip3009"))
        self.assertEqual(len(rejects), 3)

    def test_mainnet_only_selects_nothing(self):
        rows = [firewall.TokenRow("hashkey", 177, "USDC", USDC133.address, 6, "eip3009")]
        self.assertIsNone(flow.select_token(rows))

    def test_envelope_error(self):
        with self.assertRaises(flow.HspError) as cm:
            flow.unwrap({"code": 30043, "msg": "quote expired"})
        self.assertTrue(flow.is_quote_error(cm.exception))


# =============================================================== wallets and probe discipline
class TestWallets(unittest.TestCase):
    def test_create_load_and_no_key_output(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pay.main(["wallets", "--create"])
            pay.main(["wallets", "--create"])
            pay.main(["wallets"])
        out = buf.getvalue()
        self.assertNotRegex(out, r"[0-9a-fA-F]{64}")
        self.assertIn("exists", out)
        p = os.path.join(config.SECRETS_DIR, config.PAYER_ENV)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        a = wallets.load_payer()
        self.assertEqual(a.address, wallets.payer_address())
        self.assertNotEqual(a.address, wallets.merchant_address())

    def test_refuses_loose_mode_and_symlink(self):
        wallets.create(config.PAYER_ENV, "payer")
        p = os.path.join(config.SECRETS_DIR, config.PAYER_ENV)
        os.chmod(p, 0o644)
        try:
            with self.assertRaises(wallets.WalletError):
                wallets.load_payer()
        finally:
            os.chmod(p, 0o600)
        link = os.path.join(config.SECRETS_DIR, "link.env")
        os.symlink(p, link)
        with self.assertRaises(wallets.WalletError):
            wallets.load_account("link.env")


class TestProbeDiscipline(unittest.TestCase):
    def setUp(self):
        self.real_health = pay.health
        self.n = 0

        def fake(*a, **k):
            self.n += 1
            return 403, "<html>Forbidden</html>"
        pay.health = fake
        sp = os.path.join(config.STATE_DIR, "probe.json")
        if os.path.exists(sp):
            os.remove(sp)

    def tearDown(self):
        pay.health = self.real_health

    def test_one_probe_per_30_min_and_live_guard(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pay.main(["health"]), 3)
            self.assertEqual(pay.main(["health"]), 5)      # second call within 30 min: skipped, no contact
        self.assertEqual(self.n, 1)
        with self.assertRaises(SystemExit):
            pay.guard_live()                                # 403 recorded: live MCP calls refused too
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pay.main(["probe"]), 0)        # probe also respects the window
        self.assertEqual(self.n, 1)

    def test_probe_proceeds_only_when_allowlisted(self):
        calls = []
        real_make = pay.make_hsp
        pay.make_hsp = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(RuntimeError("no live contact"))
        try:
            with pay.State() as s:          # a fresh probe slot that was skipped must not lead to discover
                s.d.update(last_probe_ts=time.time(), last_probe_status=403)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pay.main(["probe"]), 0)
            self.assertEqual(calls, [])
            self.assertEqual(self.n, 0)
        finally:
            pay.make_hsp = real_make

    def test_allowlisted_probe_runs_discovery(self):
        def ok(*a, **k):
            self.n += 1
            return 200, '{"status":"ok"}'
        pay.health = ok
        reached = []
        real_make = pay.make_hsp

        def fake_make(ns, *a, **k):
            reached.append(ns.dry)
            raise pay.McpBlocked(403, "stop here")
        pay.make_hsp = fake_make
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pay.main(["probe"]), 0)
        finally:
            pay.make_hsp = real_make
        self.assertEqual(self.n, 1)
        self.assertEqual(reached, [True])          # discovery attempted, in dry mode

    def test_no_probe_after_deadline(self):
        old = config.PROBE_DEADLINE_UTC
        config.PROBE_DEADLINE_UTC = "2020-01-01T00:00:00Z"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pay.main(["health"])
                pay.main(["probe"])
        finally:
            config.PROBE_DEADLINE_UTC = old
        self.assertEqual(self.n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
