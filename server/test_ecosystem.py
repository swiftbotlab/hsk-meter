#!/usr/bin/env python3
"""Tests for the HashKey ecosystem integrations (Swift Bot Labs): APRO price feeds (server/oracle.py: quote USD
estimate, gas guard, buyer USD view) and the optional KYC SBT compliant mode (server/kyc.py). Offline: the seller runs
against testkit.FakeChain, which answers the feeds' latestRoundData/decimals/description and the SBT's
isHuman/getKycInfo.

  test_ecosystem.py [-v] [--online]     (--online adds read-only checks against the real feeds and SBT on mainnet)
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "agent"))
import buyer as B        # noqa: E402
import chain as C        # noqa: E402
import kyc as K          # noqa: E402
import oracle as O       # noqa: E402
import protocol as P     # noqa: E402
import seller as S       # noqa: E402
import testkit as T      # noqa: E402
from eth_abi import encode  # noqa: E402
from eth_account import Account  # noqa: E402
from test_security import Env, PROFILE, PQ, PWA, REFUND, CONTRACT  # noqa: E402

ONLINE = "--online" in sys.argv
if ONLINE:
    sys.argv.remove("--online")
USDC_FEED, HSK_FEED = O.FEEDS["USDC/USD"][0], O.FEEDS["HSK/USD"][0]


def feeds(ch, usdc=0.99982693, hsk=0.08728263, **kw):
    if usdc is not None:
        ch.set_feed(USDC_FEED, "USDC/USD", usdc, **kw)
    if hsk is not None:
        ch.set_feed(HSK_FEED, "HSK/USD", hsk, **kw)


def word_round(rid=5, answer=10**8, started=None, updated=None, answered=None):
    now = int(time.time())
    return encode(["uint80", "int256", "uint256", "uint256", "uint80"],
                  [rid, answer, started or now, now - 60 if updated is None else updated, rid if answered is None else answered])


# ====================================================================== oracle unit tests
class RoundValidation(unittest.TestCase):
    def test_valid_round(self):
        r = O.parse_round("USDC/USD", word_round(answer=99982693))
        self.assertEqual(r["price"], Decimal("0.99982693"))
        self.assertEqual(r["feed"], USDC_FEED)
        self.assertEqual(O.public(r)["price"], "0.99982693")

    def test_rejects_bad_rounds(self):
        now = int(time.time())
        bad = {
            "non-positive": word_round(answer=0),
            "negative": word_round(answer=-5),
            "unfinished (updatedAt 0)": word_round(updated=0),
            "answeredInRound < roundId": word_round(rid=9, answered=8),
            "roundId 0": word_round(rid=0, answered=0),
            "from the future": word_round(updated=now + 3600),
            "stale (27 h)": word_round(updated=now - 27 * 3600),
            "out of range for a stablecoin": word_round(answer=3 * 10**8),
            "malformed": b"\x00" * 31,
        }
        for name, raw in bad.items():
            with self.subTest(name), self.assertRaises(O.FeedError):
                O.parse_round("USDC/USD", raw)

    def test_25h_old_round_is_current(self):            # the feeds have a 24 h heartbeat on mainnet
        O.parse_round("HSK/USD", word_round(answer=8728263, updated=int(time.time()) - 25 * 3600))

    def test_usd_of_units_rounds_down(self):
        r = O.parse_round("USDC/USD", word_round(answer=99982693))
        self.assertEqual(O.usd_of_units(30000, r), Decimal("0.029994"))      # 0.0299948079 -> down
        self.assertEqual(O.usd_of_units(535000, r), Decimal("0.534907"))


class FeedCache(unittest.TestCase):
    def setUp(self):
        self.ch = T.FakeChain(CONTRACT, "0x" + "01" * 20, "0x" + "aa" * 20)
        self.t = [1000.0]
        self.f = O.Feeds(self.ch, mono=lambda: self.t[0])

    def n_feed_calls(self):
        return sum(1 for a in self.ch.call_targets if a in (USDC_FEED.lower(), HSK_FEED.lower()))

    def test_wrong_pair_or_decimals_is_refused(self):
        self.ch.set_feed(USDC_FEED, "USDT/USD", 1.0)
        self.assertIsNone(self.f.get("USDC/USD"))
        self.ch.set_feed(USDC_FEED, "USDC/USD", 1.0, decimals=18)
        self.t[0] += 60                                  # past the failure back-off
        self.assertIsNone(self.f.get("USDC/USD"))
        self.ch.set_feed(USDC_FEED, "USDC/USD", 1.0)
        self.t[0] += 60
        self.assertIsNotNone(self.f.get("USDC/USD"))

    def test_cached_within_ttl_then_read_again(self):
        feeds(self.ch)
        self.assertIsNotNone(self.f.get("USDC/USD"))
        n = self.n_feed_calls()
        self.assertEqual(n, 3, "decimals + description once, then latestRoundData")
        for _ in range(20):
            self.f.get("USDC/USD")
        self.assertEqual(self.n_feed_calls(), n, "no RPC while the reading is fresh")
        self.t[0] += O.TTL_S + 1
        self.f.get("USDC/USD")
        self.assertEqual(self.n_feed_calls(), n + 1, "one latestRoundData after the TTL (metadata checked once)")

    def test_transient_failure_keeps_reading_validation_failure_drops_it(self):
        feeds(self.ch)
        self.assertIsNotNone(self.f.get("HSK/USD"))
        self.ch.fail_targets.add(HSK_FEED.lower())
        self.t[0] += O.TTL_S + 1
        self.assertIsNotNone(self.f.get("HSK/USD"), "RPC blip: the current reading stays usable")
        self.assertIsNotNone(self.f.cached("HSK/USD"))
        self.ch.fail_targets.clear()
        feeds(self.ch, hsk=-1)                           # the feed now answers garbage
        self.t[0] += O.FAIL_BACKOFF_S + 1
        self.assertIsNone(self.f.get("HSK/USD"))
        self.assertIsNone(self.f.cached("HSK/USD"), "a reading that failed validation is never served")

    def test_failure_backoff_no_rpc_storm(self):
        self.ch.fail_targets.add(USDC_FEED.lower())
        for _ in range(50):
            self.assertIsNone(self.f.get("USDC/USD"))
        self.assertEqual(self.n_feed_calls(), 1, "one attempt per back-off window")

    def test_cached_never_reads_the_chain(self):
        feeds(self.ch)
        self.assertIsNone(self.f.cached("HSK/USD"))
        self.assertEqual(self.ch.call_targets, [])


# ====================================================================== seller: quote USD estimate + gas guard
class QuoteUsd(unittest.TestCase):
    def test_quote_carries_usd_estimate_with_feed_and_round(self):
        env = Env()
        feeds(env.ch)
        st, body, hdr = env.quote()
        self.assertEqual(st, 402)
        u = body["extensions"]["usd-estimate"]
        self.assertEqual(u["priceUsd"], "0.029994")      # 30000 units x 0.99982693
        self.assertEqual(u["feed"]["feed"], USDC_FEED)
        self.assertEqual(u["feed"]["pair"], "USDC/USD")
        self.assertEqual(u["feed"]["roundId"], str((1 << 64) + 100))
        self.assertTrue(u["feed"]["updatedAtIso"].endswith("Z"))
        self.assertEqual(P.parse_b64json(hdr["PAYMENT-REQUIRED"], max_chars=16384), json.loads(json.dumps(body)),
                         "PAYMENT-REQUIRED header == body (the buyer checks this)")
        self.assertIn("hsp-draft", body["extensions"], "HSP offer unchanged")

    def test_no_feed_stale_feed_or_wrong_pair_quote_works_without_estimate(self):
        for setup in (lambda ch: None,
                      lambda ch: feeds(ch, age_s=27 * 3600),
                      lambda ch: ch.set_feed(USDC_FEED, "ETH/USD", 1.0)):
            env = Env()
            setup(env.ch)
            st, body, _ = env.quote()
            self.assertEqual(st, 402)
            self.assertNotIn("usd-estimate", body.get("extensions", {}))
            hdr, (st2, b2, _) = env.buy()
            self.assertEqual(st2, 200, b2)

    def test_feeds_off(self):
        env = Env(price_feeds=False)
        feeds(env.ch)
        st, body, _ = env.quote()
        self.assertNotIn("usd-estimate", body["extensions"])
        self.assertNotIn(USDC_FEED.lower(), env.ch.call_targets)

    def test_paid_request_settles_and_payment_path_never_reads_a_feed(self):
        env = Env()
        feeds(env.ch)
        st, body, _ = env.quote()
        n = len(env.ch.call_targets)
        st, b, _ = env.pay(env.header(body))
        self.assertEqual(st, 200, b)
        self.assertEqual(env.sent(PWA), 1)
        self.assertFalse({USDC_FEED.lower(), HSK_FEED.lower()} & set(env.ch.call_targets[n:]),
                         "settlement uses the cached HSK/USD reading only")


class GasGuard(unittest.TestCase):
    def test_feed_above_floor_tightens_the_guard(self):
        env = Env()
        feeds(env.ch, hsk=0.25)
        env.s.feeds.refresh()
        self.assertEqual(env.s.hsk_usd(), 0.25)
        self.assertEqual(env.s.max_cost_wei(30000), int(30000 * 10**12 / 0.25))

    def test_feed_below_floor_never_loosens_it(self):
        env = Env()
        feeds(env.ch, hsk=0.01)
        env.s.feeds.refresh()
        self.assertEqual(env.s.hsk_usd(), 0.10)
        self.assertEqual(env.s.max_cost_wei(30000), int(30000 * 10**12 / 0.10))

    def test_stale_or_missing_feed_uses_the_floor(self):
        env = Env()
        feeds(env.ch, hsk=5.0, age_s=27 * 3600)
        env.s.feeds.refresh()
        self.assertEqual(env.s.hsk_usd(), 0.10)
        env2 = Env()
        env2.s.feeds.refresh()
        self.assertEqual(env2.s.hsk_usd(), 0.10)

    def test_expensive_hsk_on_the_feed_refuses_settlement_before_sending(self):
        # one settlement (178,123 gas x 500 gwei = 0.089 HSK) at HSK = $1000 costs ~$89 > the $0.03 sale
        env = Env()
        feeds(env.ch, hsk=1000.0)
        st, body, _ = env.quote()
        st, b, _ = env.pay(env.header(body))
        self.assertEqual(st, 503, b)
        self.assertIn("gas cost", b["message"])
        self.assertEqual(env.sent(PWA), 0, "nothing sent")
        self.assertEqual(env.ch.usdce[env.buyer.address.lower()], 10**7, "buyer not charged")


class HttpRoutes(unittest.TestCase):
    def test_health_and_well_known_show_feeds_and_compliance(self):
        env = Env(kyc_min_level=1)
        feeds(env.ch, hsk=0.2)
        env.s.feeds.refresh()
        srv = S.Server(("127.0.0.1", 0), S.make_handler(env.s))
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            h = json.loads(urllib.request.urlopen(base + "/health", timeout=10).read())
            w = json.loads(urllib.request.urlopen(base + "/.well-known/x402", timeout=10).read())
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(h["version"], "0.3.0")
        self.assertEqual(h["priceFeeds"]["HSK/USD"]["price"], "0.2")
        self.assertEqual(h["priceFeeds"]["USDC/USD"]["feed"], USDC_FEED)
        self.assertEqual(h["gasGuardHskUsd"], 0.2)
        self.assertEqual(h["compliance"]["sbt"], K.KYC_SBT)
        self.assertEqual(w["extensions"], ["hsp-draft", "usd-estimate", "kyc-sbt"])


# ====================================================================== buyer: its own USD view
class BuyerUsd(unittest.TestCase):
    def _quote(self, env):
        def http(url, headers, timeout=120):
            import urllib.parse
            u = urllib.parse.urlsplit(url)
            st, body, hdr = env.s.handle_paid(u.path, u.query, dict(headers), "127.0.0.1", {})
            return st, hdr, json.loads(json.dumps(body))
        d = tempfile.mkdtemp(dir=os.path.join(ROOT, "tmp") if os.path.isdir(os.path.join(ROOT, "tmp")) else None)
        return B.quote("http://127.0.0.1:8402" + PROFILE + "?" + PQ, me=env.buyer, rpc=env.ch, contract=CONTRACT,
                       expect_provider=env.provider.address, expect_payee=env.payee, max_units=50000, cap_units=10**6,
                       session_file=os.path.join(d, "s.json"), http=http)

    def test_quote_returns_usd_from_the_buyers_own_feed_read(self):
        env = Env()
        feeds(env.ch)
        q = self._quote(env)
        self.assertEqual(q["guard"], "pass")
        self.assertEqual(q["amountUsd"], "0.029994")
        self.assertEqual(q["usdFeed"]["feed"], USDC_FEED)

    def test_seller_claim_is_not_trusted(self):
        env = Env()
        feeds(env.ch)
        orig = env.s.usd_estimate
        env.s.usd_estimate = lambda units: {**orig(units), "priceUsd": "0.000001"}    # a seller that lies
        q = self._quote(env)
        self.assertEqual(q["amountUsd"], "0.029994", "the buyer computes it from the chain")

    def test_no_feed_quote_still_works(self):
        env = Env()
        q = self._quote(env)
        self.assertEqual(q["guard"], "pass")
        self.assertIsNone(q["amountUsd"])
        self.assertFalse(q["usdFeed"]["available"])


# ====================================================================== KYC SBT compliant mode
class Kyc(unittest.TestCase):
    def env(self, level=1, **kw):
        env = Env(kyc_min_level=level, **kw)
        env.ch.kyc_sbt = K.KYC_SBT
        return env

    def grant(self, env, who, level=1, valid=True, status=1):
        env.ch.kyc[who.address.lower() if hasattr(who, "address") else who.lower()] = {
            "valid": valid, "level": level, "status": status, "createTime": int(time.time()) - 86400}

    def test_off_by_default_no_sbt_reads(self):
        env = Env()
        env.ch.kyc_sbt = K.KYC_SBT
        hdr, (st, b, _) = env.buy()
        self.assertEqual(st, 200)
        self.assertNotIn(K.KYC_SBT.lower(), env.ch.call_targets)
        st, body, _ = env.quote()
        self.assertNotIn("kyc-sbt", body["extensions"])

    def test_payer_without_sbt_refused_at_quote_for_free(self):
        env = self.env()
        st, b, _ = env.quote()
        self.assertEqual((st, b["error"]), (403, "kyc_required"))
        self.assertIn("no KYC SBT", b["message"])
        self.assertIn("nothing was charged", b["message"])
        self.assertEqual(b["kyc"]["sbt"], K.KYC_SBT)
        self.assertNotIn("accepts", b, "no quote, no token")
        self.assertEqual(env.ch.sent, [])

    def test_verified_payer_quote_says_so_and_pays(self):
        env = self.env()
        self.grant(env, env.buyer, level=1)
        st, body, _ = env.quote()
        self.assertEqual(st, 402)
        self.assertEqual(body["extensions"]["kyc-sbt"]["minLevel"], 1)
        st, b, _ = env.pay(env.header(body))
        self.assertEqual(st, 200, b)
        self.assertEqual(env.sent(PWA), 1)

    def test_level_revoked_and_expired_reasons(self):
        cases = [({"level": 1}, 2, "below the required ADVANCED"),
                 ({"level": 3, "valid": False, "status": 2}, 1, "revoked"),
                 ({"level": 4, "valid": False, "status": 1}, 1, "not valid any more")]
        for grant, need, why in cases:
            with self.subTest(why):
                env = self.env(level=need)
                self.grant(env, env.buyer, **grant)
                st, b, _ = env.quote()
                self.assertEqual((st, b["error"]), (403, "kyc_required"))
                self.assertIn(why, b["message"])

    def test_sbt_revoked_between_quote_and_payment_refused_before_settling(self):
        env = self.env()
        env.s.kyc.ttl = 0
        self.grant(env, env.buyer)
        st, body, _ = env.quote()
        self.assertEqual(st, 402)
        self.grant(env, env.buyer, valid=False, status=2)
        st, b, _ = env.pay(env.header(body))
        self.assertEqual((st, b["error"]), (403, "kyc_required"))
        self.assertEqual(env.sent(PWA), 0, "no settlement sent")
        self.assertEqual(env.ch.usdce[env.buyer.address.lower()], 10**7)

    def test_self_paid_payment_with_lapsed_sbt_is_refunded(self):
        env = self.env()
        env.s.kyc.ttl = 0
        self.grant(env, env.buyer)
        st, body, _ = env.quote()
        x = body["accepts"][1]["extra"]
        # the buyer's own pay() transaction, as the chain records it
        env.ch.payments[x["requestId"]] = {"payer": env.buyer.address.lower(), "endpointId": 1, "refunded": False,
                                           "amount": 30000, "paidAt": int(time.time()), "paidBlock": env.ch.block}
        env.ch.usdce[env.buyer.address.lower()] -= 30000
        env.ch.credit += 30000
        env.ch.logs.append({"address": CONTRACT, "logIndex": "0x0", "blockNumber": hex(env.ch.block), "transactionHash": "0x" + "ab" * 32,
                            "topics": [C.PAID_TOPIC, "0x%064x" % 1, x["requestId"], "0x" + "00" * 12 + env.buyer.address.lower()[2:]],
                            "data": "0x" + encode(["address", "uint256"], [env.payee, 30000]).hex()})
        self.grant(env, env.buyer, valid=False, status=2)
        st, b, _ = env.s.handle_paid(PROFILE, PQ, {"X-Payer": env.buyer.address, "X-Paywall-Token": x["token"]}, "1.1.1.1", {})
        self.assertEqual((st, b["error"]), (403, "kyc_required"), b)
        self.assertTrue(b["refund"]["refunded"], b)
        self.assertEqual(env.sent(REFUND), 1)
        self.assertEqual(env.ch.usdce[env.buyer.address.lower()], 10**7, "refunded in full")

    def test_unreadable_sbt_fails_closed(self):
        env = self.env()
        self.grant(env, env.buyer)
        env.ch.fail_targets.add(K.KYC_SBT.lower())
        st, b, _ = env.quote()
        self.assertEqual((st, b["error"]), (502, "chain_unavailable"))
        self.assertNotIn("accepts", b)

    def test_level_bounds(self):
        with self.assertRaises(ValueError):
            K.KycGate(None, 0)
        with self.assertRaises(ValueError):
            K.KycGate(None, 5)


# ====================================================================== live, read-only (--online)
@unittest.skipUnless(ONLINE, "read-only mainnet checks: run with --online")
class Online(unittest.TestCase):
    def setUp(self):
        self.rpc = C.Rpc("https://mainnet.hsk.xyz")

    def test_apro_feeds_on_mainnet(self):
        for pair in O.FEEDS:
            r = O.read_checked(self.rpc, pair)
            print(f"\n     {pair} {r['price']} round {r['roundId']} updated {O.public(r)['updatedAtIso']}", end="")

    def test_kyc_sbt_on_mainnet(self):
        code = self.rpc("eth_getCode", [K.KYC_SBT, "latest"])
        self.assertGreater(len(code), 2)
        name = C.eth_call(self.rpc, K.KYC_SBT, "name()", [], [], ["string"])[0]
        self.assertEqual(name, "KYC SBT")
        self.assertEqual(K.is_human(self.rpc, "0x" + "00" * 19 + "01"), (False, 0))
        self.assertEqual(K.kyc_info(self.rpc, "0x" + "00" * 19 + "01")["status"], 0)
        ok, detail = K.KycGate(self.rpc, 1).check("0x" + "00" * 19 + "01")
        self.assertEqual((ok, detail["reason"]), (False, "no KYC SBT for this address"))


if __name__ == "__main__":
    unittest.main()
