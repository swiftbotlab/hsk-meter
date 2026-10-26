#!/usr/bin/env python3
"""Offline end-to-end tests for Agent B (agent/agent_b.py), the agent-to-agent (A2A) x402 demo.

No network, no real keys, no mainnet. Both the REAL Agent A seller (server/seller.py) and Agent B (agent_b.py,
which reuses the same Seller class) run over HTTP on random loopback ports on top of ONE shared offline chain
(MultiChain, a subclass of server/testkit.FakeChain that supports a different provider/payee per endpoint id and
authorises refunds against the endpoint's provider). The chain executes the real signed EIP-3009 / x402
transactions.

What the e2e proves:
  * one end-buyer request to Agent B (/v1/hsk/risk-brief, endpoint 3) triggers a SECOND, nested x402 settlement
    from Agent B's own wallet to Agent A (/v1/hsk/address-profile, endpoint 1) -> distinct on-chain roles;
  * the end-buyer's payment to Agent B settles exactly once, and Agent B's nested payment to Agent A settles
    exactly once;
  * the composed response carries a real upstream settlement tx hash and a riskScore;
  * when Agent A cannot deliver, Agent B's nested payment is refunded AND the end-buyer is refunded on-chain, so
    Agent B keeps nothing.

  test_agent_b.py [-v]
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest

from eth_abi import decode, encode

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, HERE)
import buyer as B       # noqa: E402
import seller as S      # noqa: E402
import chain as C       # noqa: E402
import protocol as P    # noqa: E402
import testkit as T     # noqa: E402
import agent_b as AB    # noqa: E402
from eth_account import Account  # noqa: E402

CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"
DEAD = "0x000000000000000000000000000000000000dead"
PRICE_A, PRICE_B = 30000, 50000          # endpoint 1 (Agent A) and endpoint 3 (Agent B) prices, in USDC.e units


class MultiChain(T.FakeChain):
    """FakeChain with a per-endpoint provider/payee. getEndpoint(eid) returns that endpoint's roles, and refund(rid)
    is authorised against the provider of the payment's endpoint (the base FakeChain has a single global provider)."""

    def __init__(self, contract, eps):
        first = next(iter(eps.values()))
        super().__init__(contract, first["provider"], first["payee"],
                         prices={eid: e["price"] for eid, e in eps.items()})
        self.eps = {eid: {"provider": e["provider"].lower(), "payee": e["payee"].lower()} for eid, e in eps.items()}
        for eid, e in eps.items():
            self.endpoints[eid]["active"] = e.get("active", True)
        self._swap_lock = threading.RLock()
        for e in self.eps.values():                      # every provider needs native HSK for gas
            self.native.setdefault(e["provider"], 100 * 10**18)

    def _eid_of_call(self, data):
        data = data if isinstance(data, str) else "0x" + bytes(data).hex()
        s, args = data[:10], bytes.fromhex(data[10:])
        try:
            if s == T.S_PWA:
                return decode(["uint256", "bytes32", "address", "uint256", "uint256", "uint256", "bytes"], args)[0]
            if s == T.S_SETPRICE:
                return decode(["uint256", "uint128"], args)[0]
            if s == T.S_REFUND:
                rid = "0x" + decode(["bytes32"], args)[0].hex()
                p = self.payments.get(rid)
                return p["endpointId"] if p else None
        except Exception:
            return None
        return None

    def _call(self, call):
        to, data = call["to"].lower(), call["data"]
        s, args = data[:10], bytes.fromhex(data[10:])
        if to == self.contract and s == T.S_GETEP:
            eid = decode(["uint256"], args)[0]
            ep, e = self.eps.get(eid), self.endpoints.get(eid)
            if not ep or not e:
                return "0x" + encode(T.ENDPOINT_T, [(P.ZERO, False, 0, P.ZERO, 0, 0, 0, 0, 0)]).hex()
            return "0x" + encode(T.ENDPOINT_T, [(ep["provider"], e["active"], 1, ep["payee"], 0, e["price"], 0, 0, 0)]).hex()
        return super()._call(call)

    def _simulate(self, call, apply=False, tx_hash=None):
        to = call.get("to", "")
        if not isinstance(to, str) or to.lower() != self.contract:
            return super()._simulate(call, apply, tx_hash)
        ep = self.eps.get(self._eid_of_call(call["data"]))
        with self._swap_lock:
            saved = (self.provider, self.payee)
            if ep:
                self.provider, self.payee = ep["provider"], ep["payee"]
            try:
                return super()._simulate(call, apply, tx_hash)
            finally:
                self.provider, self.payee = saved


class A2A:
    """Stand up Agent A (endpoints 1 & 2) and Agent B (endpoint 3) on one shared MultiChain, with Agent B wired to
    pay Agent A for the raw profile. S.RESOURCES / S.WORK hold all three paths at once so both sellers dispatch in
    one process (on mainnet Agent B runs in its own process and advertises only endpoint 3 via install_resources)."""

    def __init__(self, smoke_a=None, fund=10**9):
        base = os.path.join(ROOT, "work")
        os.makedirs(base, exist_ok=True)
        self.d = tempfile.mkdtemp(dir=base)
        self.pA, self.aA = Account.create(), Account.create()        # Agent A provider + HSP adapter
        self.pB, self.aB = Account.create(), Account.create()        # Agent B provider + HSP adapter
        self.payeeA, self.payeeB = "0x" + "a1" * 20, "0x" + "b2" * 20
        self.buyerB = Account.create()                               # Agent B's buyer wallet (pays Agent A)
        self.end = Account.create()                                  # the end-buyer wallet (pays Agent B)
        self.ch = MultiChain(CONTRACT, {
            1: {"provider": self.pA.address, "payee": self.payeeA, "price": PRICE_A},
            2: {"provider": self.pA.address, "payee": self.payeeA, "price": 25000},
            3: {"provider": self.pB.address, "payee": self.payeeB, "price": PRICE_B},
        })
        self.ch.fund(self.end.address, fund)
        self.ch.fund(self.buyerB.address, fund)
        self.start_end, self.start_b = fund, fund

        self.sellerA = S.Seller({"contract": CONTRACT, "tx_wait_s": 0.3, "bind_host": "127.0.0.1",
                                 "price_feeds": False, "smoke_refund_address": smoke_a},
                                rpc=self.ch, provider=self.pA, adapter=self.aA, token_key=os.urandom(32))
        self.sellerB = S.Seller({"contract": CONTRACT, "tx_wait_s": 0.3, "bind_host": "127.0.0.1",
                                 "price_feeds": False, "smoke_refund_address": None},
                                rpc=self.ch, provider=self.pB, adapter=self.aB, token_key=os.urandom(32))
        self.httpdA = S.Server(("127.0.0.1", 0), S.make_handler(self.sellerA))
        self.httpdB = S.Server(("127.0.0.1", 0), S.make_handler(self.sellerB))
        for h in (self.httpdA, self.httpdB):
            threading.Thread(target=h.serve_forever, daemon=True).start()
        self.baseA = "http://127.0.0.1:%d" % self.httpdA.server_address[1]
        self.baseB = "http://127.0.0.1:%d" % self.httpdB.server_address[1]

        self.end_session = os.path.join(self.d, "end_session.json")
        self.end_journal = os.path.join(self.d, "end_runs.jsonl")
        AB.configure_upstream(agent_a_url=self.baseA, a_provider=self.pA.address, a_payee=self.payeeA,
                              contract=CONTRACT, buyer_account=self.buyerB,
                              session_file=os.path.join(self.d, "b_session.json"),
                              max_units=200000, cap_units=1000000,
                              journal_file=os.path.join(self.d, "b_runs.jsonl"))

    def pay_agent_b(self, address):
        return B.buy(url=self.baseB + AB.RISK_PATH + "?address=" + address, me=self.end, rpc=self.ch,
                     contract=CONTRACT, expect_provider=self.pB.address, expect_payee=self.payeeB,
                     max_units=200000, cap_units=1000000, scheme="exact",
                     session_file=self.end_session, journal_file=self.end_journal)

    # ---- chain inspection helpers
    def payments(self, eid, payer=None):
        return [p for p in self.ch.payments.values()
                if p["endpointId"] == eid and (payer is None or p["payer"] == payer.address.lower())]

    def sent(self, selector):
        return sum(1 for s in self.ch.sent if s == selector)

    def bal(self, acct):
        return self.ch.usdce.get(acct.address.lower(), 0)

    def close(self):
        for h in (self.httpdA, self.httpdB):
            h.shutdown()
            h.server_close()
        shutil.rmtree(self.d, ignore_errors=True)


class Compose(unittest.TestCase):
    """The risk scoring is deterministic and explainable, derived only from the profile; the score is clamped 0-100
    and the upstream payment proof is carried through. (Pure function, no network.)"""

    STUB = {"amount": 30000, "endpointId": 1, "requestId": "0x" + "ab" * 32,
            "settlement_tx": "0x" + "cd" * 32, "settlement_verified": True}

    def brief(self, profile):
        return AB._compose(profile["address"], profile, self.STUB)

    def test_eoa_empty_is_elevated(self):
        r = self.brief({"address": "0x" + "01" * 20, "type": "eoa", "codeSize": 0, "nonce": 0,
                        "balances": {"HSK": "0.000000", "USDC.e": "0.000000", "USDT": "0.000000", "WHSK": "0.000000"},
                        "block": 100, "explorer": "https://hsk.blockscout.com/address/0x01"})
        self.assertEqual(r["riskScore"], 80)          # 50 -5(eoa) +20(no_activity) +15(empty_balance)
        self.assertEqual(r["riskLabel"], "elevated")
        self.assertEqual(r["composedBy"], "agent-b/risk-brief")
        self.assertEqual(r["upstreamPayments"][0]["tx"], self.STUB["settlement_tx"])
        self.assertEqual(r["upstreamPayments"][0]["amount"], "0.030000")
        self.assertTrue(all({"factor", "impact", "detail"} <= set(f) for f in r["riskFactors"]))
        self.assertIn("NOT financial", r["disclaimer"])

    def test_funded_established_contract_is_moderate(self):
        r = self.brief({"address": "0x" + "02" * 20, "type": "contract", "codeSize": 4096, "nonce": 500,
                        "balances": {"HSK": "1.000000", "USDC.e": "10.000000", "USDT": "0.000000", "WHSK": "0.000000"},
                        "block": 100, "explorer": "https://hsk.blockscout.com/address/0x02"})
        self.assertEqual(r["riskScore"], 40)          # 50 +15(contract) -15(established) -10(stablecoins)
        self.assertEqual(r["riskLabel"], "moderate")

    def test_score_is_clamped_at_100(self):
        r = self.brief({"address": "0x" + "03" * 20, "type": "contract", "codeSize": 2, "nonce": 0,
                        "balances": {"HSK": "0", "USDC.e": "0", "USDT": "0", "WHSK": "0"}, "block": 1})
        self.assertEqual(r["riskScore"], 100)         # 50 +15 +20 +15 = 100, clamped
        self.assertEqual(r["riskLabel"], "elevated")

    def test_untrusted_profile_is_handled_defensively(self):
        # garbage / hostile upstream body: no crash, bounded output, explorer host allow-listed
        r = self.brief({"address": "0x" + "04" * 20, "type": "../../evil", "codeSize": "lots", "nonce": "many",
                        "balances": {"HSK": {"nested": 1}, "USDC.e": "oops"}, "block": "x",
                        "explorer": "javascript:alert(1)"})
        self.assertTrue(0 <= r["riskScore"] <= 100)
        self.assertIsNone(r["basis"]["type"])
        self.assertIsNone(r["basis"]["nonce"])
        self.assertIsNone(r["basis"]["explorer"])
        self.assertIsNone(r["basis"]["block"])


class ResourceInstall(unittest.TestCase):
    def test_install_resources_advertises_only_ep3(self):
        saved = (dict(S.RESOURCES), dict(S.WORK))
        try:
            AB.install_resources()
            self.assertEqual(list(S.RESOURCES), [AB.RISK_PATH])
            self.assertEqual(S.RESOURCES[AB.RISK_PATH]["endpointId"], 3)
            self.assertIs(S.WORK[AB.RISK_PATH], AB.risk_brief)
        finally:
            S.RESOURCES, S.WORK = saved


class EndToEnd(unittest.TestCase):
    """The full A2A chain against the shared offline chain."""

    def setUp(self):
        # all three paths dispatchable in one process; fast receipt polling (no 40 s waits in tests)
        self.saved_res, self.saved_work = dict(S.RESOURCES), dict(S.WORK)
        self.saved_poll = (B.RECEIPT_POLLS, B.RECEIPT_WAIT_S)
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = 3, 0
        S.RESOURCES = {**S.RESOURCES, **AB.RISK_RESOURCE}
        S.WORK = {**S.WORK, AB.RISK_PATH: AB.risk_brief}

    def tearDown(self):
        S.RESOURCES, S.WORK = self.saved_res, self.saved_work
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = self.saved_poll
        self.env.close()

    def test_a2a_success_one_request_two_settlements(self):
        self.env = A2A()
        ADDR = "0x" + "01" * 20
        out = self.env.pay_agent_b(ADDR)

        # the end-buyer got the composed brief
        self.assertTrue(out.get("delivered"), out)
        data = out["data"]
        self.assertIsInstance(data, dict)
        self.assertEqual(data["composedBy"], "agent-b/risk-brief")
        self.assertEqual(data["address"], ADDR)
        self.assertEqual(data["chainId"], 177)
        self.assertTrue(0 <= data["riskScore"] <= 100)
        self.assertEqual(data["riskScore"], 80, "EOA, nonce 0, empty balances")

        # (c) the response carries a REAL upstream tx hash and a riskScore
        up = data["upstreamPayments"][0]
        self.assertTrue(P.B32_RE.match(up["tx"] or ""), up)
        self.assertIs(up["verifiedOnChain"], True)
        self.assertEqual((up["endpointId"], up["amountUnits"], up["amount"]), (1, PRICE_A, "0.030000"))

        # (a) end-buyer -> Agent B settled exactly once (endpoint 3, end-buyer wallet)
        ep3 = self.env.payments(3, self.env.end)
        self.assertEqual(len(ep3), 1, "exactly one end-buyer payment to Agent B")
        self.assertFalse(ep3[0]["refunded"])
        self.assertEqual(ep3[0]["amount"], PRICE_B)

        # (b) Agent B -> Agent A settled exactly once (endpoint 1, Agent B's buyer wallet) -> distinct roles
        ep1 = self.env.payments(1, self.env.buyerB)
        self.assertEqual(len(ep1), 1, "exactly one nested payment to Agent A")
        self.assertFalse(ep1[0]["refunded"])
        self.assertEqual(ep1[0]["amount"], PRICE_A)
        self.assertNotEqual(self.env.pB.address.lower(), self.env.pA.address.lower(), "distinct providers")
        self.assertNotEqual(self.env.payeeB.lower(), self.env.payeeA.lower(), "distinct payees")

        # exactly two settlements, no refunds
        self.assertEqual((self.env.sent(T.S_PWA), self.env.sent(T.S_REFUND)), (2, 0))
        self.assertEqual(self.env.start_end - self.env.bal(self.env.end), PRICE_B, "end-buyer charged once")
        self.assertEqual(self.env.start_b - self.env.bal(self.env.buyerB), PRICE_A, "Agent B charged once (nested)")

    def test_upstream_failure_refunds_the_end_buyer(self):
        # Agent A is made to fail delivery for DEAD (its operator smoke test); Agent B must not keep the money.
        self.env = A2A(smoke_a=DEAD)
        out = self.env.pay_agent_b(DEAD)

        # (d) the end-buyer is refunded on-chain and gets no data
        self.assertFalse(out.get("delivered"), out)
        self.assertNotIn("data", out)
        self.assertEqual(out.get("payment"), "refunded", out)
        self.assertEqual(out["refund"]["status"], "verified", out)
        self.assertTrue(out["refund"]["verified"])

        # the nested payment to Agent A was settled then refunded to Agent B; the end-buyer payment refunded too
        ep1 = self.env.payments(1, self.env.buyerB)
        ep3 = self.env.payments(3, self.env.end)
        self.assertEqual((len(ep1), len(ep3)), (1, 1))
        self.assertTrue(ep1[0]["refunded"], "Agent A refunded Agent B's nested payment")
        self.assertTrue(ep3[0]["refunded"], "Agent B refunded the end-buyer")

        # two settlements, two refunds; nobody keeps anything
        self.assertEqual((self.env.sent(T.S_PWA), self.env.sent(T.S_REFUND)), (2, 2))
        self.assertEqual(self.env.bal(self.env.end), self.env.start_end, "end-buyer made whole")
        self.assertEqual(self.env.bal(self.env.buyerB), self.env.start_b, "Agent B kept nothing")

    def test_positive_control_delivers_when_agent_a_is_healthy(self):
        # same DEAD address, but WITHOUT Agent A's smoke failure: it delivers (proves the refund path above is the
        # failure, not the address).
        self.env = A2A(smoke_a=None)
        out = self.env.pay_agent_b(DEAD)
        self.assertTrue(out.get("delivered"), out)
        self.assertEqual(out["data"]["address"], DEAD)
        self.assertTrue(P.B32_RE.match(out["data"]["upstreamPayments"][0]["tx"] or ""))
        self.assertEqual((self.env.sent(T.S_PWA), self.env.sent(T.S_REFUND)), (2, 0))


if __name__ == "__main__":
    unittest.main()
