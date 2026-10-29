#!/usr/bin/env python3
"""Regression tests for the 2026-10-23 security review of the seller (findings 1-8, 11-13). Offline: the seller runs
against testkit.FakeChain, which executes the real signed transactions with the contract's and USDC.e's rules.

  test_security.py [-v]
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chain as C        # noqa: E402
import hsp_draft as HSP  # noqa: E402
import protocol as P     # noqa: E402
import seller as S       # noqa: E402
import state as ST       # noqa: E402
import testkit as T      # noqa: E402
from eth_abi import encode  # noqa: E402
from eth_account import Account  # noqa: E402

CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"
PROFILE, PQ = "/v1/hsk/address-profile", "address=0x" + "01" * 20
TXR = "/v1/hsk/tx-receipt"
PWA = T.S_PWA
REFUND = T.S_REFUND


class Env:
    def __init__(self, state_dir=None, prices=None, provider_hsk=10 * 10**18, **cfg):
        self.provider, self.adapter, self.buyer = Account.create(), Account.create(), Account.create()
        self.payee = "0x" + "aa" * 20
        self.ch = T.FakeChain(CONTRACT, self.provider.address, self.payee, prices, provider_hsk=provider_hsk)
        self.ch.fund(self.buyer.address, 10**7)
        self.state_dir = state_dir
        self.token_key = os.urandom(32)
        self.cfg = {"contract": CONTRACT, "state_dir": state_dir, "tx_wait_s": 0.3, **cfg}
        self.s = self.new_seller()

    def new_seller(self):
        return S.Seller(self.cfg, rpc=self.ch, provider=self.provider, adapter=self.adapter, token_key=self.token_key)

    def quote(self, path=PROFILE, q=PQ, payer=None, ip="1.1.1.1"):
        return self.s.handle_paid(path, q, {"X-Payer": (payer or self.buyer).address}, ip, {})

    def header(self, body, signer=None, frm=None, value=None, vb=None, mandate_caps=None, deadline=None, sig=None,
               mutate=None):
        acc = json.loads(json.dumps(body["accepts"][0]))    # never mutate the caller's quote
        x = acc["extra"]
        signer = signer or self.buyer
        frm = frm or signer.address
        v = int(acc["amount"]) if value is None else value
        vb = vb or int(time.time()) + 300
        sig = sig or T.sign_auth(signer, acc["payTo"], v, 0, vb, x["nonce"])
        payload = {"x402Version": 2, "accepted": acc, "payload": {"signature": sig, "authorization": {
            "from": frm, "to": acc["payTo"], "value": str(v), "validAfter": "0", "validBefore": str(vb), "nonce": x["nonce"]}}}
        if mandate_caps is not None:
            m = HSP.mandate_message(payer=frm, payee=self.payee, token=P.USDCE, amount=v, reference=x["requestId"],
                                    required_caps=mandate_caps, deadline=deadline or int(time.time()) + 900)
            payload["extensions"] = {"hsp-draft": {"mandate": HSP.mandate_to_json(m), "signature": HSP.sign_mandate(signer, m, 177)}}
        if mutate:
            mutate(payload)
        return P.b64json(payload)

    def pay(self, hdr, path=PROFILE, q=PQ, ip="1.1.1.1"):
        return self.s.handle_paid(path, q, {"PAYMENT-SIGNATURE": hdr}, ip, {})

    def buy(self, path=PROFILE, q=PQ, ip="1.1.1.1", **kw):
        st, body, _ = self.quote(path, q, payer=kw.get("signer"), ip=ip)
        assert st == 402, (st, body)
        hdr = self.header(body, **kw)
        return hdr, self.pay(hdr, path, q, ip)

    def sent(self, selector):
        return self.ch.sent.count(selector)


def pwa_calldata(env, body, signer, vb):
    acc = body["accepts"][0]
    x = acc["extra"]
    sig = T.sign_auth(signer, acc["payTo"], int(acc["amount"]), 0, vb, x["nonce"])
    return C.selector("payWithAuthorization(uint256,bytes32,address,uint256,uint256,uint256,bytes)") + encode(
        ["uint256", "bytes32", "address", "uint256", "uint256", "uint256", "bytes"],
        [int(x["endpointId"]), bytes.fromhex(x["requestId"][2:]), signer.address, int(acc["amount"]), 0, vb, bytes.fromhex(sig[2:])])


class F1SettlementDoS(unittest.TestCase):
    def test_forged_signature_refused_locally_no_rpc_no_limiter(self):
        env = Env()
        st, body, _ = env.quote()
        n_calls = len(env.ch.calls)
        st, b, _ = env.pay(env.header(body, sig="0x" + "11" * 65))
        self.assertEqual((st, b["error"]), (402, "bad_signature"))
        self.assertEqual(env.ch.calls[n_calls:], [], "no RPC call at all for a forged signature")
        self.assertEqual(env.s.settle_rl.b, {}, "the global settlement limiter was not touched")

    def test_signature_by_someone_else_and_malleated_signature_refused(self):
        env = Env()
        st, body, _ = env.quote()
        other = Account.create()
        acc, x = body["accepts"][0], body["accepts"][0]["extra"]
        vb = int(time.time()) + 300
        st, b, _ = env.pay(env.header(body, sig=T.sign_auth(other, acc["payTo"], int(acc["amount"]), 0, vb, x["nonce"]), vb=vb))
        self.assertEqual(b["error"], "bad_signature")
        good = bytes.fromhex(T.sign_auth(env.buyer, acc["payTo"], int(acc["amount"]), 0, vb, x["nonce"])[2:])
        s_ = int.from_bytes(good[32:64], "big")
        high = good[:32] + (P.SECP256K1_N - s_).to_bytes(32, "big") + bytes([55 - good[64]])   # same signer, high-s form
        st, b, _ = env.pay(env.header(body, sig="0x" + high.hex(), vb=vb))
        self.assertEqual(b["error"], "bad_signature", "high-s form is refused like FiatToken does")
        self.assertEqual(env.sent(PWA), 0)

    def test_contract_or_delegated_payer_refused_before_simulation(self):
        env = Env()
        env.ch.codes[env.buyer.address.lower()] = "0xef0100" + "22" * 20        # EIP-7702 delegated account
        hdr, (st, b, _) = env.buy()
        self.assertEqual((st, b["error"]), (402, "payer_not_eoa"))
        self.assertNotIn("eth_estimateGas", env.ch.calls)
        self.assertEqual(env.sent(PWA), 0)

    def test_unfunded_payer_and_used_nonce_refused_before_simulation(self):
        env = Env()
        poor = Account.create()
        st, body, _ = env.quote(payer=poor)
        st, b, _ = env.pay(env.header(body, signer=poor))
        self.assertEqual((st, b["error"]), (402, "insufficient_funds"))
        st, body, _ = env.quote()
        nonce = body["accepts"][0]["extra"]["nonce"]
        env.ch.auth_used.add((env.buyer.address.lower(), nonce))              # canceled via cancelAuthorization
        st, b, _ = env.pay(env.header(body))
        self.assertEqual((st, b["error"]), (402, "authorization_used"))
        self.assertNotIn("eth_estimateGas", env.ch.calls)

    def test_attacker_cannot_starve_honest_buyer(self):
        env = Env(settle_per_min=12)
        attacker = Account.create()
        for i in range(13):                       # forged signatures
            st, body, _ = env.quote(payer=attacker, ip="6.6.6.6")
            env.pay(env.header(body, signer=attacker, sig="0x" + "22" * 65), ip="6.6.6.6")
        for i in range(13):                       # valid signatures from fresh unfunded keys
            k = Account.create()
            st, body, _ = env.quote(payer=k, ip="7.7.7.%d" % i)
            env.pay(env.header(body, signer=k), ip="7.7.7.%d" % i)
        hdr, (st, b, _) = env.buy(ip="9.9.9.9")
        self.assertEqual(st, 200, b)
        self.assertEqual(env.sent(PWA), 1)

    def test_per_payer_limit(self):
        env = Env(payer_settle_per_min=3)
        codes = [env.buy(ip="1.1.1.%d" % i)[1][0] for i in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        self.assertEqual(env.sent(PWA), 3)

    def test_ban_24h_after_onchain_revert(self):
        env = Env()

        def front_run(call):                      # the payer empties its wallet between simulation and execution
            env.ch.usdce[env.buyer.address.lower()] = 0
        env.ch.pre_execute = front_run
        hdr, (st, b, _) = env.buy()
        self.assertEqual((st, b["error"]), (402, "settlement_failed"))
        env.ch.pre_execute = None
        env.ch.fund(env.buyer.address, 10**7)
        hdr, (st, b, h) = env.buy(ip="2.2.2.2")
        self.assertEqual((st, b["error"]), (403, "payer_suspended"))
        self.assertGreater(int(h["Retry-After"]), 86000)
        self.assertEqual(env.sent(PWA), 1)


class F2GasDrain(unittest.TestCase):
    def test_unknown_tx_hash_is_404_at_quote(self):
        env = Env()
        st, b, _ = env.quote(TXR, "hash=0x" + "ab" * 32)
        self.assertEqual((st, b["error"]), (404, "not_found"))
        self.assertEqual(env.ch.sent, [])

    def test_tx_hash_checked_again_before_settle(self):
        env = Env()
        h = "0x" + "cd" * 32
        env.ch.add_external_tx(h)
        st, body, _ = env.quote(TXR, "hash=" + h)
        self.assertEqual(st, 402)
        del env.ch.receipts[h]                    # gone (reorg / lying node) between quote and payment
        env.s.exists.clear()
        st, b, _ = env.pay(env.header(body), TXR, "hash=" + h)
        self.assertEqual((st, b["error"]), (404, "not_found"))
        self.assertEqual(env.ch.sent, [], "nothing settled, nothing refunded")

    def test_simulation_carries_real_gas_and_fee_fields(self):
        calls = []
        ch = T.FakeChain(CONTRACT, "0x" + "00" * 20, "0x" + "aa" * 20)

        def rpc(method, params, tries=1):
            calls.append((method, params))
            return ch(method, params, tries)
        acct = Account.create()
        ch.provider = acct.address.lower()
        ch.native[ch.provider] = 10**19
        snd = C.Sender(rpc, acct, 177)
        data = C.selector("setPrice(uint256,uint128)") + encode(["uint256", "uint128"], [1, 25000])
        rc = snd.send(CONTRACT, data, kind="settle", wait_s=0.3)
        self.assertEqual(rc["status"], "0x1")
        est = [p[0] for m, p in calls if m == "eth_estimateGas"][0]
        sim = [p[0] for m, p in calls if m == "eth_call"][0]
        raw = [p[0] for m, p in calls if m == "eth_sendRawTransaction"][0]
        tx = T.TypedTransaction.from_bytes(T.HexBytes(bytes.fromhex(raw[2:]))).as_dict()
        for c in (est, sim):
            self.assertEqual(int(c["maxFeePerGas"], 16), tx["maxFeePerGas"])
            self.assertEqual(int(c["maxPriorityFeePerGas"], 16), tx["maxPriorityFeePerGas"])
        self.assertEqual(int(sim["gas"], 16), tx["gas"], "the eth_call uses the exact gas limit that is signed")

    def test_gasprice_zero_trick_fails_simulation_no_tx(self):
        env = Env()
        wallet = Account.create()                 # smart wallet: signature valid only when tx.gasprice == 0
        env.ch.contract_wallets.add(wallet.address.lower())
        env.ch.fund(wallet.address, 10**7)
        st, body, _ = env.quote(payer=wallet)
        vb = int(time.time()) + 300
        data = pwa_calldata(env, body, wallet, vb)
        # positive control: a simulation WITHOUT fee fields (what the old seller did) passes
        env.ch("eth_estimateGas", [{"from": env.provider.address, "to": CONTRACT, "data": "0x" + data.hex()}])
        with self.assertRaises(C.RpcError) as cm:   # the real Sender simulates at the real gas price
            env.s.sender.send(CONTRACT, data, kind="settle", wait_s=0.3)
        self.assertEqual(C.revert_reason(cm.exception), "FiatTokenV2: invalid signature")
        self.assertEqual(env.ch.sent, [])
        self.assertEqual(env.ch.gas_burned, 0)

    def test_refunds_capped_per_payer_per_day_then_sent_next_day(self):
        target = "0x" + "de" * 20
        env = Env(smoke_refund_address=target)
        out = [env.buy(PROFILE, "address=" + target, ip="1.1.1.%d" % i)[1] for i in range(3)]
        self.assertEqual([o[1]["refund"]["refunded"] for o in out], [True, True, False])
        self.assertTrue(out[2][1]["refund"]["note"].startswith("refund queued"))
        self.assertEqual((env.sent(PWA), env.sent(REFUND)), (3, 2))
        env.s.reconcile_once()
        self.assertEqual(env.sent(REFUND), 2, "still capped today")
        env.s.state.refund_days.clear()          # next UTC day
        env.s.reconcile_once()
        self.assertEqual(env.sent(REFUND), 3)

    def test_refund_budget_is_separate(self):
        target = "0x" + "de" * 20
        env = Env(smoke_refund_address=target, refund_budget_wei=1)
        hdr, (st, b, _) = env.buy(PROFILE, "address=" + target)
        self.assertFalse(b["refund"]["refunded"])
        self.assertEqual(env.sent(REFUND), 0)
        hdr, (st, b, _) = env.buy(ip="2.2.2.2")
        self.assertEqual(st, 200, "settlements keep their own budget")

    def test_low_balance_alert(self):
        d = tempfile.mkdtemp(dir=os.path.join(os.path.dirname(HERE), "tmp")) if os.path.isdir(os.path.join(os.path.dirname(HERE), "tmp")) else tempfile.mkdtemp()
        try:
            env = Env(state_dir=d, provider_hsk=4 * 10**18)       # < 2 x (2 + 0.5) HSK
            env.s.check_balance()
            self.assertTrue(env.s.low_balance)
            self.assertEqual(env.s.state.alerts[-1]["kind"], "provider_low_balance")
            self.assertTrue(os.path.exists(os.path.join(d, "ALERT")))
            env.ch.native[env.ch.provider] = 6 * 10**18             # topped up above the threshold
            env.s.check_balance()
            self.assertFalse(env.s.low_balance)
            self.assertFalse(os.path.exists(os.path.join(d, "ALERT")), "alert clears once the balance is back")
            self.assertEqual(env.s.state.alerts[-1]["kind"], "provider_balance_ok")
        finally:
            shutil.rmtree(d)

    def test_stale_alert_file_cleared_at_start(self):
        base = os.path.join(os.path.dirname(HERE), "tmp")
        d = tempfile.mkdtemp(dir=base) if os.path.isdir(base) else tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "ALERT"), "w") as f:      # left by an earlier run
                f.write('{"alert": "provider_low_balance"}\n')
            with open(os.path.join(d, "OTHER"), "w") as f:
                f.write("x")
            env = Env(state_dir=d)                                 # 10 HSK: healthy
            env.s.check_balance()
            self.assertFalse(os.path.exists(os.path.join(d, "ALERT")))
            self.assertTrue(os.path.exists(os.path.join(d, "OTHER")), "only the ALERT file is touched")
            os.symlink(os.path.join(d, "OTHER"), os.path.join(d, "ALERT"))
            env.s.check_balance()
            self.assertTrue(os.path.exists(os.path.join(d, "OTHER")), "a symlinked ALERT is never followed")
        finally:
            shutil.rmtree(d)

    def test_settlement_refused_when_gas_exceeds_price(self):
        env = Env(hsk_usd=1000.0)                 # HSK so expensive that one settlement costs more than the sale
        hdr, (st, b, _) = env.buy()
        self.assertEqual((st, b["error"]), (503, "settlement_unavailable"))
        self.assertEqual(env.ch.sent, [])


class F3PaidDataAfterRefund(unittest.TestCase):
    def test_refunded_payment_is_never_served(self):
        h = "0x" + "cd" * 32
        env = Env()
        env.ch.add_external_tx(h)
        env.ch.fail_methods.add("eth_getTransactionByHash")      # upstream fails during the work -> refund
        hdr, (st, b, _) = env.buy(TXR, "hash=" + h)
        self.assertTrue(b["refund"]["refunded"])
        env.ch.fail_methods.clear()
        for _ in range(3):
            st, b, _ = env.pay(hdr, TXR, "hash=" + h)
            self.assertEqual((st, b["error"]), (410, "payment_refunded"))

    def test_served_payment_is_never_refunded(self):
        env = Env()
        hdr, (st, b, _) = env.buy()
        self.assertEqual(st, 200)
        env.ch.fail_methods.add("eth_getBalance")                # upstream fails on a replay
        st, b, _ = env.pay(hdr)
        self.assertEqual(st, 502)
        self.assertFalse(b["refund"]["refunded"])
        self.assertEqual(env.sent(REFUND), 0)


class F4Races(unittest.TestCase):
    def test_concurrent_replays_respect_serve_cap(self):
        env = Env()
        hdr, (st, b, _) = env.buy()
        self.assertEqual(st, 200)
        orig = S.WORK[PROFILE]
        S.WORK[PROFILE] = lambda rpc, a: (time.sleep(0.3), {"stub": True})[1]
        try:
            res = []
            ts = [threading.Thread(target=lambda i=i: res.append(env.pay(hdr, ip="10.0.0.%d" % i)[0])) for i in range(20)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            for _ in range(5):
                res.append(env.pay(hdr, ip="10.0.1.1")[0])
        finally:
            S.WORK[PROFILE] = orig
        self.assertLessEqual(1 + res.count(200), S.MAX_SERVES)
        self.assertEqual(env.s.state.recs[b["payment"]["requestId"]]["served"], S.MAX_SERVES)
        self.assertEqual(env.sent(PWA), 1)

    def test_concurrent_first_payments_settle_once(self):
        env = Env()
        env.ch.broadcast_delay = 0.5
        st, body, _ = env.quote()
        hdr = env.header(body)
        res = []
        ts = [threading.Thread(target=lambda i=i: res.append(env.pay(hdr, ip="10.0.0.%d" % i)[0])) for i in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(env.sent(PWA), 1, "one payment -> one payWithAuthorization")
        self.assertEqual(res.count(200), 1)
        self.assertEqual(sorted(set(res)), [200, 409])


class F5Hsp(unittest.TestCase):
    def test_unprovable_capability_refused_before_settlement(self):
        env = Env()
        hdr, (st, b, _) = env.buy(mandate_caps=[HSP.CAP_SETTLED, "proves:delivery"])
        self.assertEqual((st, b["error"]), (400, "hsp_mandate_invalid"))
        self.assertIn("proves:delivery", b["message"])
        self.assertEqual(env.ch.sent, [])

    def test_short_deadline_refused_before_settlement(self):
        env = Env()
        hdr, (st, b, _) = env.buy(mandate_caps=[HSP.CAP_SETTLED], deadline=int(time.time()) + 30)
        self.assertEqual((st, b["error"]), (400, "hsp_mandate_invalid"))
        self.assertEqual(env.ch.sent, [])

    def test_policy_after_settlement_is_refunded_not_shipped(self):
        env = Env()
        env.ch.broadcast_delay = 3.5
        old = S.HSP_MIN_DEADLINE
        S.HSP_MIN_DEADLINE = 1
        try:
            hdr, (st, b, _) = env.buy(mandate_caps=[HSP.CAP_SETTLED], deadline=int(time.time()) + 2)
        finally:
            S.HSP_MIN_DEADLINE = old
        self.assertEqual((st, b["error"]), (409, "hsp_not_accepted"))
        self.assertEqual(b["hsp"]["decision"]["outcomeClass"], "POLICY")
        self.assertTrue(b["refund"]["refunded"])
        self.assertNotIn("data", b)
        self.assertEqual((env.sent(PWA), env.sent(REFUND)), (1, 1))

    def test_accept_path_still_works(self):
        env = Env()
        hdr, (st, b, _) = env.buy(mandate_caps=[HSP.CAP_SETTLED])
        self.assertEqual(st, 200, b)
        self.assertEqual(b["hsp"]["decision"]["outcomeClass"], "ACCEPT")


class F6Reconciliation(unittest.TestCase):
    def setUp(self):
        base = os.path.join(os.path.dirname(HERE), "tmp")
        self.d = tempfile.mkdtemp(dir=base if os.path.isdir(base) else None)

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_payment_submitted_directly_is_served(self):
        env = Env()
        st, body, _ = env.quote()
        vb = int(time.time()) + 300
        env.ch.submit_direct(env.buyer.address, pwa_calldata(env, body, env.buyer, vb))   # buyer front-runs us
        st, b, _ = env.pay(env.header(body, vb=vb))
        self.assertEqual(st, 200, b)
        self.assertEqual(env.sent(PWA), 0)

    def test_already_paid_during_simulation_is_served(self):
        env = Env()
        st, body, _ = env.quote()
        vb = int(time.time()) + 300
        data = pwa_calldata(env, body, env.buyer, vb)
        env.ch.on_estimate = lambda call: (env.ch.submit_direct(env.buyer.address, data), setattr(env.ch, "on_estimate", None))
        st, b, _ = env.pay(env.header(body, vb=vb))
        self.assertEqual(st, 200, b)
        self.assertEqual(env.sent(PWA), 0)

    def test_slow_settlement_pending_then_served_without_resending(self):
        env = Env()
        env.ch.hold_txs = True
        hdr, (st, b, _) = env.buy()
        self.assertEqual((st, b["error"]), (503, "settlement_pending"))
        st, b, _ = env.pay(hdr)
        self.assertEqual((st, b["error"]), (503, "settlement_pending"))
        env.ch.mine()
        st, b, _ = env.pay(hdr)
        self.assertEqual(st, 200, b)
        self.assertEqual(env.sent(PWA), 1)

    def test_journal_survives_restart(self):
        env = Env(state_dir=self.d)
        target = "0x" + "de" * 20
        env.cfg["smoke_refund_address"] = target
        env.s = env.new_seller()
        hdr, (st, b, _) = env.buy()
        self.assertEqual(st, 200)
        env.pay(hdr)
        rhdr, (st, rb, _) = env.buy(PROFILE, "address=" + target, ip="2.2.2.2")
        self.assertTrue(rb["refund"]["refunded"])
        env.s.state.record("ban", payer="0x" + "99" * 20, until=time.time() + 3600, why="test")
        env.s = env.new_seller()                  # restart
        st, b, _ = env.pay(hdr)
        self.assertEqual(st, 200)
        st, b, _ = env.pay(hdr)
        self.assertEqual((st, b["error"]), (429, "already_served"), "serve count survived the restart")
        st, b, _ = env.pay(rhdr, PROFILE, "address=" + target)
        self.assertEqual((st, b["error"]), (410, "payment_refunded"))
        self.assertTrue(env.s.state.banned_until("0x" + "99" * 20))
        self.assertGreater(env.s.sender.spent.get("settle", 0), 0, "gas spent today survived the restart")
        self.assertEqual(env.sent(PWA), 2)

    def test_failed_refund_is_retried_by_reconciler(self):
        target = "0x" + "de" * 20
        env = Env(smoke_refund_address=target)
        env.ch.block_refunds = True               # refund reverts (InsufficientCredit) for now
        hdr, (st, b, _) = env.buy(PROFILE, "address=" + target)
        self.assertFalse(b["refund"]["refunded"])
        rid = b["payment"]["requestId"]
        self.assertTrue(env.s.state.recs[rid]["refund_failed"])
        st, b2, _ = env.pay(hdr, PROFILE, "address=" + target)
        self.assertEqual(st, 410, "never served while its refund is pending")
        env.ch.block_refunds = False
        env.s.reconcile_once()
        self.assertTrue(env.s.state.recs[rid]["refunded"])
        self.assertTrue(env.ch.payments[rid]["refunded"])


class F7KeysAndSandbox(unittest.TestCase):
    def setUp(self):
        base = os.path.join(os.path.dirname(HERE), "tmp")
        self.d = tempfile.mkdtemp(dir=base if os.path.isdir(base) else None)

    def tearDown(self):
        shutil.rmtree(self.d)

    def _w(self, name, text, mode=0o600):
        p = os.path.join(self.d, name)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(p, mode)
        return p

    def test_key_file_rules(self):
        k = Account.create().key.hex()
        good = self._w("provider.env", f"HKPW_PROVIDER_PK={k}\n")
        self.assertEqual(S.read_key_file(good, "HKPW_PROVIDER_PK"), k)
        multi = self._w("multi.env", f"HKPW_PROVIDER_PK={k}\nHKPW_PAYEE_PK={k}\n")
        with self.assertRaises(S.KeyFileError):
            S.read_key_file(multi, "HKPW_PROVIDER_PK")
        loose = self._w("loose.env", f"HKPW_PROVIDER_PK={k}\n", 0o644)
        with self.assertRaises(S.KeyFileError):
            S.read_key_file(loose, "HKPW_PROVIDER_PK")
        link = os.path.join(self.d, "link.env")
        os.symlink(good, link)
        with self.assertRaises(S.KeyFileError):
            S.read_key_file(link, "HKPW_PROVIDER_PK")

    def test_seller_loads_key_dir_and_creates_nothing(self):
        self._w("provider.env", f"HKPW_PROVIDER_PK={Account.create().key.hex()}\n")
        self._w("adapter.env", f"HKPW_ADAPTER_PK={Account.create().key.hex()}\n")
        self._w("token.env", f"PAYWALL_TOKEN_KEY={os.urandom(32).hex()}\n")
        before = sorted(os.listdir(self.d))
        s = S.Seller({"contract": CONTRACT, "key_dir": self.d}, rpc=lambda *a, **k: None)
        self.assertEqual(sorted(os.listdir(self.d)), before)
        self.assertTrue(s.provider.address)
        os.remove(os.path.join(self.d, "token.env"))
        with self.assertRaises(S.KeyFileError):
            S.Seller({"contract": CONTRACT, "key_dir": self.d}, rpc=lambda *a, **k: None)
        self.assertNotIn("token.env", os.listdir(self.d), "a missing key file is an error, never created")

    def test_egress_guard_rules(self):
        import egress
        g = egress.EgressGuard([("127.0.0.1", 18545), ("mainnet.hsk.xyz", 443)])
        self.assertIsNone(g.refusal(("127.0.0.1", 18545)))
        for bad in [("127.0.0.1", 9222), ("10.0.0.1", 443), ("8.8.8.8", 53), "/run/x.sock", ("::1", 18545),
                    ("example.com", 443)]:
            self.assertIsNotNone(g.refusal(bad), bad)

    def test_egress_guard_enforced_in_process_with_positive_control(self):
        lst = socket.socket()
        lst.bind(("127.0.0.1", 0))
        lst.listen(5)
        port = lst.getsockname()[1]
        prog = ("import sys, socket; sys.path.insert(0, %r); import egress\n"
                "if sys.argv[1] == 'guard': egress.install([('127.0.0.1', 1)])\n"
                "try:\n  socket.create_connection(('127.0.0.1', %d), timeout=3); print('CONNECTED')\n"
                "except PermissionError as e: print('BLOCKED')\n") % (HERE, port)
        out = {m: subprocess.run([sys.executable, "-I", "-c", prog, m], capture_output=True, text=True, timeout=30).stdout.strip()
               for m in ("noguard", "guard")}
        lst.close()
        self.assertEqual(out, {"noguard": "CONNECTED", "guard": "BLOCKED"})


class F8ClientKeysAndBounds(unittest.TestCase):
    def test_client_key_rules(self):
        h = {"CF-Connecting-IP": "203.0.113.7"}
        self.assertEqual(S.client_key("127.0.0.1", h, "127.0.0.1"), "203.0.113.7")
        self.assertEqual(S.client_key("127.0.0.1", h, "0.0.0.0"), "127.0.0.1", "not bound to loopback: header ignored")
        self.assertEqual(S.client_key("198.51.100.2", h, "127.0.0.1"), "198.51.100.2", "peer not loopback: ignored")
        self.assertEqual(S.client_key("127.0.0.1", {"CF-Connecting-IP": "junk"}, "127.0.0.1"), "127.0.0.1")
        k1 = S.client_key("127.0.0.1", {"CF-Connecting-IP": "2001:db8:1:2:aaaa::1"}, "127.0.0.1")
        k2 = S.client_key("127.0.0.1", {"CF-Connecting-IP": "2001:db8:1:2:bbbb::9"}, "127.0.0.1")
        self.assertEqual((k1, k1 == k2), ("2001:db8:1:2::/64", True))
        self.assertEqual(S.client_key("::ffff:198.51.100.2", {}, "0.0.0.0"), "198.51.100.2")

    def test_connection_and_request_bounds(self):
        env = Env()

        class Small(S.Server):
            max_connections = 2
        srv = Small(("127.0.0.1", 0), S.make_handler(env.s))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
            time.sleep(0.2)
            c = socket.create_connection(("127.0.0.1", port))
            c.settimeout(3)
            got = b""
            while b"busy" not in got:
                chunk = c.recv(300)
                if not chunk:
                    break
                got += chunk
            self.assertIn(b"503", got)
            for s_ in idle + [c]:
                s_.close()
            time.sleep(0.3)
            for _ in range(srv.max_requests):     # every request slot busy -> 503
                srv.req_sem.acquire()
            c = socket.create_connection(("127.0.0.1", port))
            c.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            c.settimeout(3)
            got = b""
            while b"busy" not in got:
                chunk = c.recv(300)
                if not chunk:
                    break
                got += chunk
            self.assertIn(b" 503 ", got)
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()


class F11VerifyTx(unittest.TestCase):
    def test_contract_read_first_client_tx_never_fetched(self):
        env = Env()
        st, body, _ = env.quote()
        x = body["accepts"][0]["extra"]
        bogus = "0x" + "77" * 32
        n = len(env.ch.calls)
        st, b, _ = env.s.handle_paid(PROFILE, PQ, {"X-Payer": env.buyer.address, "X-Paywall-Token": x["token"],
                                                   "X-Paywall-Tx": bogus}, "1.1.1.1", {})
        self.assertEqual((st, b["error"]), (402, "payment_not_found"))
        self.assertEqual(env.ch.calls[n:], ["eth_call"], "only getPayment(requestId): no receipt fetch")
        vb = int(time.time()) + 300
        real = env.ch.submit_direct(env.buyer.address, pwa_calldata(env, body, env.buyer, vb))
        n = len(env.ch.calls)
        st, b, _ = env.s.handle_paid(PROFILE, PQ, {"X-Payer": env.buyer.address, "X-Paywall-Token": x["token"],
                                                   "X-Paywall-Tx": bogus}, "1.1.1.1", {})
        self.assertEqual(st, 200, b)
        self.assertEqual(b["payment"]["tx"], real, "the Paid log found by requestId, not the client's hash")
        self.assertNotIn("eth_getTransactionReceipt", env.ch.calls[n:])


class F12HostileValues(unittest.TestCase):
    def test_hostile_values_never_500(self):
        env = Env()
        st, body, _ = env.quote()

        def m(path, value):
            def f(p):
                cur = p
                for k in path[:-1]:
                    cur = cur[k]
                cur[path[-1]] = value
            return f
        cases = [
            (["accepted", "amount"], "²"), (["accepted", "amount"], 10000), (["accepted", "payTo"], {"a": 1}),
            (["accepted", "asset"], None), (["accepted", "extra", "endpointId"], "²"),
            (["extensions"], {"hsp-draft": ["x"]}), (["extensions"], {"hsp-draft": "x"}),
            (["extensions"], {"hsp-draft": {"mandate": "x", "signature": "0x"}}),
            (["extensions"], {"hsp-draft": {"mandate": {"payer": 1}, "signature": "0x" + "11" * 65}}),
        ]
        mand = json.loads(base64.b64decode(env.header(body, mandate_caps=[HSP.CAP_SETTLED])))["extensions"]["hsp-draft"]
        for k, v in [("requiredCapabilities", ["x"]), ("requiredCapabilities", 5), ("amount", "²"), ("amount", -1),
                     ("deadline", [1]), ("reference", 7), ("payer", None)]:
            mm = json.loads(json.dumps(mand))
            mm["mandate"][k] = v
            cases.append((["extensions"], {"hsp-draft": mm}))
        for path, value in cases:
            st, b, _ = env.pay(env.header(body, mutate=m(path, value)))
            self.assertIn(st, (400, 402), (path, value, st, b))
        self.assertEqual(env.ch.sent, [])


class F13OneReceiptPerPayment(unittest.TestCase):
    def test_one_hsp_receipt_per_payment(self):
        env = Env()
        st, body, _ = env.quote()
        vb = int(time.time()) + 300
        h1 = env.header(body, vb=vb, mandate_caps=[HSP.CAP_SETTLED], deadline=int(time.time()) + 900)
        st, b1, _ = env.pay(h1)
        self.assertEqual(st, 200, b1)
        time.sleep(1.1)
        st, b2, _ = env.pay(h1)
        self.assertEqual(st, 200)
        self.assertEqual((b2["hsp"]["receipt"], b2["hsp"]["receiptSignature"]), (b1["hsp"]["receipt"], b1["hsp"]["receiptSignature"]))
        h3 = env.header(body, vb=vb, mandate_caps=[HSP.CAP_SETTLED], deadline=int(time.time()) + 901)
        st, b3, _ = env.pay(h3)
        self.assertEqual((st, b3["error"]), (409, "hsp_receipt_exists"))
        self.assertEqual(env.sent(PWA), 1)


class StateJournal(unittest.TestCase):
    def test_torn_line_and_unknown_events_are_skipped(self):
        base = os.path.join(os.path.dirname(HERE), "tmp")
        d = tempfile.mkdtemp(dir=base if os.path.isdir(base) else None)
        try:
            p = os.path.join(d, "j.jsonl")
            st = ST.SellerState(p)
            st.record("served", rid="0x" + "11" * 32)
            with open(p, "a") as f:
                f.write('{"event": "served", "rid": "0x' + "11" * 32 + '"}\n{"event":"bogus"}\n{"event": "serv')
            st2 = ST.SellerState(p)
            self.assertEqual(st2.recs["0x" + "11" * 32]["served"], 2)
            self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")
        finally:
            shutil.rmtree(d)


class B2B4LauncherLifetime(unittest.TestCase):
    """B2B-4: deploy/run_sandboxed.sh started in the background (`&`, nohup, disown) keeps the sandbox running after the
    launching shell exits; SIGTERM or SIGKILL to the launcher still takes the whole sandbox down, and
    HSK_METER_DIE_WITH_PARENT=1 still ties it to whatever started the script. The real script and real bwrap run a
    stand-in "python" (a shell loop) with dummy key files: no keys, no network."""

    SCRIPT = os.path.join(os.path.dirname(HERE), "deploy", "run_sandboxed.sh")

    def setUp(self):
        if not os.access("/usr/bin/bwrap", os.X_OK):
            self.skipTest("bubblewrap is not installed")
        base = os.path.join(os.path.dirname(HERE), "tmp")      # not /tmp: the sandbox mounts its own /tmp
        os.makedirs(base, exist_ok=True)
        self.d = tempfile.mkdtemp(dir=base)
        keys = os.path.join(self.d, "keys")
        os.mkdir(keys, 0o700)
        for f in ("provider.env", "adapter.env", "token.env"):
            with open(os.path.join(keys, f), "w") as fh:
                fh.write("DUMMY=1\n")                           # never a real key: the stand-in reads nothing
        os.makedirs(os.path.join(self.d, "venv", "bin"))
        self.marker = os.path.join(self.d, "venv", "bin", "python")
        with open(self.marker, "w") as fh:
            fh.write("#!/bin/sh\nwhile :; do sleep 1; done\n")
        os.chmod(self.marker, 0o755)
        self.env = {"PATH": "/usr/bin:/bin", "HOME": self.d, "HSK_METER_KEY_DIR": keys,
                    "HSK_METER_VENV": os.path.join(self.d, "venv"), "HSK_METER_STATE_DIR": os.path.join(self.d, "state")}
        self.launchers = []

    def tearDown(self):
        import signal
        for pid in self.launchers + self.procs(any_arg=True):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.wait_for(lambda: not self.procs(any_arg=True), 10)
        shutil.rmtree(self.d)

    def procs(self, any_arg=False):
        """Host PIDs of the stand-in seller (`/bin/sh <venv>/bin/python ...`); any_arg: also the bwrap processes."""
        found = []
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                with open(f"/proc/{p}/cmdline", "rb") as fh:
                    argv = fh.read().split(b"\0")
                with open(f"/proc/{p}/stat") as fh:
                    if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                        continue
            except OSError:
                continue
            m = self.marker.encode()
            if (argv[1:2] == [m]) or (any_arg and m in argv):
                found.append(int(p))
        return found

    @staticmethod
    def alive(pid):
        try:
            with open(f"/proc/{pid}/stat") as fh:
                return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False

    @staticmethod
    def wait_for(cond, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.2)
        return cond()

    def launch(self, how, extra_env=None):
        """Start the launcher from a shell that exits ~2 s later (after bwrap is up); returns the launcher PID."""
        cmd = {"amp": '"$S" probe >"$L" 2>&1 & echo $!',
               "nohup": 'nohup "$S" probe >"$L" 2>&1 & echo $!',
               "disown": '"$S" probe >"$L" 2>&1 & echo $!; disown'}[how] + "; sleep 2"
        env = {**self.env, **(extra_env or {}), "S": self.SCRIPT, "L": os.path.join(self.d, how + ".log")}
        r = subprocess.run(["/bin/bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=60)
        pid = int(r.stdout.split()[0])
        self.launchers.append(pid)
        return pid

    def test_background_start_survives_the_shell_exit(self):
        for how in ("amp", "nohup", "disown"):
            launcher = self.launch(how)
            time.sleep(2)                               # the launching shell is gone by now
            with open(os.path.join(self.d, how + ".log")) as fh:
                log = fh.read()
            self.assertEqual(len(self.procs()), 1, f"{how}: the sandboxed process must survive ({log[-300:]})")
            self.assertTrue(self.alive(launcher), how)
            os.kill(launcher, 15)
            self.assertTrue(self.wait_for(lambda: not self.procs(any_arg=True), 10), f"{how}: SIGTERM stops it")

    def test_killing_the_launcher_kills_the_sandbox(self):
        launcher = self.launch("nohup")
        self.assertTrue(self.wait_for(lambda: len(self.procs()) == 1, 5), "started")
        os.kill(launcher, 9)                            # the supervisor dies hard: bwrap's PDEATHSIG fires
        self.assertTrue(self.wait_for(lambda: not self.procs(any_arg=True), 10), "the sandbox died with its launcher")

    def test_die_with_parent_on_request(self):
        for flag, survives in (("1", False), ("0", True)):
            parent = subprocess.Popen(["/bin/bash", "-c", '"$S" probe >/dev/null 2>&1 & wait'],
                                      env={**self.env, "S": self.SCRIPT, "HSK_METER_DIE_WITH_PARENT": flag})
            try:
                self.assertTrue(self.wait_for(lambda: len(self.procs()) == 1, 10), "started")
                parent.kill()                           # whatever started the script dies
                parent.wait(10)
                time.sleep(2)
                self.assertEqual(bool(self.procs()), survives,
                                 f"HSK_METER_DIE_WITH_PARENT={flag}: {'survives' if survives else 'dies with the parent'}")
            finally:
                for pid in self.procs(any_arg=True):
                    os.kill(pid, 9)
                self.wait_for(lambda: not self.procs(any_arg=True), 10)


if __name__ == "__main__":
    unittest.main()
