#!/usr/bin/env python3
"""Tests for the HashKey Merchant (HSP) gateway rail: server/hsp_gateway.py, server/hsp_merchant_agent.py,
agent/hsp_payer.py, against the local mock gateway server/hsp_mockgw.py. Offline (loopback only); openssl is used as
an independent implementation for the HMAC and ES256K checks (those tests are skipped without it).

  test_hsp_gateway.py [-v]
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "agent"))
import hsp_gateway as G          # noqa: E402
import hsp_merchant_agent as MA  # noqa: E402
import hsp_mockgw as MOCK        # noqa: E402
import hsp_payer as PAYER        # noqa: E402
from eth_account import Account  # noqa: E402

OPENSSL = shutil.which("openssl")
PAYEE = "0x" + "ab" * 20
SECRET = "test-app-secret-0123456789"
APP_KEY = "ak_test_0001"
FULL_EXAMPLE = {   # the docs' "Full structure" example (cart-mandate page), addresses filled in
    "id": "ORDER-001", "user_cart_confirmation_required": True,
    "payment_request": {
        "method_data": [
            {"supported_methods": "https://www.x402.org/", "data": {"x402Version": 2, "network": "sepolia", "chain_id": 11155111,
                                                                     "contract_address": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
                                                                     "pay_to": PAYEE, "coin": "USDC"}},
            {"supported_methods": "https://www.x402.org/", "data": {"x402Version": 2, "network": "sepolia", "chain_id": 11155111,
                                                                     "contract_address": "0xff5588b3b38dff1b4b49bfdcbf985e84d8751a0e",
                                                                     "pay_to": PAYEE, "coin": "USDT"}}],
        "details": {"id": "PAY-REQ-001",
                    "display_items": [{"label": "Item A", "amount": {"currency": "USD", "value": "10.00"}},
                                      {"label": "Item B", "amount": {"currency": "USD", "value": "5.00"}}],
                    "total": {"label": "Total", "amount": {"currency": "USD", "value": "15.00"}},
                    "modifiers": [{"supported_methods": "https://www.x402.org/", "data": {"method_data_indexes": [0, 1]},
                                   "additional_display_items": [{"label": "Service fee", "amount": {"currency": "USD", "value": "1.00"}}],
                                   "total": {"label": "Amount due", "amount": {"currency": "USD", "value": "16.00"}}}]}},
    "cart_expiry": "2024-03-01T12:00:00Z", "merchant_name": "My Store"}


def deep(o):
    return json.loads(json.dumps(o))


def test_key():
    return G.MerchantKey(int.from_bytes(os.urandom(32), "big") % (G.N - 1) + 1)


class Canonical(unittest.TestCase):
    def test_docs_vectors(self):
        self.assertEqual(G.canonical_json({"merchant_name": "My Store", "id": "cart-123", "cart_expiry": "2024-03-01T12:00:00Z"}),
                         '{"cart_expiry":"2024-03-01T12:00:00Z","id":"cart-123","merchant_name":"My Store"}')
        self.assertEqual(G.canonical_json({"b": 2, "a": 1, "nested": {"z": "last", "m": "middle"}}),
                         '{"a":1,"b":2,"nested":{"m":"middle","z":"last"}}')

    def test_utf16_key_order_like_javascript(self):
        # JS sorts keys by UTF-16 code units: U+10000 (D800 DC00) sorts BEFORE U+FFFF; code-point order would not.
        self.assertEqual(G.canonical_json({"￿": 1, "\U00010000": 2}), '{"\U00010000":2,"￿":1}')

    def test_rejects_ambiguous_values(self):
        for bad in ({"a": 1.5}, {"a": 2 ** 60}, {1: "x"}, {"a": "\ud800"}):
            with self.assertRaises(G.HspError):
                G.canonical_json(bad)
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1.0}', b"\xff"):
            with self.assertRaises(G.HspError):
                G.loads_strict(raw)


class Hmac(unittest.TestCase):
    def test_message_format_matches_docs_examples(self):
        self.assertEqual(G.signing_message("post", "/api/v1/merchant/orders", "", "abc123def456", 1709123456, "a3f1b2c4d5e6"),
                         "POST\n/api/v1/merchant/orders\n\nabc123def456\n1709123456\na3f1b2c4d5e6")
        self.assertEqual(G.signing_message("GET", "/api/v1/merchant/payments", "cart_mandate_id=ORDER-123", "", 1709123456,
                                           "a3f1b2c4d5e6"),
                         "GET\n/api/v1/merchant/payments\ncart_mandate_id=ORDER-123\n\n1709123456\na3f1b2c4d5e6")

    @unittest.skipUnless(OPENSSL, "openssl not installed")
    def test_matches_the_docs_bash_pipeline(self):
        body = G.canonical_json({"cart_mandate": {"contents": {"id": "X"}}}).encode()
        h = G.sign_request(APP_KEY, SECRET, "POST", "/api/v1/merchant/orders", "", body, 1709123456, "a3f1b2c4d5e6")
        bh = subprocess.run(["openssl", "dgst", "-sha256", "-hex"], input=body, capture_output=True).stdout.decode().split()[-1]
        sign_str = f"POST\\n/api/v1/merchant/orders\\n\\n{bh}\\n1709123456\\na3f1b2c4d5e6"
        expanded = subprocess.run(["printf", "%b", sign_str], capture_output=True).stdout
        for secret, same in ((SECRET, True), ("another-secret-value", False)):     # positive + negative control
            sig = subprocess.run(["openssl", "dgst", "-sha256", "-hmac", secret, "-hex"], input=expanded,
                                 capture_output=True).stdout.decode().split()[-1]
            self.assertEqual(sig == h["X-Signature"], same)

    def test_refuses_bad_inputs(self):
        with self.assertRaises(G.HspError):
            G.sign_request(APP_KEY, SECRET, "GET", "/api/v1/checkout/orders/x")
        with self.assertRaises(G.HspError):
            G.sign_request(APP_KEY, SECRET, "GET", "/api/v1/merchant/payments", "?a=b")
        with self.assertRaises(G.HspError):
            G.sign_request(APP_KEY, SECRET, "GET", "/api/v1/merchant/payments", nonce="short")


@unittest.skipUnless(OPENSSL, "openssl not installed")
class Keys(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def _ossl(self, *args):
        subprocess.run(["openssl", *args], check=True, capture_output=True)

    def test_sec1_pkcs8_and_public_agree_with_openssl(self):
        sec1, pk8, pub = (os.path.join(self.d, n) for n in ("k.pem", "k8.pem", "pub.pem"))
        self._ossl("ecparam", "-name", "secp256k1", "-genkey", "-noout", "-out", sec1)
        self._ossl("pkcs8", "-topk8", "-nocrypt", "-in", sec1, "-out", pk8)
        self._ossl("ec", "-in", sec1, "-pubout", "-out", pub)
        a, b = G.parse_private_pem(open(sec1).read()), G.parse_private_pem(open(pk8).read())
        self.assertEqual(a, b)
        key = G.MerchantKey(a)
        self.assertEqual(G.parse_public_pem(open(pub).read()), key.public)
        self.assertEqual(G.parse_public_pem(key.public_pem()), key.public)
        os.chmod(sec1, 0o644)
        with self.assertRaises(G.HspError):           # group/other-readable key file refused
            G.MerchantKey.load(sec1)
        os.chmod(sec1, 0o600)
        self.assertEqual(G.MerchantKey.load(sec1).public, key.public)

    def test_wrong_curve_and_bad_points_refused(self):
        p256 = os.path.join(self.d, "p256.pem")
        self._ossl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", p256)
        with self.assertRaises(G.HspError):
            G.parse_private_pem(open(p256).read())
        pem = test_key().public_pem()
        der = bytearray(base64.b64decode("".join(pem.splitlines()[1:-1])))
        der[-1] ^= 1                                    # point no longer on the curve
        bad = "-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(bytes(der)).decode() + "\n-----END PUBLIC KEY-----\n"
        with self.assertRaises(G.HspError):
            G.parse_public_pem(bad)

    def test_es256k_jwt_verified_by_openssl(self):
        key = test_key()
        pub = os.path.join(self.d, "pub.pem")
        open(pub, "w").write(key.public_pem())
        tok = G.jwt_sign({"a": 1}, key)
        h, p, s = tok.split(".")
        sig = G.b64url_decode(s)

        def i(b):
            b = b.lstrip(b"\0")
            return b"\x02" + bytes([len(b) + (b[0] >= 0x80)]) + (b"\0" if b[0] >= 0x80 else b"") + b
        body = i(sig[:32]) + i(sig[32:])
        open(os.path.join(self.d, "sig"), "wb").write(b"\x30" + bytes([len(body)]) + body)
        for msg, ok in (((h + "." + p).encode(), True), ((h + "." + p + "x").encode(), False)):
            open(os.path.join(self.d, "msg"), "wb").write(msg)
            r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub, "-signature", os.path.join(self.d, "sig"),
                                os.path.join(self.d, "msg")], capture_output=True)
            self.assertEqual(r.returncode == 0, ok)


class Jwt(unittest.TestCase):
    def setUp(self):
        self.key = test_key()
        self.contents = deep(FULL_EXAMPLE)

    def test_roundtrip_and_claims(self):
        tok = G.merchant_authorization(self.contents, "My Store", self.key)
        claims = G.jwt_verify(tok, self.key.public)
        self.assertEqual(claims["aud"], "HashkeyMerchant")
        self.assertTrue(claims["jti"].startswith("JWT-"))
        self.assertTrue(G.check_merchant_claims(claims, self.contents))

    def test_tampered_cart_rejected(self):
        tok = G.merchant_authorization(self.contents, "My Store", self.key)
        claims = G.jwt_verify(tok, self.key.public)
        self.contents["payment_request"]["details"]["total"]["amount"]["value"] = "1.00"
        with self.assertRaisesRegex(G.HspError, "cart_hash"):
            G.check_merchant_claims(claims, self.contents)

    def test_forgeries_rejected(self):
        tok = G.merchant_authorization(self.contents, "My Store", self.key)
        h, p, s = tok.split(".")
        other = G.jwt_sign(G.jwt_verify(tok, self.key.public), test_key())
        forged_claims = G.b64url(json.dumps({**G.jwt_verify(tok, self.key.public), "cart_hash": "00" * 32}).encode())
        cases = [other, h + "." + forged_claims + "." + s,
                 G.b64url(b'{"alg":"none","typ":"JWT"}') + "." + p + ".",
                 G.b64url(b'{"alg":"HS256","typ":"JWT"}') + "." + p + "." + s,
                 h + "." + p + "." + G.b64url(b"\0" * 64), "a.b"]
        for t in cases:
            with self.assertRaises(G.HspError):
                G.jwt_verify(t, self.key.public)

    def test_expired_wrong_aud_wrong_issuer(self):
        now = int(time.time())
        for kw, aud in (({"now": now - 4000}, G.JWT_AUD_DOCS), ({}, "SomebodyElse")):
            claims = G.jwt_verify(G.merchant_authorization(self.contents, "My Store", self.key, **kw), self.key.public)
            with self.assertRaises(G.HspError):
                G.check_merchant_claims(claims, self.contents, aud=aud)
        claims = G.jwt_verify(G.merchant_authorization(self.contents, "Other Store", self.key), self.key.public)
        with self.assertRaisesRegex(G.HspError, "issuer"):
            G.check_merchant_claims(claims, self.contents)


class Cart(unittest.TestCase):
    def test_docs_examples(self):
        self.assertEqual(G.validate_cart_contents(deep(FULL_EXAMPLE)), {0: Decimal("16.00"), 1: Decimal("16.00")})
        c = deep(FULL_EXAMPLE)          # "Different charges per option": index 0 = 10.00, index 1 = 11.00
        det = c["payment_request"]["details"]
        det["display_items"] = [{"label": "Item", "amount": {"currency": "USD", "value": "10.00"}}]
        det["total"]["amount"]["value"] = "10.00"
        det["modifiers"] = [{"supported_methods": "https://www.x402.org", "data": {"method_data_indexes": [1]},
                             "additional_display_items": [{"label": "Base network fee", "amount": {"currency": "USD", "value": "1.00"}}],
                             "total": {"label": "Amount due", "amount": {"currency": "USD", "value": "11.00"}}}]
        self.assertEqual(G.validate_cart_contents(c), {0: Decimal("10.00"), 1: Decimal("11.00")})

    def test_documented_violations_rejected(self):
        def mut(f):
            c = deep(FULL_EXAMPLE)
            f(c)
            return c
        det = lambda c: c["payment_request"]["details"]       # noqa: E731
        mod = lambda c: det(c)["modifiers"][0]                 # noqa: E731
        bad = [
            mut(lambda c: det(c)["total"]["amount"].update(value="14.00")),                     # sum(items) != total
            mut(lambda c: mod(c)["total"]["amount"].update(value="17.00")),                     # modifier total wrong
            mut(lambda c: mod(c).update(additional_display_items=[])),                          # must be non-empty
            mut(lambda c: mod(c)["data"].update(method_data_indexes=[0, 0])),                   # duplicate index
            mut(lambda c: mod(c)["data"].update(method_data_indexes=[2])),                      # out of range
            mut(lambda c: mod(c)["data"].update(method_data_indexes=[])),                       # empty
            mut(lambda c: det(c)["modifiers"].append(deep(det(c)["modifiers"][0]))),            # index matched twice
            mut(lambda c: det(c)["total"]["amount"].update(currency="EUR")),                    # USD only
            mut(lambda c: c["payment_request"]["method_data"][0]["data"].update(x402Version=1)),
            mut(lambda c: c["payment_request"]["method_data"][0]["data"].update(pay_to="0x123")),
            mut(lambda c: det(c)["total"]["amount"].update(value="1e1")),                       # not a decimal string
            mut(lambda c: c.update(cart_expiry="tomorrow")),
            mut(lambda c: c.pop("merchant_name")),
        ]
        for i, c in enumerate(bad):
            with self.assertRaises(G.CartError, msg=f"case {i}"):
                G.validate_cart_contents(c)

    def test_units(self):
        self.assertEqual(G.units_from_usd("0.03", 6), 30000)
        self.assertEqual(G.usd_from_units(25000), "0.025")
        self.assertEqual(G.usd_from_units(30000), "0.03")
        with self.assertRaises(G.CartError):
            G.units_from_usd("0.0000001", 6)

    def test_builder_maps_a_request(self):
        c = G.build_cart_contents(cart_id="hskm-1-ab", payment_request_id="pr-1-ab", merchant_name="Swift Bot Labs",
                                  items=[("GET /v1/hsk/address-profile?address=0x" + "11" * 20, "0.03")],
                                  methods=[G.method_data("hashkey-testnet", "USDC", PAYEE),
                                           G.method_data("base-sepolia", "USDC", PAYEE)])
        d = c["payment_request"]["method_data"]
        self.assertEqual((d[0]["data"]["chain_id"], d[0]["data"]["contract_address"]), (133, "0x8FE3cB719Ee4410E236Cd6b72ab1fCDC06eF53c6"))
        self.assertEqual((d[1]["data"]["chain_id"], d[1]["data"]["contract_address"]), (84532, "0x036CbD53842c5426634e7929541eC2318f3dCF7e"))
        self.assertEqual(c["payment_request"]["details"]["total"]["amount"]["value"], "0.03")


class Config(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "hsp.env")

    def tearDown(self):
        shutil.rmtree(self.d)

    def write(self, text, mode=0o600):
        with open(self.p, "w") as f:
            f.write(text)
        os.chmod(self.p, mode)

    def test_switch(self):
        self.assertIsNone(G.load_config(self.p))                                   # no file: off
        self.write("APP_KEY=REPLACE_ME\nAPP_SECRET=REPLACE_ME\nBASE_URL=REPLACE_ME\n")
        self.assertIsNone(G.load_config(self.p))                                   # template: off
        good = f"APP_KEY={APP_KEY}\nAPP_SECRET={SECRET}\nBASE_URL=https://merchant-qa.hashkeymerchant.com\n"
        self.write(good)
        cfg = G.load_config(self.p)
        self.assertEqual((cfg.base_url, cfg.environment), ("https://merchant-qa.hashkeymerchant.com", "testnet"))
        self.assertNotIn(SECRET, repr(cfg))
        self.write(good, 0o644)
        with self.assertRaises(G.HspError):                                        # readable by others: refused
            G.load_config(self.p)
        os.remove(self.p)
        tgt = os.path.join(self.d, "real.env")
        open(tgt, "w").write(good)
        os.chmod(tgt, 0o600)
        os.symlink(tgt, self.p)
        with self.assertRaises(G.HspError):                                        # symlink: refused
            G.load_config(self.p)

    def test_hosts_and_networks(self):
        for url in ("https://evil.example", "http://merchant-qa.hashkeymerchant.com", "https://merchant-qa.hashkeymerchant.com/x",
                    "https://merchant-stg.hashkeymerchant.com", "https://hsp.hashkey.com", "http://127.0.0.1:9"):
            self.write(f"APP_KEY={APP_KEY}\nAPP_SECRET={SECRET}\nBASE_URL={url}\n")
            with self.assertRaises(G.HspError, msg=url):
                G.load_config(self.p)
        self.write(f"APP_KEY={APP_KEY}\nAPP_SECRET={SECRET}\nBASE_URL=https://merchant-qa.hashkeymerchant.com\nNETWORKS=hashkey\n")
        with self.assertRaises(G.HspError):                                        # mainnet network on the QA gateway
            G.load_config(self.p)


class Webhook(unittest.TestCase):
    EV = {"event_type": "payment", "payment_request_id": "pr-1", "request_id": "req_1", "cart_mandate_id": "hskm-1",
          "status": "payment-included", "tx_signature": "0x" + "ab" * 32}

    def sign(self, ev, ts=None, secret=SECRET):
        raw = json.dumps(ev).encode()
        ts = int(time.time()) if ts is None else ts
        return raw, f"t={ts},v1={G.webhook_signature(secret, ts, raw)}"

    def test_verify(self):
        raw, hdr = self.sign(self.EV)
        ev, _ = G.verify_webhook(raw, hdr, SECRET)
        self.assertEqual(ev["status"], "payment-included")
        bad = [(raw, hdr, "another-secret"), (raw.replace(b"included", b"finalized"), hdr, SECRET),
               (*self.sign(self.EV, ts=int(time.time()) - 400), SECRET), (raw, "", SECRET), (raw, "t=1", SECRET),
               (*self.sign({**self.EV, "status": "payment-successful"}), SECRET),
               (*self.sign({**self.EV, "event_type": "refund"}), SECRET), (raw, hdr, "")]
        for r, h, s in bad:
            with self.assertRaises(G.HspError):
                G.verify_webhook(r, h, s)

    def test_journal_idempotent_and_persistent(self):
        d = tempfile.mkdtemp()
        try:
            j = G.WebhookJournal(os.path.join(d, "w.jsonl"))
            raw, hdr = self.sign(self.EV)
            ev, sig = G.verify_webhook(raw, hdr, SECRET)
            self.assertEqual(j.apply(ev, sig), "applied")
            self.assertEqual(j.apply(ev, sig), "replay")                          # same delivery replayed
            raw2, hdr2 = self.sign(self.EV, ts=int(time.time()) - 1)               # a retry: new t, same status
            self.assertEqual(j.apply(*G.verify_webhook(raw2, hdr2, SECRET)), "duplicate")
            fin = {**self.EV, "status": "payment-finalized"}
            self.assertEqual(j.apply(*G.verify_webhook(*self.sign(fin), SECRET)), "applied")
            safe = {**self.EV, "status": "payment-safe"}                          # late, lower status
            self.assertEqual(j.apply(*G.verify_webhook(*self.sign(safe, ts=int(time.time()) - 2), SECRET)), "stale")
            failed = {**self.EV, "status": "payment-failed"}                      # nothing leaves a terminal state
            self.assertEqual(j.apply(*G.verify_webhook(*self.sign(failed, ts=int(time.time()) - 3), SECRET)), "stale")
            self.assertEqual(G.WebhookJournal(os.path.join(d, "w.jsonl")).latest("req_1")["status"], "payment-finalized")
            self.assertEqual(oct(os.stat(os.path.join(d, "w.jsonl")).st_mode & 0o777), "0o600")
        finally:
            shutil.rmtree(d)


class Records(unittest.TestCase):
    ORDER = {"cart_id": "hskm-1", "payment_request_id": "pr-1", "usd": "0.03",
             "methods": [{"chain_id": 133, "contract": "0x8FE3cB719Ee4410E236Cd6b72ab1fCDC06eF53c6", "pay_to": PAYEE, "decimals": 6}]}
    REC = {"content_id": "hskm-1", "payment_request_id": "pr-1", "status": "payment-safe", "chain": "eip155:133",
           "token_address": "0x8fe3cb719ee4410e236cd6b72ab1fcdc06ef53c6", "to_pay_address": PAYEE, "product_amount": "0.03",
           "order_amount": "0.03", "pay_amount": "0.0301", "tx_signature": "0x" + "cd" * 32, "payer_address": "0x" + "11" * 20}

    def test_checks(self):
        ok, why, m = G.payment_record_ok(self.REC, self.ORDER, payer="0x" + "11" * 20)
        self.assertTrue(ok, why)
        for change, payer in (({"to_pay_address": "0x" + "cc" * 20}, None), ({"token_address": "0x" + "dd" * 20}, None),
                              ({"product_amount": "0.01"}, None), ({"status": "payment-processing"}, None),
                              ({"status": "payment-failed"}, None), ({"content_id": "other"}, None),
                              ({"tx_signature": ""}, None), ({"chain": "eip155:177"}, None), ({}, "0x" + "22" * 20)):
            self.assertFalse(G.payment_record_ok({**self.REC, **change}, self.ORDER, payer=payer)[0], change)
        self.assertFalse(G.payment_record_ok(self.REC, self.ORDER, min_status="payment-finalized")[0])

    def test_onchain_check(self):
        tok, frm = self.ORDER["methods"][0]["contract"], "0x" + "11" * 20
        rc = {"status": "0x1", "logs": [{"address": tok.lower(), "data": hex(30000),
                                         "topics": [G.TRANSFER_TOPIC, "0x" + "0" * 24 + frm[2:], "0x" + "0" * 24 + PAYEE[2:]]}]}
        call = lambda cid, m, p: rc                                                # noqa: E731
        self.assertTrue(G.onchain_transfer_ok(133, "0x" + "cd" * 32, tok, frm, PAYEE, 30000, call=call)[0])
        self.assertFalse(G.onchain_transfer_ok(133, "0x" + "cd" * 32, tok, frm, PAYEE, 30001, call=call)[0])
        self.assertFalse(G.onchain_transfer_ok(133, "0x" + "cd" * 32, tok, "0x" + "22" * 20, PAYEE, 1, call=call)[0])
        self.assertFalse(G.onchain_transfer_ok(133, "0x" + "cd" * 32, tok, frm, PAYEE, 1, call=lambda *a: {**rc, "status": "0x0"})[0])


class Stack(unittest.TestCase):
    """Mock gateway + receiving agent + payment agent, all over HTTP on loopback."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.key = test_key()
        self.gw, self.gsrv = MOCK.start()
        self.gw.webhook_delay = 0.02
        self.cfg = G.HspConfig({"APP_KEY": APP_KEY, "APP_SECRET": SECRET, "BASE_URL": self.gw.base, "PAY_TO": PAYEE},
                               allow_loopback=True)
        self.client = G.Gateway(self.cfg, self.key)
        self.served = []
        self.agent = MA.MerchantAgent(self.cfg, self.client, state_dir=os.path.join(self.d, "state"),
                                      work=lambda path, arg: self.served.append(arg) or {"address": arg, "mock": True})
        self.asrv = MA.serve(self.agent)
        self.abase = f"http://127.0.0.1:{self.asrv.server_address[1]}"
        self.gw.register_app(APP_KEY, SECRET, self.key.public, webhook_url=self.abase + "/hsp/webhook")
        self.payer = Account.create()
        self.policy = self.make_policy()
        self.ledger = PAYER.Ledger(os.path.join(self.d, "ledger.json"))
        self.addr = "0x" + "aB" * 20                     # mixed case, like a checksum address
        self.url = self.abase + "/v1/hsk/address-profile?address=" + self.addr

    def make_policy(self, **kw):
        base = dict(merchants={"Swift Bot Labs": self.key.public}, payees=[PAYEE], networks=["hashkey-testnet", "base-sepolia"],
                    max_payment_usd="0.05", budget_usd="0.20", gateways=[self.gw.base], allow_loopback=True)
        return PAYER.Policy(**{**base, **kw})

    def tearDown(self):
        self.gsrv.shutdown()
        self.asrv.shutdown()
        shutil.rmtree(self.d)

    def contents(self, tag):
        return G.build_cart_contents(cart_id=f"hskm-t-{tag}", payment_request_id=f"pr-t-{tag}", merchant_name="Swift Bot Labs",
                                     items=[("GET /x", "0.03")], methods=[G.method_data("hashkey-testnet", "USDC", PAYEE)])

    def get(self, url, headers, signer=None):
        if "X-HSP-Flow" in headers and "X-HSP-Proof" not in headers:
            headers = {**headers, "X-HSP-Proof": PAYER.delivery_proof(signer or self.payer, headers["X-HSP-Flow"],
                                                                       PAYER.canonical_resource(url))}
        return PAYER.http_get(url, headers)

    # ---- merchant API against the mock
    def test_orders_one_time_and_reusable(self):
        r = self.client.create_order(self.contents("a"))
        self.assertTrue(r["payment_url"].endswith("/flow/" + r["flow_id"]))
        self.assertFalse(r["multi_pay"])
        with self.assertRaises(G.HspApiError) as e:
            self.client.create_order(self.contents("a"))
        self.assertEqual(e.exception.code, 40001)                                  # same cart_mandate_id twice
        with self.assertRaises(G.HspApiError) as e:
            self.client.create_order(self.contents("a"), reusable=True)
        self.assertEqual(e.exception.code, 40002)                                  # one-time vs reusable
        c = self.contents("r")
        c["cart_expiry"] = G.rfc3339(time.time() + 365 * 86400)
        self.assertTrue(self.client.create_order(c, reusable=True)["multi_pay"])
        with self.assertRaises(G.HspApiError) as e:
            self.client.query_payments(payment_request_id="pr-t-a")
        self.assertEqual(e.exception.code, 10003)
        self.assertEqual(self.client.query_payments(reusable=True, cart_mandate_id="hskm-t-r", page=1, page_size=20)["total"], 0)
        with self.assertRaises(G.HspError):
            self.client.query_payments(payment_request_id="a", flow_id="b")      # exactly one identifier

    def test_wrong_hmac_rejected(self):
        bad = G.Gateway(G.HspConfig({"APP_KEY": APP_KEY, "APP_SECRET": "a-wrong-secret-value", "BASE_URL": self.gw.base,
                                     "PAY_TO": PAYEE}, allow_loopback=True), self.key)
        with self.assertRaises(G.HspApiError) as e:
            bad.create_order(self.contents("w"))
        self.assertEqual((e.exception.http_status, e.exception.code), (401, 10002))
        self.client.create_order(self.contents("w"))                               # positive control: right secret works

    def test_replayed_nonce_and_stale_timestamp_rejected(self):
        body = G.canonical_json({"cart_mandate": self.client.sign_cart(self.contents("n"))}).encode()
        path = G.ROUTES["orders"]
        hdr = G.sign_request(APP_KEY, SECRET, "POST", path, "", body)
        self.assertIsInstance(G.http_json("POST", self.gw.base + path, hdr, body), dict)
        with self.assertRaises(G.HspApiError) as e:                                 # identical request replayed
            G.http_json("POST", self.gw.base + path, hdr, body)
        self.assertEqual(e.exception.code, 10002)
        old = G.sign_request(APP_KEY, SECRET, "POST", path, "", body, timestamp=time.time() - 400)
        with self.assertRaises(G.HspApiError) as e:
            G.http_json("POST", self.gw.base + path, old, body)
        self.assertEqual(e.exception.code, 10002)

    def test_tampered_cart_rejected_by_gateway(self):
        cm = self.client.sign_cart(self.contents("t"))
        cm["contents"]["payment_request"]["method_data"][0]["data"]["pay_to"] = "0x" + "ee" * 20
        body = G.canonical_json({"cart_mandate": cm}).encode()
        hdr = G.sign_request(APP_KEY, SECRET, "POST", G.ROUTES["orders"], "", body)  # valid HMAC, tampered cart
        with self.assertRaises(G.HspApiError) as e:
            G.http_json("POST", self.gw.base + G.ROUTES["orders"], hdr, body)
        self.assertEqual(e.exception.code, 10002)
        self.assertIn("cart_hash", e.exception.msg)

    def test_invalid_cart_rejected_locally_first(self):
        c = self.contents("v")
        c["payment_request"]["details"]["total"]["amount"]["value"] = "0.04"
        with self.assertRaises(G.CartError):
            self.client.create_order(c)

    # ---- the A2A flow
    def test_a2a_end_to_end(self):
        st, body = self.get(self.url, {"X-Payer": self.payer.address})
        self.assertEqual(st, 402)
        offer = body["extensions"]["hsp-gateway"]
        st2, body2 = self.get(self.url, {"X-Payer": self.payer.address})            # unpaid order is re-offered
        self.assertEqual(body2["extensions"]["hsp-gateway"]["flow_id"], offer["flow_id"])
        st3, body3 = self.get(self.url, {"X-Payer": self.payer.address, "X-HSP-Flow": offer["flow_id"]})
        self.assertEqual((st3, body3["error"]), (402, "payment_pending"))         # nothing served before payment
        out = PAYER.pay(self.url, account=self.payer, policy=self.policy, ledger=self.ledger, poll_s=10, poll_every=0.05)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["body"]["data"]["mock"], True)
        self.assertEqual(out["body"]["payment"]["confirmed_by"], "webhook")
        self.assertEqual(out["body"]["payment"]["network"], "hashkey-testnet")
        self.assertEqual(self.ledger.get(offer["flow_id"])["state"], "paid")
        rec = self.client.query_payments(flow_id=offer["flow_id"])                  # the merchant API agrees
        self.assertEqual(rec["payer_address"].lower(), self.payer.address.lower())
        time.sleep(0.2)
        self.assertTrue(all(code == 200 for _, code in self.gw.webhook_log), self.gw.webhook_log)
        # the same flow from another payer is not served; the payer gets at most 3 serves
        other = Account.create()
        st, b = self.get(self.url, {"X-Payer": other.address, "X-HSP-Flow": offer["flow_id"]}, signer=other)
        self.assertEqual(st, 404)
        # claiming to be the payer without its signature (or with another wallet's) is refused
        for proof in (None, "0x" + "00" * 65, PAYER.delivery_proof(other, offer["flow_id"], PAYER.canonical_resource(self.url))):
            h = {"X-Payer": self.payer.address, "X-HSP-Flow": offer["flow_id"]}
            st, b = PAYER.http_get(self.url, {**h, **({"X-HSP-Proof": proof} if proof else {})})
            self.assertEqual((st, b["error"]), (401, "proof_required"))
        for _ in range(2):
            self.assertEqual(self.get(self.url, {"X-Payer": self.payer.address, "X-HSP-Flow": offer["flow_id"]})[0], 200)
        self.assertEqual(self.get(self.url, {"X-Payer": self.payer.address, "X-HSP-Flow": offer["flow_id"]})[0], 429)
        self.assertEqual(len(self.served), 3)
        # a new request is a new Cart Mandate (the paid one is not re-offered)
        st, b = self.get(self.url, {"X-Payer": self.payer.address})
        self.assertNotEqual(b["extensions"]["hsp-gateway"]["flow_id"], offer["flow_id"])

    def test_replayed_webhooks_ignored(self):
        self.gw.duplicate_webhooks = True
        out = PAYER.pay(self.url, account=self.payer, policy=self.policy, ledger=self.ledger, poll_s=10, poll_every=0.05)
        self.assertTrue(out["ok"], out)
        time.sleep(0.3)
        rows = [json.loads(x) for x in open(os.path.join(self.d, "state", "hsp_webhooks.jsonl"))]
        self.assertEqual([r["event"]["status"] for r in rows], ["payment-included", "payment-safe", "payment-finalized"])
        self.assertEqual(len(self.gw.webhook_log), 6)                               # 3 statuses x 2 deliveries, all acked
        # a replay of a captured delivery, and a forged one, through the HTTP endpoint
        rec = next(iter(self.gw.payments.values()))
        raw = self.gw.webhook_body(rec)
        ts = int(time.time()) - 5                       # a fresh signature (not one the mock already delivered)
        hdr = f"t={ts},v1={G.webhook_signature(SECRET, ts, raw)}"
        for h, want in ((hdr, (200, "duplicate")), (hdr, (200, "replay")),
                        (f"t={int(time.time())},v1={'0' * 64}", (401, None))):
            req = urllib.request.Request(self.abase + "/hsp/webhook", data=raw, method="POST",
                                         headers={"Content-Type": "application/json", "X-Signature": h})
            try:
                with urllib.request.urlopen(req) as r:
                    got = (r.status, json.loads(r.read()).get("result"))
            except urllib.error.HTTPError as e:
                got = (e.code, None)
            self.assertEqual(got, want)

    def test_failed_payment_not_served(self):
        self.gw.fail_next_payment = True
        out = PAYER.pay(self.url, account=self.payer, policy=self.policy, ledger=self.ledger, poll_s=10, poll_every=0.05)
        self.assertFalse(out["ok"])
        self.assertEqual(out["gateway_status"], "payment-failed")
        st, b = self.get(self.url, {"X-Payer": self.payer.address, "X-HSP-Flow": out["flow_id"]})
        self.assertEqual(st, 402)
        self.assertEqual(self.served, [])

    def test_onchain_check_gates_delivery(self):
        self.agent.chain_check = lambda *a: (False, "no matching token transfer in the transaction")
        out = PAYER.pay(self.url, account=self.payer, policy=self.policy, ledger=self.ledger, poll_s=10, poll_every=0.05)
        self.assertEqual((out["status"], out["body"]["error"]), (409, "onchain_check_failed"))
        self.assertEqual(self.served, [])
        self.agent.chain_check = lambda *a: (True, "transfer found")              # positive control
        st, b = self.get(self.url, {"X-Payer": self.payer.address, "X-HSP-Flow": out["flow_id"]})
        self.assertEqual((st, b["payment"]["confirmed_by"]), (200, "webhook+chain"))

    # ---- the payer refuses before signing
    def offer(self):
        st, body = self.get(self.url, {"X-Payer": self.payer.address})
        return body["extensions"]["hsp-gateway"], "GET /v1/hsk/address-profile?address=" + self.addr

    def test_payer_signs_only_verified_terms(self):
        offer, res = self.offer()
        d = PAYER.evaluate(offer, self.policy, expected_resource=res,
                           checkout=G.checkout_order(self.gw.base, offer["flow_id"]))
        self.assertEqual((d["units"], d["token"]["chain_id"]), (30000, 133))
        m = PAYER.sign_payment_mandate(d, self.payer)
        det = m["payment_mandate_contents"]["payment_response"]["details"]
        self.assertEqual(PAYER.recover_transfer_signer(det, d["token"]), self.payer.address)
        self.assertEqual(det["payload"]["authorization"]["to"], PAYEE)
        self.assertEqual(m["payment_mandate_contents"]["payment_mandate_id"], offer["payment_request_id"])

        def refused(o=None, policy=None, resource=res, checkout=None, match=""):
            with self.assertRaisesRegex(PAYER.Refused, match):
                PAYER.evaluate(o or offer, policy or self.policy, expected_resource=resource, checkout=checkout)
        t = deep(offer)
        t["cart_mandate"]["contents"]["payment_request"]["details"]["display_items"][0]["amount"]["value"] = "0.01"
        t["cart_mandate"]["contents"]["payment_request"]["details"]["total"]["amount"]["value"] = "0.01"
        refused(t, match="cart_hash")                                               # tampered cart
        t = deep(offer)
        t["cart_mandate"]["contents"]["payment_request"]["method_data"][0]["data"]["pay_to"] = "0x" + "ee" * 20
        refused(t, match="cart_hash")                                               # tampered payee
        refused(policy=self.make_policy(merchants={"Swift Bot Labs": test_key().public}), match="differs from the pinned")
        t = deep(offer)
        t.pop("merchant_public_key")
        refused(t, policy=self.make_policy(merchants={"Swift Bot Labs": test_key().public}), match="signature")
        refused(policy=self.make_policy(max_payment_usd="0.02"), match="amount_range")
        refused(policy=self.make_policy(payees=["0x" + "cc" * 20]), match="payee not allowed")
        refused(policy=self.make_policy(networks=["sepolia"]), match="not allowed")
        refused(resource="GET /v1/hsk/address-profile?address=0x" + "22" * 20, match="resource")
        t = deep(offer)
        t["gateway"] = "https://merchant-qa.hashkeymerchant.com"
        refused(t, match="pinned|payment_url")                                      # gateway swapped
        t = deep(offer)
        t["payment_url"] = "https://phish.example/flow/" + offer["flow_id"]
        refused(t, match="payment_url")
        refused(checkout={"payment_request_id": offer["payment_request_id"], "status": "payment-required", "amount": "0.05"},
                match="gateway amount")                                             # gateway asks more than the cart
        refused(checkout={"payment_request_id": "pr-other"}, match="another payment request")
        refused(checkout={"payment_request_id": offer["payment_request_id"], "status": "payment-finalized"}, match="not awaiting")

    def test_payer_refuses_mainnet_and_budget(self):
        offer, res = self.offer()
        t = deep(offer)                                  # a correctly signed mainnet cart is still refused
        c = t["cart_mandate"]["contents"]
        c["payment_request"]["method_data"] = [G.method_data("hashkey", "USDC", PAYEE)]
        t["cart_mandate"]["merchant_authorization"] = G.merchant_authorization(c, "Swift Bot Labs", self.key)
        with self.assertRaisesRegex(PAYER.Refused, "mainnet refused"):
            PAYER.evaluate(t, self.make_policy(networks=["hashkey-testnet", "hashkey"]), expected_resource=res)
        pol = self.make_policy(budget_usd="0.05")
        self.assertTrue(PAYER.pay(self.url, account=self.payer, policy=pol, ledger=self.ledger, poll_s=10, poll_every=0.05)["ok"])
        url2 = self.abase + "/v1/hsk/address-profile?address=0x" + "33" * 20
        with self.assertRaisesRegex(PAYER.Refused, "budget"):
            PAYER.pay(url2, account=self.payer, policy=pol, ledger=self.ledger, poll_s=10, poll_every=0.05)
        self.assertEqual(len(self.gw.payments), 1)                                  # nothing signed or sent for #2

    def test_payer_never_contacts_an_unpinned_gateway(self):
        import http.server
        hits = []

        class Evil(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.path)
                base = f"http://127.0.0.1:{self.server.server_address[1]}"
                offer = {"version": "hsp-gateway/1", "gateway": base, "payment_url": base + "/flow/abcd-1234",
                         "flow_id": "abcd-1234", "payment_request_id": "pr-x", "cart_mandate": {}}
                raw = json.dumps({"extensions": {"hsp-gateway": offer}}).encode()
                self.send_response(402)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        srv = MOCK.Server(("127.0.0.1", 0), Evil)
        __import__("threading").Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/v1/hsk/address-profile?address={self.addr}"
            with self.assertRaisesRegex(PAYER.Refused, "pinned"):
                PAYER.pay(url, account=self.payer, policy=self.policy, ledger=self.ledger, poll_s=1)
            self.assertEqual([h.split("?")[0] for h in hits], ["/v1/hsk/address-profile"])   # no checkout fetch
        finally:
            srv.shutdown()

    def test_paid_checks_rate_limited(self):
        offer, _ = self.offer()
        codes = [self.get(self.url, {"X-Payer": self.payer.address, "X-HSP-Flow": offer["flow_id"]})[0] for _ in range(31)]
        self.assertEqual(codes[:30], [402] * 30)
        self.assertEqual(codes[30], 429)

    def test_real_gateway_submission_stays_off(self):
        qa = "https://merchant-qa.hashkeymerchant.com"
        self.assertIsNone(PAYER.choose_submit_path(qa))                            # undocumented: never guessed live
        self.assertEqual(PAYER.choose_submit_path(qa, "/api/v1/payment/pay-mandate"), "/api/v1/payment/pay-mandate")
        self.assertEqual(PAYER.choose_submit_path(self.gw.base), PAYER.ASSUMED_SUBMIT_PATH)

if __name__ == "__main__":
    unittest.main()
