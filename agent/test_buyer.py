#!/usr/bin/env python3
"""Tests for the buyer's spend guard (finding 10): the cap counts a payment BEFORE it is signed, a reservation is
released only when the chain proves nothing was charged, and endpoints must belong to the pinned provider/payee.
Phase 2B review: buy() never raises once it signed (B2B-1), seller tx hashes are checked before any RPC call, queued
refunds are credited back by reconcile (B2B-2), the payments-per-minute limit lives in the ledger (MCP-4).
Offline (server/testkit.FakeChain); journals go to a temp dir.

  test_buyer.py [-v]
"""
import importlib
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
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, HERE)
import buyer as B        # noqa: E402
import protocol as P     # noqa: E402
import seller as S       # noqa: E402
import chain as C        # noqa: E402
import testkit as T      # noqa: E402
from eth_account import Account  # noqa: E402

CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"


def seller_http(seller, after=None):
    """In-process HTTP for buy(): calls the real seller's handler (no sockets). `after(st, hdr, body)` post-processes
    the paid request's answer (e.g. raises to simulate a lost response)."""
    def http(url, headers, timeout=120):
        u = urllib.parse.urlsplit(url)
        st, body, hdr = seller.handle_paid(u.path, u.query, dict(headers), "127.0.0.1", {})
        body = json.loads(json.dumps(body))
        if after and "PAYMENT-SIGNATURE" in headers:
            return after(st, hdr, body)
        return st, hdr, body
    return http


def totals(path):
    """(spent, reserved) units in a session ledger."""
    with B.Session(path)._ledger() as d:
        return d["spent_units"], sum(r["amount"] for r in d["reserved"].values())


class NoRpc:
    """An RPC that must not be called."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, params, tries=1):
        self.calls.append(method)
        raise AssertionError("unexpected RPC call " + method)


class DownRpc:
    def __call__(self, method, params, tries=1):
        raise C.RpcError("RPC error: rate limit exceeded")


class BuyerGuard(unittest.TestCase):
    def setUp(self):
        base = os.path.join(ROOT, "tmp")
        self.d = tempfile.mkdtemp(dir=base if os.path.isdir(base) else None)
        self.sess = B.Session(os.path.join(self.d, "session.json"))
        self.provider, self.me = Account.create(), Account.create()
        self.payee = "0x" + "aa" * 20
        self.ch = T.FakeChain(CONTRACT, self.provider.address, self.payee)
        self.ch.fund(self.me.address, 10**7)

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_cap_counts_reservations_before_signing(self):
        self.sess.reserve("0x" + "01" * 32, 30000, 50000, "exact", "0x" + "11" * 32, time.time() + 120)
        with self.assertRaises(B.Refused):          # second signature would exceed the cap while the first is open
            self.sess.reserve("0x" + "02" * 32, 30000, 50000, "exact", "0x" + "22" * 32, time.time() + 120)
        self.assertEqual(self.sess.total(), 30000)
        with self.assertRaises(B.Refused):
            self.sess.reserve("0x" + "01" * 32, 1, 10**9, "exact", "0x" + "11" * 32, time.time() + 120)

    def _quote_and_auth(self, valid_s):
        s = S.Seller({"contract": CONTRACT, "tx_wait_s": 0.3}, rpc=self.ch, provider=self.provider,
                     adapter=Account.create(), token_key=os.urandom(32))
        st, body, _ = s.handle_paid("/v1/hsk/address-profile", "address=0x" + "01" * 20, {"X-Payer": self.me.address}, "1.1.1.1", {})
        self.assertEqual(st, 402)
        x = body["accepts"][0]["extra"]
        return s, body, x["requestId"], x["nonce"], int(time.time()) + valid_s

    def test_reservation_released_only_when_chain_proves_unused(self):
        s, body, rid, nonce, vb = self._quote_and_auth(120)
        self.sess.reserve(rid, 30000, 10**6, "exact", nonce, vb)
        self.assertEqual(self.sess.reconcile(self.ch, CONTRACT, self.me.address), [], "not expired: kept")
        later = vb + 31
        self.ch.auth_used.add((self.me.address.lower(), nonce))   # used (e.g. paid, not yet visible) -> kept
        out = self.sess.reconcile(self.ch, CONTRACT, self.me.address, now=later)
        self.assertEqual(out[0][1].split(":")[0], "kept")
        self.ch.auth_used.clear()
        out = self.sess.reconcile(self.ch, CONTRACT, self.me.address, now=later)
        self.assertEqual(out, [(rid, "released")])
        self.assertEqual(self.sess.total(), 0)

    def test_unverified_but_paid_reservation_becomes_spent(self):
        s, body, rid, nonce, vb = self._quote_and_auth(300)
        self.sess.reserve(rid, 30000, 10**6, "exact", nonce, vb)
        acc = body["accepts"][0]
        sig = T.sign_auth(self.me, acc["payTo"], 30000, 0, vb, nonce)
        from eth_abi import encode
        import chain as C
        data = C.selector("payWithAuthorization(uint256,bytes32,address,uint256,uint256,uint256,bytes)") + encode(
            ["uint256", "bytes32", "address", "uint256", "uint256", "uint256", "bytes"],
            [1, bytes.fromhex(rid[2:]), self.me.address, 30000, 0, vb, bytes.fromhex(sig[2:])])
        self.ch.submit_direct("0x" + "99" * 20, data)              # the server settled, but its answer was lost
        out = self.sess.reconcile(self.ch, CONTRACT, self.me.address, now=vb + 31)
        self.assertEqual(out, [(rid, "paid")])
        self.assertEqual(self.sess.total(), 30000)
        self.assertEqual(self.sess.refunded(rid), 0, "a verified refund gives the amount back")

    def test_commit_and_refund(self):
        self.sess.reserve("0x" + "03" * 32, 25000, 10**6, "exact", "0x" + "33" * 32, time.time() + 120)
        self.assertEqual(self.sess.commit("0x" + "03" * 32), 25000)
        self.assertEqual(self.sess.refunded("0x" + "03" * 32), 0)
        self.assertEqual(self.sess.refunded("0x" + "03" * 32), 0, "a refund is counted once")

    def test_pinned_provider_and_payee(self):
        s, body, rid, nonce, vb = self._quote_and_auth(120)
        url = "http://127.0.0.1:8402/v1/hsk/address-profile?address=0x" + "01" * 20
        args = (self.ch, body, url, self.me.address, CONTRACT, 10**6, 10**6, "exact", self.sess)
        B.check_quote(*args, self.provider.address, self.payee)    # positive control
        with self.assertRaises(B.Refused):
            B.check_quote(*args, "0x" + "12" * 20, self.payee)
        with self.assertRaises(B.Refused):
            B.check_quote(*args, self.provider.address, "0x" + "34" * 20)

    def test_corrupt_ledger_refuses_to_spend(self):
        with open(self.sess.path, "w") as f:
            f.write("{not json")
        with self.assertRaises(B.Refused):
            self.sess.total()

    def test_old_ledger_format_is_read(self):
        with open(self.sess.path, "w") as f:
            json.dump({"spent_units": 25000}, f)
        self.assertEqual(self.sess.total(), 25000)



class AfterSigning(unittest.TestCase):
    """B2B-1 (CLI side of MCP-1), B2B-2 and MCP-4 at the buyer/ledger level."""

    def setUp(self):
        base = os.path.join(ROOT, "tmp")
        self.d = tempfile.mkdtemp(dir=base if os.path.isdir(base) else None)
        self.session = os.path.join(self.d, "session.json")
        self.journal = os.path.join(self.d, "runs.jsonl")
        self.provider, self.me = Account.create(), Account.create()
        self.payee = "0x" + "aa" * 20
        self.ch = T.FakeChain(CONTRACT, self.provider.address, self.payee)
        self.ch.fund(self.me.address, 10**7)
        self.saved = (getattr(B, "RECEIPT_POLLS", None), getattr(B, "RECEIPT_WAIT_S", None))
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = 2, 0          # no 40 s receipt polling in tests

    def tearDown(self):
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = self.saved
        shutil.rmtree(self.d)

    def test_verification_is_unknown_not_an_exception_when_the_chain_is_down(self):
        rid, tx = "0x" + "01" * 32, "0x" + "02" * 32
        ok, why = B.verify_paid(DownRpc(), CONTRACT, tx, rid, self.me.address, 30000, 1)
        self.assertIsNone(ok, why)
        ok, why = B.verify_refund(DownRpc(), CONTRACT, tx, rid, self.me.address, 30000, wait_s=0)
        self.assertIsNone(ok, why)

    def test_malformed_seller_tx_hash_is_never_looked_up(self):
        rpc = NoRpc()
        for bad in ("0x1234", "0x" + "zz" * 32, "x" * 5000, None, 42):
            ok, why = B.verify_paid(rpc, CONTRACT, bad, "0x" + "01" * 32, self.me.address, 30000, 1)
            self.assertIsNone(ok)
            ok, why = B.verify_refund(rpc, CONTRACT, bad, "0x" + "01" * 32, self.me.address, 30000, wait_s=0)
            self.assertIsNone(ok)
        self.assertEqual(rpc.calls, [])

    def test_buy_never_raises_after_signing_and_a_retry_resends(self):
        s = S.Seller({"contract": CONTRACT, "tx_wait_s": 0.3}, rpc=self.ch, provider=self.provider,
                     adapter=Account.create(), token_key=os.urandom(32))
        url = "http://127.0.0.1:8402/v1/hsk/address-profile?address=0x" + "01" * 20
        kw = dict(me=self.me, rpc=self.ch, contract=CONTRACT, expect_provider=self.provider.address,
                  expect_payee=self.payee, max_units=50_000, cap_units=200_000, session_file=self.session,
                  journal_file=self.journal)

        def lost(st, hdr, body):
            raise ConnectionResetError("connection reset by peer")    # settled + served, the answer is lost
        out = B.buy(url, http=seller_http(s, after=lost), **kw)
        self.assertEqual((out["payment"], out["delivered"]), ("signed_unconfirmed", False))
        self.assertIn("ConnectionResetError", out["post_payment_error"])
        self.assertEqual(self.ch.usdce[self.me.address.lower()], 10**7 - 30000, "the seller settled it")
        self.assertEqual(B.pending_resend(self.session, self.me.address, url), out["requestId"])
        with open(self.journal) as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual([x["payment"] for x in rows], ["signed_unconfirmed"], "journaled to the given file")
        again = B.buy(url, http=seller_http(s), **kw)
        self.assertTrue(again["resent"])
        self.assertEqual((again["requestId"], again["payment"], again["delivered"]), (out["requestId"], "settled", True))
        self.assertEqual(self.ch.usdce[self.me.address.lower()], 10**7 - 30000, "charged once")
        self.assertEqual(totals(self.session), (30000, 0))
        self.assertIsNone(B.pending_resend(self.session, self.me.address, url))

    def test_tx_scheme_pay_that_was_never_sent_is_not_charged(self):
        s = S.Seller({"contract": CONTRACT, "tx_wait_s": 0.3}, rpc=self.ch, provider=self.provider,
                     adapter=Account.create(), token_key=os.urandom(32))
        allowance = C.selector("allowance(address,address)").hex()

        def rpc(method, params, tries=1):     # allowance is enough; FakeChain's simulation then rejects pay()
            if method == "eth_call" and params[0]["data"][2:].startswith(allowance):
                return "0x" + "ff" * 32
            return self.ch(method, params, tries)
        url = "http://127.0.0.1:8402/v1/hsk/address-profile?address=0x" + "01" * 20
        out = B.buy(url, me=self.me, rpc=rpc, contract=CONTRACT, expect_provider=self.provider.address,
                    expect_payee=self.payee, max_units=50_000, cap_units=200_000, scheme="tx",
                    session_file=self.session, journal_file=self.journal, http=seller_http(s))
        self.assertEqual((out["payment"], out["delivered"]), ("not_charged", False))
        self.assertIn("RpcError", out["post_payment_error"])
        self.assertEqual(self.ch.usdce[self.me.address.lower()], 10**7)
        self.assertEqual(totals(self.session), (0, 30000), "kept until the reconciler proves it unpaid")

    def test_queued_refund_is_credited_back_by_reconcile(self):
        sess = B.Session(self.session)
        rid = "0x" + self.me.address[2:].lower() + "33" * 12
        sess.reserve(rid, 30000, 10**6, "exact", "0x" + "33" * 32, time.time() + 120)
        sess.commit(rid)
        sess.refund_pending(rid, 30000, None)
        self.ch.payments[rid] = {"payer": self.me.address.lower(), "endpointId": 1, "refunded": False, "amount": 30000,
                                 "paidAt": int(time.time()), "paidBlock": self.ch.block}
        self.assertEqual(sess.reconcile(self.ch, CONTRACT, self.me.address), [], "not refunded yet: still spent")
        self.assertEqual(sess.total(), 30000)
        self.ch.payments[rid]["refunded"] = True                         # the seller's reconciler refunds it later
        later = time.time() + 30 * 86400                                  # even long after the watch window
        self.assertEqual(sess.reconcile(self.ch, CONTRACT, self.me.address, now=later), [(rid, "refunded")])
        self.assertEqual(sess.total(), 0)
        self.assertEqual(sess.reconcile(self.ch, CONTRACT, self.me.address), [], "credited once")
        st = B.status(me=self.me, rpc=self.ch, cap_units=10**6, session_file=self.session, contract=CONTRACT)
        self.assertEqual((st["spentUnits"], st["refundsWatched"]), (0, 0))
        rid2 = "0x" + self.me.address[2:].lower() + "34" * 12          # a refund that never lands: dropped, stays spent
        sess.reserve(rid2, 30000, 10**6, "exact", "0x" + "34" * 32, time.time() + 120)
        sess.commit(rid2)
        sess.refund_pending(rid2, 30000, None)
        self.assertEqual(sess.reconcile(self.ch, CONTRACT, self.me.address, now=later),
                         [(rid2, "refund never seen on-chain; stays spent")])
        self.assertEqual(sess.total(), 30000)

    def test_reconcile_keeps_entries_when_the_chain_is_down(self):
        sess = B.Session(self.session)
        sess.reserve("0x" + "04" * 32, 30000, 10**6, "exact", "0x" + "44" * 32, time.time() - 1000)
        self.assertEqual(sess.reconcile(DownRpc(), CONTRACT, self.me.address), [("0x" + "04" * 32, "kept: chain unavailable")])
        self.assertEqual(sess.total(), 30000, "fail closed")

    def _buy_kw(self):
        s = S.Seller({"contract": CONTRACT, "tx_wait_s": 0.3}, rpc=self.ch, provider=self.provider,
                     adapter=Account.create(), token_key=os.urandom(32))
        url = "http://127.0.0.1:8402/v1/hsk/address-profile?address=0x" + "01" * 20
        kw = dict(me=self.me, rpc=self.ch, contract=CONTRACT, expect_provider=self.provider.address,
                  expect_payee=self.payee, max_units=50_000, cap_units=200_000, session_file=self.session,
                  journal_file=self.journal)
        return s, url, kw

    def test_pending_resend_survives_a_new_process_and_a_crash_mid_request(self):
        s, url, kw = self._buy_kw()

        def crash(st, hdr, body):
            raise KeyboardInterrupt()            # the process dies after the seller settled and served
        with self.assertRaises(KeyboardInterrupt):
            B.buy(url, http=seller_http(s, after=crash), **kw)
        importlib.reload(B)                      # a new process: nothing kept in memory
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = 2, 0
        again = B.buy(url, http=seller_http(s), **kw)
        self.assertEqual((again.get("resent"), again["payment"], again["delivered"]), (True, "settled", True))
        self.assertEqual(self.ch.usdce[self.me.address.lower()], 10**7 - 30000, "charged once")
        self.assertEqual(totals(self.session), (30000, 0))
        self.assertEqual(oct(os.stat(self.session).st_mode & 0o777), "0o600")

    def test_expired_or_released_resend_is_dropped(self):
        s, url, kw = self._buy_kw()
        out = B.buy(url, http=seller_http(s, after=lambda st, hdr, body: (502, {}, {"raw": "bad gateway"})), **kw)
        self.assertTrue(out["resend_on_retry"])
        sess = B.Session(self.session)
        self.assertEqual(len(sess.resends(self.me.address, now=out["resend_until"])), 1)
        self.assertEqual(sess.resends(self.me.address, now=out["resend_until"] + 1), [], "kept no longer than its expiry")
        rid, nonce = "0x" + "07" * 32, "0x" + "77" * 32  # a signed payment the chain later proves unused
        sess.reserve(rid, 1, 10**6, "exact", nonce, time.time() - 100)
        sess.resend_put("k", url, {"rid": rid, "eid": 1, "amount": 1, "scheme": "exact", "headers": {"X-Payer": "x"},
                                   "until": time.time() + 600})
        self.assertIn((rid, "released"), sess.reconcile(self.ch, CONTRACT, self.me.address))
        self.assertIsNone(sess.resend_get("k"), "never charged: nothing left to resend")

    def test_malformed_resend_entry_fails_closed(self):
        s, url, kw = self._buy_kw()
        B.buy(url, http=seller_http(s, after=lambda st, hdr, body: (503, {}, {"raw": "x"})), **kw)
        with B.Session(self.session)._ledger() as d:
            next(iter(d["resend"].values()))["headers"] = "not a dict"
        with self.assertRaises(B.Refused):
            B.buy(url, http=seller_http(s), **kw)
        self.assertEqual(self.ch.usdce[self.me.address.lower()], 10**7 - 30000, "nothing new was signed or paid")

    def test_already_served_keeps_the_resend_and_final_answers_drop_it(self):
        """Round 3 variant 2: a 502 'already served; not refunded' (refund status none) is resent on retry; a verified,
        pending or queued refund, 429 already_served and 410 payment_expired end it."""
        ctx = {"headers": {"X-Payer": "x"}}

        def keep(st, res, **out):
            return B._outcome_unknown({"delivered": False, "payment": "settled", **out}, ctx, st, res)
        body = {"ok": False, "error": "not_delivered", "message": "upstream data unavailable"}
        self.assertTrue(keep(502, body, refund={"status": "none", "note": "this payment was already served"}))
        for status in ("verified", "pending", "queued"):
            self.assertFalse(keep(502, body, refund={"status": status}), status)
        self.assertFalse(keep(429, {"ok": False, "error": "already_served", "message": "served 3 times already"}))
        self.assertFalse(keep(410, {"ok": False, "error": "payment_expired", "message": "paid more than 1 h ago"}))
        self.assertTrue(keep(409, {"ok": False, "error": "in_progress", "retryable": True}))
        self.assertTrue(keep(None, None))                                # no answer at all
        self.assertFalse(keep(200, {"data": 1}, delivered=True))

    def test_delivered_payment_is_re_served_within_the_window(self):
        """Round 3 variant 1 (CLI side): a same-URL buy() right after a delivery resends that payment (the seller
        re-serves it for free) unless new_purchase; the entry is kept DELIVERED_KEEP_S at most."""
        s, url, kw = self._buy_kw()
        out = B.buy(url, http=seller_http(s), **kw)
        self.assertTrue(out["delivered"])
        self.assertEqual(B.pending_resend(self.session, self.me.address, url, delivered=True), out["requestId"])
        self.assertIsNone(B.pending_resend(self.session, self.me.address, url), "not an unknown outcome")
        again = B.buy(url, http=seller_http(s), **kw)
        self.assertEqual((again.get("resent"), again.get("redelivery"), again["delivered"], again["requestId"]),
                         (True, True, True, out["requestId"]))
        self.assertEqual(self.ch.usdce[self.me.address.lower()], 10**7 - 30000, "charged once")
        self.assertEqual(totals(self.session), (30000, 0))
        sess = B.Session(self.session)
        rows = sess.resends(self.me.address, delivered=True)
        self.assertEqual([x["requestId"] for x in rows], [out["requestId"]])
        self.assertLessEqual(rows[0]["until"], time.time() + B.DELIVERED_KEEP_S)
        self.assertEqual(sess.resends(self.me.address, now=rows[0]["until"] + 1, delivered=True), [], "10 min at most")
        new = B.buy(url, http=seller_http(s), new_purchase=True, **kw)
        self.assertNotEqual(new["requestId"], out["requestId"])
        self.assertEqual(self.ch.usdce[self.me.address.lower()], 10**7 - 60000, "new_purchase pays again")

    def test_receipt_polling_stops_at_the_budget_or_a_cancel(self):
        """Round 3: buy() is bounded; a poll cut short is 'unknown' (None), never 'missing' (False)."""
        class NoReceipt:
            def __call__(self, method, params, tries=1):
                return None
        B.RECEIPT_POLLS, B.RECEIPT_WAIT_S = 20, 2                         # 38 s of polling without a budget
        cancelled = threading.Event()
        cancelled.set()
        for budget in (B.Budget(0.5), B.Budget(None, cancelled)):
            t0 = time.time()
            ok, why = B.verify_paid(NoReceipt(), CONTRACT, "0x" + "02" * 32, "0x" + "01" * 32, self.me.address,
                                    30000, 1, budget=budget)
            self.assertIsNone(ok, why)
            self.assertLess(time.time() - t0, 1.5)

    def test_pays_per_minute_is_kept_in_the_ledger(self):
        a, b = B.Session(self.session), B.Session(self.session)     # two processes on one ledger
        a.reserve("0x" + "05" * 32, 1, 10**6, "exact", None, time.time() + 120, pays_per_minute=1)
        with self.assertRaises(B.Refused) as cm:
            b.reserve("0x" + "06" * 32, 1, 10**6, "exact", None, time.time() + 120, pays_per_minute=1)
        self.assertIn("rate limit", str(cm.exception))
        b.reserve("0x" + "06" * 32, 1, 10**6, "exact", None, time.time() + 120, pays_per_minute=1, now=time.time() + 61)
        self.assertEqual(a.total(), 2)


if __name__ == "__main__":
    unittest.main()
