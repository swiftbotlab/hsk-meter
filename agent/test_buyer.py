#!/usr/bin/env python3
"""Tests for the buyer's spend guard (finding 10): the cap counts a payment BEFORE it is signed, a reservation is
released only when the chain proves nothing was charged, and endpoints must belong to the pinned provider/payee.
Offline (server/testkit.FakeChain).

  test_buyer.py [-v]
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, HERE)
import buyer as B        # noqa: E402
import protocol as P     # noqa: E402
import seller as S       # noqa: E402
import testkit as T      # noqa: E402
from eth_account import Account  # noqa: E402

CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"


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


if __name__ == "__main__":
    unittest.main()
