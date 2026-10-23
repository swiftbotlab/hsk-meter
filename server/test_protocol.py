#!/usr/bin/env python3
"""Tests for protocol.py / hsp_draft.py / seller pieces. Offline by default; --online adds read-only checks against
the deployed contract on HashKey Chain (eth_call only: nothing is sent).

  test_protocol.py [-v] [--online]
"""
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chain as C        # noqa: E402
import hsp_draft as HSP  # noqa: E402
import protocol as P     # noqa: E402
import seller as S       # noqa: E402
from eth_abi import encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from eth_utils import keccak  # noqa: E402

ONLINE = "--online" in sys.argv
if ONLINE:
    sys.argv.remove("--online")
ROOT = os.path.dirname(HERE)
VEC = {"domain": "0x4cc9139c98107575051bd6bb5f66ae90d3ff9e3e996f1cb12aa95e7d624e0d64",
       "digest": "0x63cb1cdf9b0be81256dc25fadf023a41de8b27b5ce67d3960506edb07d765c95",
       "rid": "0x2222222222222222222222222222222222222222ce67d3960506edb07d765c95"}


class Offline(unittest.TestCase):
    def test_request_id_vector_matches_solidity(self):
        self.assertEqual("0x" + P.DOMAIN.hex(), VEC["domain"])
        d, rid = P.compute_request_id(177, "0x1111111111111111111111111111111111111111", 1,
                                      "0x2222222222222222222222222222222222222222", "GET /v1/weather?city=Paris", "0x" + "33" * 32)
        self.assertEqual(d, VEC["digest"])
        self.assertEqual(rid, VEC["rid"])
        self.assertEqual(P.payer_of(rid), "0x" + "22" * 20)

    def test_request_id_binds_every_input(self):
        base = dict(chain_id=177, contract="0x" + "11" * 20, endpoint_id=1, payer="0x" + "22" * 20, resource="GET /a", token="0x" + "33" * 32)
        r0 = P.compute_request_id(**base)[1]
        for k, v in [("chain_id", 133), ("contract", "0x" + "12" * 20), ("endpoint_id", 2), ("payer", "0x" + "23" * 20),
                     ("resource", "GET /b"), ("token", "0x" + "34" * 32)]:
            self.assertNotEqual(P.compute_request_id(**{**base, k: v})[1], r0, k)

    def test_bad_inputs_rejected(self):
        with self.assertRaises(P.ProtocolError):
            P.compute_request_id(177, "0x11", 1, None, "GET /a", "0x" + "33" * 32)
        with self.assertRaises(P.ProtocolError):
            P.compute_request_id(177, "0x" + "11" * 20, 1, None, "", "0x" + "33" * 32)
        with self.assertRaises(P.ProtocolError):
            P.authorization_nonce("0x" + "11" * 20, 1, "0x1234")

    def test_nonce_formula(self):
        c, rid = "0x" + "ab" * 20, "0x" + "cd" * 32
        want = "0x" + keccak(encode(["address", "uint256", "bytes32"], [c, 7, bytes.fromhex("cd" * 32)])).hex()
        self.assertEqual(P.authorization_nonce(c, 7, rid), want)

    def test_typed_data_digest_matches_manual_eip712(self):
        acct = Account.create()
        td = P.receive_auth_typed_data(acct.address, "0x" + "ab" * 20, 10000, 0, 2_000_000_000, "0x" + "cd" * 32)
        sm = encode_typed_data(full_message=td)
        dom = keccak(encode(["bytes32", "bytes32", "bytes32", "uint256", "address"], [
            keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
            keccak(text="Bridged USDC"), keccak(text="2"), 177, P.USDCE]))
        self.assertEqual(dom.hex(), "78101f50ab6b299bde76a9973e97b1c9ead7df26f6a40692f4440296a507259d")  # = DOMAIN_SEPARATOR() on 177
        th = keccak(text="ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)")
        self.assertEqual(th.hex(), "d099cc98ef71107a616c4f0f941f04c322d8e254fe26b3c6668db87aae413de8")   # = token typehash on 177
        sh = keccak(encode(["bytes32", "address", "address", "uint256", "uint256", "uint256", "bytes32"],
                           [th, acct.address, "0x" + "ab" * 20, 10000, 0, 2_000_000_000, bytes.fromhex("cd" * 32)]))
        self.assertEqual(sm.header, dom)
        self.assertEqual(sm.body, sh)

    def test_b64json_caps_and_garbage(self):
        obj = {"a": 1}
        self.assertEqual(P.parse_b64json(P.b64json(obj)), obj)
        for bad in ["", "!!!!", "a" * 9000, P.b64json([1, 2]), P.b64json("x" * 7000)]:
            with self.assertRaises(P.ProtocolError):
                P.parse_b64json(bad)

    def _payload(self, **over):
        p = {"x402Version": 2, "accepted": {"scheme": "exact", "network": P.NETWORK,
                                             "extra": {"endpointId": "1", "requestId": "0x" + "11" * 32, "token": "0x" + "22" * 32,
                                                       "nonce": "0x" + "33" * 32, "resource": "GET /x"}},
             "payload": {"signature": "0x" + "44" * 65, "authorization": {"from": "0x" + "55" * 20, "to": "0x" + "66" * 20,
                                                                          "value": "10000", "validAfter": "0", "validBefore": "1999999999",
                                                                          "nonce": "0x" + "33" * 32}}}
        for k, v in over.items():
            cur = p
            parts = k.split(".")
            for q in parts[:-1]:
                cur = cur[q]
            if v is None:
                del cur[parts[-1]]
            else:
                cur[parts[-1]] = v
        return p

    def test_payload_validation(self):
        acc, au, sig, ext = P.validate_payment_payload(self._payload())
        self.assertEqual(au["value"], 10000)
        bad = [{"x402Version": 1}, {"accepted.scheme": "upto"}, {"accepted.network": "eip155:1"},
               {"payload.signature": "0x1234"}, {"payload.authorization.value": 10000}, {"payload.authorization.value": "-1"},
               {"payload.authorization.value": "1" * 40}, {"payload.authorization.from": "0xzz"},
               {"payload.authorization.nonce": "0x12"}, {"accepted.extra.endpointId": "01"}, {"accepted.extra": None},
               {"extensions": "x"}]
        for b in bad:
            with self.assertRaises(P.ProtocolError, msg=str(b)):
                P.validate_payment_payload(self._payload(**b))

    def test_hsp_draft_roundtrip_and_rules(self):
        payer, adapter, other = Account.create(), Account.create(), Account.create()
        m = HSP.mandate_message(payer=payer.address, payee="0x" + "ab" * 20, token=P.USDCE, amount=10000,
                                reference="0x" + "cd" * 32, required_caps=[HSP.CAP_SETTLED], deadline=2_000_000_000)
        msig = HSP.sign_mandate(payer, m, 177)
        self.assertEqual(HSP.mandate_from_json(HSP.mandate_to_json(m)), m)
        mh = HSP.mandate_hash(m, 177)
        r = HSP.receipt_message(mandate_hash_=mh, tx_hash="0x" + "ef" * 32, log_index=3, contract="0x" + "01" * 20,
                                payer=payer.address, payee="0x" + "ab" * 20, token=P.USDCE, amount=10000, block=5,
                                caps=[HSP.CAP_SETTLED, HSP.CAP_REFUNDABLE], observed_at=1_900_000_000)
        rsig = HSP.sign_receipt(adapter, r, 177)
        self.assertEqual(HSP.receipt_from_json(HSP.receipt_to_json(r)), r)
        ok = HSP.verify(mandate=m, mandate_sig=msig, receipt=r, receipt_sig=rsig, chain_id=177, pinned_adapter=adapter.address)
        self.assertEqual(ok["outcomeClass"], "ACCEPT")
        self.assertEqual(ok["paymentId"], mh)
        # wrong pinned adapter, forged mandate signer, amount mismatch, missing capability, late, on-chain retryable
        self.assertEqual(HSP.verify(mandate=m, mandate_sig=msig, receipt=r, receipt_sig=rsig, chain_id=177,
                                    pinned_adapter=other.address)["outcomeClass"], "PERMANENT")
        self.assertEqual(HSP.verify(mandate=m, mandate_sig=HSP.sign_mandate(other, m, 177), receipt=r, receipt_sig=rsig,
                                    chain_id=177, pinned_adapter=adapter.address)["outcomeClass"], "PERMANENT")
        r2 = {**r, "amount": 9999}
        self.assertEqual(HSP.verify(mandate=m, mandate_sig=msig, receipt=r2, receipt_sig=HSP.sign_receipt(adapter, r2, 177),
                                    chain_id=177, pinned_adapter=adapter.address)["outcomeClass"], "PERMANENT")
        m3 = HSP.mandate_message(payer=payer.address, payee="0x" + "ab" * 20, token=P.USDCE, amount=10000,
                                 reference="0x" + "cd" * 32, required_caps=[HSP.CAP_SETTLED, "attests:kyc"], deadline=2_000_000_000)
        r3 = {**r, "mandateHash": bytes.fromhex(HSP.mandate_hash(m3, 177)[2:])}
        d3 = HSP.verify(mandate=m3, mandate_sig=HSP.sign_mandate(payer, m3, 177), receipt=r3,
                        receipt_sig=HSP.sign_receipt(adapter, r3, 177), chain_id=177, pinned_adapter=adapter.address)
        self.assertEqual(d3["outcomeClass"], "POLICY")
        self.assertIn("attests:kyc", d3["reasons"][0])
        r4 = {**r, "observedAt": 2_000_000_001}
        self.assertEqual(HSP.verify(mandate=m, mandate_sig=msig, receipt=r4, receipt_sig=HSP.sign_receipt(adapter, r4, 177),
                                    chain_id=177, pinned_adapter=adapter.address)["outcomeClass"], "POLICY")
        self.assertEqual(HSP.verify(mandate=m, mandate_sig=msig, receipt=r, receipt_sig=rsig, chain_id=177,
                                    pinned_adapter=adapter.address, onchain=lambda _r: (False, True, "lag"))["outcomeClass"], "RETRYABLE")
        self.assertEqual(HSP.verify(mandate=m, mandate_sig=msig, receipt=r, receipt_sig=rsig, chain_id=133,
                                    pinned_adapter=adapter.address)["outcomeClass"], "PERMANENT", "chain id is signed in")

    def test_coordinator_disabled_without_credentials(self):
        c = HSP.Coordinator(url=None, api_key=None)
        with self.assertRaises(HSP.Coordinator.NotConfigured):
            c.chains()
        with self.assertRaises(HSP.Coordinator.NotConfigured):
            HSP.Coordinator(url="http://x.example", api_key="k").register_payment({}, "0x")

    def test_rpc_host_allowlist(self):
        for bad in ["http://mainnet.hsk.xyz", "https://evil.example", "https://mainnet.hsk.xyz.evil.example",
                    "http://127.0.0.1:8545", "https://user@mainnet.hsk.xyz"]:
            with self.assertRaises(ValueError, msg=bad):
                C.Rpc(bad)
        C.Rpc("https://mainnet.hsk.xyz")
        C.Rpc("http://127.0.0.1:8545", allow_local=True)

    def test_rate_limiter(self):
        rl = S.RateLimiter(3, 60, max_keys=2)
        self.assertEqual([rl.hit("a") for _ in range(3)], [0, 0, 0])
        self.assertGreater(rl.hit("a"), 0)
        rl.hit("b"), rl.hit("c")             # evicts the least recently used key
        self.assertLessEqual(len(rl.b), 2)

    def test_revert_reason_decoding(self):
        e = C.RpcError("x")
        e.data = "0x" + keccak(text="AlreadyPaid(bytes32)").hex()[:8] + "00" * 32
        self.assertEqual(C.revert_reason(e), "AlreadyPaid")
        e.data = "0x08c379a0" + encode(["string"], ["FiatTokenV2: invalid signature"]).hex()
        self.assertEqual(C.revert_reason(e), "FiatTokenV2: invalid signature")


@unittest.skipUnless(ONLINE, "needs --online")
class Online(unittest.TestCase):
    """Read-only checks against the deployed contract on HashKey Chain (eth_call; nothing is sent)."""

    @classmethod
    def setUpClass(cls):
        cls.rpc = C.Rpc("https://mainnet.hsk.xyz")
        with open(os.path.join(ROOT, "ops", "state", "deployment.json")) as f:
            cls.c = json.load(f)["address"]
        with open(os.path.join(ROOT, "ops", "state", "addresses.json")) as f:
            addrs = json.load(f)
        cls.provider = addrs["provider"]
        env = S._read_env("secrets/hashkey_paywall_keys.env")
        cls.buyer = Account.from_key(env["HKPW_BUYER_PK"])

    def test_nonce_matches_contract(self):
        rid = P.compute_request_id(177, self.c.lower(), 1, self.buyer.address.lower(), "GET /t", "0x" + "77" * 32)[1]
        on = C.eth_call(self.rpc, self.c, "authorizationNonce(uint256,bytes32)", ["uint256", "bytes32"],
                        [1, bytes.fromhex(rid[2:])], ["bytes32"])[0]
        self.assertEqual("0x" + on.hex(), P.authorization_nonce(self.c, 1, rid))
        on_rid = C.eth_call(self.rpc, self.c, "requestIdFor(address,bytes32)", ["address", "bytes32"],
                            [self.buyer.address, bytes.fromhex(VEC["digest"][2:])], ["bytes32"])[0]
        self.assertEqual("0x" + on_rid.hex(), "0x" + self.buyer.address[2:].lower() + VEC["digest"][-24:])

    def _sim(self, rid, eid, value, sig, frm=None, va=0, vb=None):
        vb = vb or int(time.time()) + 120
        data = C.selector("payWithAuthorization(uint256,bytes32,address,uint256,uint256,uint256,bytes)") + encode(
            ["uint256", "bytes32", "address", "uint256", "uint256", "uint256", "bytes"],
            [eid, bytes.fromhex(rid[2:]), frm or self.buyer.address, value, va, vb, bytes.fromhex(sig[2:])])
        return self.rpc("eth_call", [{"from": self.provider, "to": self.c, "data": "0x" + data.hex()}, "latest"])

    def test_signed_authorization_simulates_ok_and_tampering_fails(self):
        e = C.read_endpoint(self.rpc, self.c, 1)
        rid = P.compute_request_id(177, self.c.lower(), 1, self.buyer.address.lower(), "GET /sim", "0x" + "%064x" % int(time.time()))[1]
        nonce = P.authorization_nonce(self.c, 1, rid)
        vb = int(time.time()) + 120
        td = P.receive_auth_typed_data(self.buyer.address, self.c, e["price"], 0, vb, nonce)
        sig = "0x" + self.buyer.sign_message(encode_typed_data(full_message=td)).signature.hex()
        self.assertEqual(self._sim(rid, 1, e["price"], sig, vb=vb), "0x")          # positive control: would succeed
        with self.assertRaises(C.RpcError) as cm:                                     # other endpoint: nonce differs
            self._sim(rid, 2, e["price"], sig, vb=vb)
        self.assertIn(C.revert_reason(cm.exception), ("FiatTokenV2: invalid signature", "WrongAmount"))
        with self.assertRaises(C.RpcError) as cm:                                     # tampered validBefore
            self._sim(rid, 1, e["price"], sig, vb=vb + 1)
        self.assertEqual(C.revert_reason(cm.exception), "FiatTokenV2: invalid signature")
        with self.assertRaises(C.RpcError) as cm:                                     # wrong amount: contract rule first
            self._sim(rid, 1, e["price"] + 1, sig, vb=vb)
        self.assertEqual(C.revert_reason(cm.exception), "WrongAmount")


if __name__ == "__main__":
    unittest.main()
