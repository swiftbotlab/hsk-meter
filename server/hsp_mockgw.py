#!/usr/bin/env python3
"""Local mock of the HashKey Merchant (HSP) gateway, for tests only (Swift Bot Labs). Loopback, in-memory, no chain.

Implements what the docs specify, and checks it the way the docs say the gateway does:
  - /api/v1/merchant/* : HMAC-SHA256 (X-App-Key, X-Signature, X-Timestamp +-300 s, X-Nonce unique per app for 5 min),
    body hash = SHA-256 of the canonical JSON of the body. Errors: 10002 (auth), 10001 (params), 40001 (duplicate
    one-time cart), 40002 (one-time vs reusable), 10003 (not found). Envelope {code, msg, data}.
  - POST /merchant/orders and /orders/reusable: the cart rules (validate_cart_contents) and the ES256K
    merchant_authorization checked against the merchant public key registered for the app (cart_hash, aud, exp).
  - GET /merchant/payments and /payments/reusable: exactly one query parameter; PaymentItemResponse records.
  - GET /api/v1/checkout/orders/{flow_id}: no auth (route from the official Go SDK).
  - Payer side, no auth, in the hosted checkout's wire format as found in public prior art (UNVERIFIED against a
    live gateway; the docs' diagram names only pay-mandate and flow/:id): GET /api/v1/payment/cart-mandate/flow/{id}
    ({cartmandate, multi_pay}), POST /api/v1/payment/pay-mandate and /pay-mandate/multi-pay ({flow_id,
    payment_mandate}), GET /api/v1/payment/flow/{id}[?payment_details_id=]. pay-mandate checks every field the
    checkout sends: the x402 v2 payment_payload (base64) against the cart's method row and the token's EIP-712
    domain, the EIP-3009 signature, amount and validity, and user_authorization (HS256 keyed by the payer address,
    aud, iss = signer, transaction_data = the two hashes for cart_hash_scope). Then it "executes" (a fake tx hash,
    no chain) and sends signed webhooks ("t=<unix>,v1=<hmac>" over "<t>.<raw body>") for payment-included / -safe /
    -finalized to the app's webhook_url.
Knobs: fail_next_payment, duplicate_webhooks (deliver each webhook twice, like a retry), webhook_delay,
cart_hash_scope ("contents" or "cart_mandate": which cart hash transaction_data[0] must carry).
"""
import http.server
import json
import os
import re
import secrets
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "agent"))
import hsp_gateway as G  # noqa: E402

SKEW = 300


class Fail(Exception):
    def __init__(self, http, code, msg):
        self.http, self.code, self.msg = http, code, msg


class MockGateway:
    def __init__(self):
        self.apps = {}              # app_key -> {secret, pub, webhook_url}
        self.nonces = {}            # (app_key, nonce) -> ts
        self.orders = {}            # cart_id -> order
        self.flows = {}             # flow_id -> cart_id
        self.payments = {}          # payment key (request_id) -> record
        self.used_auth_nonces = set()
        self.lock = threading.Lock()
        self.fail_next_payment = False
        self.duplicate_webhooks = False
        self.webhook_delay = 0.05
        self.webhook_log = []       # (status, http status of the merchant's answer)
        self.cart_hash_scope = "contents"
        self.submissions = []       # raw pay-mandate bodies as received (tests inspect the wire)
        self.base = None

    def register_app(self, app_key, secret, merchant_pub, webhook_url=None):
        self.apps[app_key] = {"secret": secret, "pub": merchant_pub, "webhook_url": webhook_url}

    # ------------------------------------------------------------ auth
    def check_hmac(self, method, path, query, headers, body_obj):
        k, sig, ts, nonce = (headers.get(h) for h in ("X-App-Key", "X-Signature", "X-Timestamp", "X-Nonce"))
        app = self.apps.get(k or "")
        if not app or not sig or not ts or not nonce:
            raise Fail(401, 10002, "Unauthorized")
        if not re.fullmatch(r"[0-9]{1,12}", ts) or abs(time.time() - int(ts)) > SKEW:
            raise Fail(401, 10002, "timestamp out of window")
        if not re.fullmatch(r"[0-9a-fA-F]{12,32}|[0-9a-fA-F-]{36}", nonce):
            raise Fail(401, 10002, "bad nonce")
        bh = "" if body_obj is None else G.sha256_hex(G.canonical_json(body_obj))
        want = G.hmac_hex(app["secret"], G.signing_message(method, path, query, bh, int(ts), nonce))
        if not G.hmac.compare_digest(want, sig):
            raise Fail(401, 10002, "signature mismatch")
        with self.lock:
            now = time.time()
            for key, t in list(self.nonces.items()):
                if now - t > SKEW:
                    del self.nonces[key]
            if (k, nonce) in self.nonces:
                raise Fail(401, 10002, "nonce reused")
            self.nonces[(k, nonce)] = now
        return k

    # ------------------------------------------------------------ merchant API
    def create_order(self, app_key, body, reusable):
        if not isinstance(body, dict) or not isinstance(body.get("cart_mandate"), dict):
            raise Fail(400, 10001, "cart_mandate required")
        cm = body["cart_mandate"]
        contents = cm.get("contents")
        try:
            eff = G.validate_cart_contents(contents)
        except G.CartError as e:
            raise Fail(400, 10001, f"Invalid parameters: {e}")
        try:
            claims = G.jwt_verify(cm.get("merchant_authorization"), self.apps[app_key]["pub"])
            G.check_merchant_claims(claims, contents, aud=G.JWT_AUD_DOCS)
        except G.HspError as e:
            raise Fail(401, 10002, f"merchant_authorization: {e}")
        cid = contents["id"]
        with self.lock:
            if cid in self.orders:
                if self.orders[cid]["reusable"] != reusable:
                    raise Fail(400, 40002, "Type mismatch")
                raise Fail(400, 40001, "Invalid mandate state")
            flow = str(__import__("uuid").uuid4())
            prid = contents["payment_request"]["details"]["id"]
            self.orders[cid] = {"app_key": app_key, "cart_mandate": cm, "reusable": reusable, "flow_id": flow,
                                "payment_request_id": prid, "eff": eff, "status": "payment-required"}
            self.flows[flow] = cid
        return {"payment_request_id": prid, "payment_url": f"{self.base}/flow/{flow}", "multi_pay": reusable}

    def query(self, app_key, params, reusable):
        allowed = {"cart_mandate_id", "flow_id", "request_id"} if reusable else {"cart_mandate_id", "payment_request_id", "flow_id"}
        ids = {k: v for k, v in params.items() if k not in ("page", "page_size")}
        if len(ids) != 1 or set(ids) - allowed:
            raise Fail(400, 10001, "exactly one query parameter")
        (k, v), = ids.items()
        with self.lock:
            rows = [p for p in self.payments.values() if p["app_key"] == app_key and
                    self.orders[p["content_id"]]["reusable"] == reusable]
        rows = [p for p in rows if {"cart_mandate_id": p["content_id"], "payment_request_id": p["payment_request_id"],
                                    "flow_id": p["flow_id"], "request_id": p["request_id"]}[k] == v]
        rows.sort(key=lambda p: p["created_at"], reverse=True)
        public = [{x: y for x, y in p.items() if x != "app_key"} | {"app_key": p["app_key"]} for p in rows]
        if reusable:
            if k == "request_id":
                if not public:
                    raise Fail(404, 10003, "Not found")
                return public[0]
            page, size = int(params.get("page", 1)), int(params.get("page_size", 20))
            return {"list": public[(page - 1) * size: page * size], "total": len(public), "page": page, "page_size": size}
        if k == "cart_mandate_id":
            return public
        if not public:
            raise Fail(404, 10003, "Not found")
        return public[0]

    # ------------------------------------------------------------ checkout side (assumed shapes)
    def checkout(self, flow_id):
        with self.lock:
            cid = self.flows.get(flow_id)
            if not cid:
                raise Fail(404, 10003, "Not found")
            o = self.orders[cid]
            amt = min(o["eff"].values())
            return {"payment_request_id": o["payment_request_id"], "cartmandate": o["cart_mandate"],
                    "status": o["status"], "amount": format(amt, "f")}

    def cart_mandate_flow(self, flow_id):
        """GET /api/v1/payment/cart-mandate/flow/{id}: the order as the hosted checkout reads it."""
        with self.lock:
            cid = self.flows.get(flow_id)
            if not cid:
                raise Fail(404, 10003, "Not found")
            o = self.orders[cid]
            return {"cartmandate": o["cart_mandate"], "multi_pay": o["reusable"]}

    PM_KEYS = {"payment_mandate_id", "payment_details_id", "payment_details_total", "payment_response", "merchant_agent",
               "timestamp"}
    PR_KEYS = {"request_id", "method_name", "details", "shipping_address", "shipping_option", "payer_name",
               "payer_email", "payer_phone"}

    def pay_mandate(self, body, multi=False):
        import hsp_payer as PAYER
        self.submissions.append(body)
        if not isinstance(body, dict) or set(body) != {"flow_id", "payment_mandate"} or \
                not isinstance(body["payment_mandate"], dict) or \
                set(body["payment_mandate"]) != {"payment_mandate_contents", "user_authorization"}:
            raise Fail(400, 10001, "body must be {flow_id, payment_mandate: {payment_mandate_contents, user_authorization}}")
        flow = body.get("flow_id")
        with self.lock:
            cid = self.flows.get(flow) if isinstance(flow, str) else None
            if not cid:
                raise Fail(404, 10003, "Not found")
            o = self.orders[cid]
            if o["reusable"] != multi:
                raise Fail(400, 10001, "reusable orders use pay-mandate/multi-pay, one-time orders pay-mandate")
            if not o["reusable"] and o["status"] != "payment-required":
                raise Fail(400, 40001, "Invalid mandate state")
        pm = body["payment_mandate"]["payment_mandate_contents"]
        contents = o["cart_mandate"]["contents"]
        if G.rfc3339_ts(contents["cart_expiry"]) < time.time():
            raise Fail(400, 40001, "Invalid mandate state")
        if not isinstance(pm, dict) or set(pm) != self.PM_KEYS:
            raise Fail(400, 10001, "payment_mandate_contents fields")
        pr = pm["payment_response"]
        if not isinstance(pr, dict) or set(pr) != self.PR_KEYS or \
                any(pr[k] is not None for k in self.PR_KEYS - {"request_id", "method_name", "details"}):
            raise Fail(400, 10001, "payment_response fields")
        if pm["payment_mandate_id"] != o["payment_request_id"] or pr["request_id"] != o["payment_request_id"]:
            raise Fail(400, 10001, "payment mandate is for another payment request")
        if pm["merchant_agent"] != G.CHECKOUT_MERCHANT_AGENT:
            raise Fail(400, 10001, "merchant_agent")
        if not isinstance(pm["payment_details_id"], str) or not re.fullmatch(r"[0-9]{20}", pm["payment_details_id"]):
            raise Fail(400, 10001, "payment_details_id")
        if not isinstance(pm["timestamp"], str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", pm["timestamp"]):
            raise Fail(400, 10001, "timestamp")
        tot = contents["payment_request"]["details"]["total"]
        t = pm["payment_details_total"]
        if not isinstance(t, dict) or set(t) != {"label", "amount", "pending"} or t["label"] != tot["label"] or \
                t["amount"] != {"value": tot["amount"]["value"], "currency": "USD"} or not isinstance(t["pending"], bool):
            raise Fail(400, 10001, "payment_details_total must repeat the cart total")
        det = pr["details"]
        if not isinstance(det, dict) or set(det) != {"payment_payload"}:
            raise Fail(400, 10001, "details must be {payment_payload}")
        try:
            x = G.decode_payment_payload(det["payment_payload"])
        except G.HspError as e:
            raise Fail(400, 10001, f"payment_payload: {e}")
        acc, pl = x.get("accepted"), x.get("payload")
        if x.get("x402Version") != 2 or not isinstance(acc, dict) or not isinstance(pl, dict) or \
                not isinstance(pl.get("authorization"), dict) or acc.get("scheme") != "exact":
            raise Fail(400, 10001, "payment_payload is not an x402 v2 exact payment")
        md = contents["payment_request"]["method_data"]
        net = re.fullmatch(r"eip155:([0-9]{1,12})", str(acc.get("network", "")))
        idx = next((i for i, m in enumerate(md) if net and m["data"]["chain_id"] == int(net.group(1)) and
                    m["data"]["contract_address"].lower() == str(acc.get("asset", "")).lower()), None)
        if idx is None:
            raise Fail(400, 10001, "no method_data row for this network and asset")
        m = md[idx]["data"]
        if pr["method_name"] != md[idx]["supported_methods"]:
            raise Fail(400, 10001, "method_name")
        row = G.token_by_contract(m["chain_id"], m["contract_address"])
        extra = acc.get("extra") or {}
        if row is None or row["eip712"] is None or extra.get("protocol") != "eip3009" or \
                (extra.get("name"), extra.get("version")) != row["eip712"]:
            raise Fail(400, 10001, "accepted.extra must name eip3009 and the token's EIP-712 domain")
        a = pl["authorization"]
        try:
            signer = PAYER.recover_transfer_signer(x, row)
        except Exception:
            raise Fail(400, 30001, "bad authorization signature")
        need = G.units_from_usd(format(o["eff"][idx], "f"), row["decimals"])
        now = time.time()
        if signer.lower() != str(a.get("from", "")).lower():
            raise Fail(400, 30001, "authorization not signed by from")
        if str(a.get("to", "")).lower() != m["pay_to"].lower() or str(acc.get("payTo", "")).lower() != m["pay_to"].lower():
            raise Fail(400, 10001, "authorization pays another address")
        if str(acc.get("amount")) != str(a.get("value")) or int(a.get("value", 0)) < need:
            raise Fail(400, 10001, "authorization value below the cart total")
        if not int(a["validAfter"]) <= now < int(a["validBefore"]):
            raise Fail(400, 10001, "authorization outside its validity window")
        try:
            u = G.hs256_verify(body["payment_mandate"]["user_authorization"])        # key = the iss address
        except G.HspError as e:
            raise Fail(400, 30001, f"user_authorization: {e}")
        if u.get("aud") != G.CHECKOUT_AUD or str(u.get("iss")).lower() != signer.lower() or \
                not isinstance(u.get("exp"), int) or u["exp"] < now or not isinstance(u.get("iat"), int) or \
                u["iat"] > now + 60 or not re.fullmatch(r"0x[0-9a-f]{64}", str(u.get("nonce"))):
            raise Fail(400, 30001, "user_authorization claims")
        if u.get("transaction_data") != G.transaction_data(o["cart_mandate"], pm, self.cart_hash_scope):
            raise Fail(400, 30001, "user_authorization does not bind this cart and mandate")
        with self.lock:
            if a["nonce"] in self.used_auth_nonces:
                raise Fail(400, 10004, "Conflict")
            self.used_auth_nonces.add(a["nonce"])
            failed = self.fail_next_payment
            self.fail_next_payment = False
            tx = "0x" + secrets.token_hex(32)
            amount = Decimal(int(a["value"])).scaleb(-row["decimals"])
            rec = {"app_key": o["app_key"], "content_id": cid, "payment_request_id": o["payment_request_id"],
                   "request_id": pm.get("payment_details_id"), "flow_id": flow, "status": "payment-submitted",
                   "product_amount": format(o["eff"][idx], "f"), "order_amount": format(o["eff"][idx], "f"),
                   "pay_amount": format(amount, "f"), "usd_amount": format(o["eff"][idx], "f"), "usd_amount_rate": "1",
                   "token": row["coin"], "token_address": row["contract"], "payer_address": signer,
                   "to_pay_address": m["pay_to"], "chain": f"eip155:{row['chain_id']}", "network": row["network"],
                   "extra_protocol": "eip3009", "gas_fee_advanced": True, "tx_signature": "",
                   "created_at": G.rfc3339(now), "updated_at": G.rfc3339(now)}
            self.payments[rec["request_id"]] = rec
            o["status"] = "payment-submitted"
        threading.Thread(target=self._execute, args=(rec["request_id"], tx, failed), daemon=True).start()
        return {"metadata": {"status": "payment-submitted"}}      # the checkout reads data.metadata.status

    def _execute(self, key, tx, failed):
        steps = ["payment-failed"] if failed else ["payment-included", "payment-safe", "payment-finalized"]
        for st in steps:
            time.sleep(self.webhook_delay)
            with self.lock:
                rec = self.payments[key]
                rec["status"], rec["updated_at"] = st, G.rfc3339(time.time())
                if st != "payment-failed":
                    rec["tx_signature"] = tx
                    rec.setdefault("included_at", rec["updated_at"])
                if st == "payment-finalized":
                    rec["completed_at"] = rec["updated_at"]
                o = self.orders[rec["content_id"]]
                if not o["reusable"]:
                    o["status"] = st
                app = self.apps[rec["app_key"]]
            self._webhook(app, rec)

    def webhook_body(self, rec):
        ev = {"event_type": "payment", "payment_request_id": rec["payment_request_id"], "request_id": rec["request_id"],
              "cart_mandate_id": rec["content_id"], "payer_address": rec["payer_address"],
              "to_pay_address": rec["to_pay_address"], "amount": rec["pay_amount"], "order_amount": rec["order_amount"],
              "product_amount": rec["product_amount"], "pay_amount": rec["pay_amount"], "usd_amount": rec["usd_amount"],
              "token": rec["token"], "token_address": rec["token_address"], "chain": rec["chain"],
              "network": rec["network"], "status": rec["status"], "created_at": rec["created_at"],
              "status_reason": "mock"}
        if rec.get("tx_signature"):
            ev["tx_signature"] = rec["tx_signature"]
        return json.dumps(ev).encode()

    def _webhook(self, app, rec):
        if not app.get("webhook_url"):
            return
        raw = self.webhook_body(rec)
        ts = int(time.time())
        hdr = f"t={ts},v1={G.webhook_signature(app['secret'], ts, raw)}"
        for _ in range(2 if self.duplicate_webhooks else 1):
            req = urllib.request.Request(app["webhook_url"], data=raw, method="POST",
                                         headers={"Content-Type": "application/json", "X-Signature": hdr})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
            except OSError:
                code = None
            self.webhook_log.append((rec["status"], code))

    def flow(self, flow_id, payment_details_id=None):
        with self.lock:
            cid = self.flows.get(flow_id)
            if not cid:
                raise Fail(404, 10003, "Not found")
            recs = [p for p in self.payments.values() if p["flow_id"] == flow_id and
                    payment_details_id in (None, p["request_id"])]
            o = self.orders[cid]
        if not recs:
            return {"payment_id": "", "status": o["status"], "amount": format(min(o["eff"].values()), "f")}
        r = max(recs, key=lambda p: p["updated_at"])
        return {"payment_id": r["request_id"], "status": r["status"], "tx_signature": r.get("tx_signature", ""),
                "amount": r["pay_amount"], "token": r["token"], "network": r["network"], "payer_address": r["payer_address"],
                "to_pay_address": r["to_pay_address"], "created_at": r["created_at"], "updated_at": r["updated_at"]}


def make_handler(gw):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, http_status, env):
            raw = json.dumps(env).encode()
            self.send_response(http_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _route(self, method):
            u = urllib.parse.urlsplit(self.path)
            body_obj = None
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 1 << 20:
                    raise Fail(400, 10001, "body too large")
                try:
                    body_obj = G.loads_strict(self.rfile.read(n))
                except G.HspError:
                    raise Fail(400, 10001, "Invalid parameters")
            params = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
            p = u.path
            if p.startswith("/api/v1/merchant/"):
                app = gw.check_hmac(method, p, u.query, self.headers, body_obj)
                if method == "POST" and p == G.ROUTES["orders"]:
                    return gw.create_order(app, body_obj, False)
                if method == "POST" and p == G.ROUTES["orders_reusable"]:
                    return gw.create_order(app, body_obj, True)
                if method == "GET" and p == G.ROUTES["payments"]:
                    return gw.query(app, params, False)
                if method == "GET" and p == G.ROUTES["payments_reusable"]:
                    return gw.query(app, params, True)
            m = re.fullmatch(r"/api/v1/checkout/orders/([A-Za-z0-9-]{4,64})", p)
            if method == "GET" and m:
                return gw.checkout(m.group(1))
            if method == "POST" and p == G.PAYER_ROUTES["pay_mandate"]:
                return gw.pay_mandate(body_obj, multi=False)
            if method == "POST" and p == G.PAYER_ROUTES["pay_mandate_multi"]:
                return gw.pay_mandate(body_obj, multi=True)
            m = re.fullmatch(r"/api/v1/payment/cart-mandate/flow/([A-Za-z0-9-]{4,64})", p)
            if method == "GET" and m:
                return gw.cart_mandate_flow(m.group(1))
            m = re.fullmatch(r"/api/v1/payment/flow/([A-Za-z0-9-]{4,64})", p)
            if method == "GET" and m:
                if set(params) - {"payment_details_id"}:
                    raise Fail(400, 10001, "Invalid parameters")
                return gw.flow(m.group(1), params.get("payment_details_id"))
            raise Fail(404, 10003, "Not found")

        def _do(self, method):
            try:
                data = self._route(method)
                self._send(200, {"code": 0, "msg": "success", "data": data})
            except Fail as f:
                self._send(f.http, {"code": f.code, "msg": f.msg})

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

    return H


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def start(gw=None):
    gw = gw or MockGateway()
    srv = Server(("127.0.0.1", 0), make_handler(gw))
    gw.base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return gw, srv


def demo():
    """The whole A2A flow offline: mock gateway + receiving agent + payment agent, ephemeral keys, no chain, no funds."""
    import tempfile
    import hsp_merchant_agent as MA
    import hsp_payer as PAYER
    from eth_account import Account
    payee = "0xA8A49A6Ca872D54a8FD5FB121139De9bff62a48D"
    gw, gsrv = start()
    key = G.MerchantKey(int.from_bytes(os.urandom(32), "big") % (G.N - 1) + 1)
    cfg = G.HspConfig({"APP_KEY": "ak_demo_0001", "APP_SECRET": secrets.token_hex(16), "BASE_URL": gw.base, "PAY_TO": payee},
                      allow_loopback=True)
    state = tempfile.mkdtemp(prefix="hsp-demo-")
    agent = MA.MerchantAgent(cfg, G.Gateway(cfg, key), state_dir=state,
                             work=lambda path, arg: {"address": arg, "note": "demo data (the real agent reads HashKey Chain)"})
    asrv = MA.serve(agent)
    gw.register_app(cfg.app_key, cfg.app_secret, key.public, webhook_url=f"http://127.0.0.1:{asrv.server_address[1]}/hsp/webhook")
    payer = Account.create()
    url = f"http://127.0.0.1:{asrv.server_address[1]}/v1/hsk/address-profile?address=0x78C8476772BC209C3df09Da5e129d0879cd57F8c"
    st, body = PAYER.http_get(url, {"X-Payer": payer.address})
    offer = body["extensions"]["hsp-gateway"]
    c = offer["cart_mandate"]["contents"]
    claims = json.loads(G.b64url_decode(offer["cart_mandate"]["merchant_authorization"].split(".")[1]))
    print("1. receiving agent -> 402, Cart Mandate registered at the gateway (POST /api/v1/merchant/orders, HMAC)")
    print(json.dumps({"cart_mandate_id": c["id"], "payment_request_id": offer["payment_request_id"], "flow_id": offer["flow_id"],
                      "total_usd": c["payment_request"]["details"]["total"]["amount"]["value"],
                      "methods": [(m["data"]["network"], m["data"]["coin"]) for m in c["payment_request"]["method_data"]],
                      "merchant_authorization": {"alg": "ES256K", "aud": claims["aud"], "cart_hash": claims["cart_hash"][:16] + "..."}},
                     indent=1))
    policy = PAYER.Policy(merchants={cfg.merchant_name: key.public}, payees=[payee], networks=["hashkey-testnet", "base-sepolia"],
                          max_payment_usd="0.05", budget_usd="0.20", gateways=[gw.base], allow_loopback=True)
    out = PAYER.pay(url, account=payer, policy=policy, ledger=PAYER.Ledger(os.path.join(state, "ledger.json")),
                    poll_s=10, poll_every=0.05)
    time.sleep(0.2)
    rec = next(iter(gw.payments.values()))
    print("2. payment agent verified the cart (pinned merchant key, cart_hash, rules, AP2 constraints), signed the "
          "Payment Mandate and submitted it (POST /api/v1/payment/pay-mandate):")
    pmw = gw.submissions[-1]["payment_mandate"]
    pmc = pmw["payment_mandate_contents"]
    x = G.decode_payment_payload(pmc["payment_response"]["details"]["payment_payload"])
    ua = json.loads(G.b64url_decode(pmw["user_authorization"].split(".")[1]))
    print(json.dumps({"constraints": out["constraints"],
                      "payment_mandate_contents": {k: pmc[k] for k in ("payment_mandate_id", "payment_details_id",
                                                                       "payment_details_total", "merchant_agent")},
                      "payment_payload (base64 x402 v2, decoded)": {
                          "accepted": x["accepted"], "authorization": {k: x["payload"]["authorization"][k]
                                                                       for k in ("from", "to", "value", "validBefore")}},
                      "user_authorization (HS256, claims)": {"aud": ua["aud"], "iss": ua["iss"],
                                                              "transaction_data": [h[:16] + "..." for h in ua["transaction_data"]]},
                      "gateway record": {"payer": rec["payer_address"], "to": rec["to_pay_address"],
                                         "chain": rec["chain"], "amount": rec["pay_amount"]}}, indent=1))
    print("3. gateway executed it and signed webhooks to the receiving agent:", [s for s, _ in gw.webhook_log],
          "acks", [c for _, c in gw.webhook_log])
    print("4. GET with X-HSP-Flow ->", out["status"], json.dumps({k: out["body"].get(k) for k in ("data", "payment")}, indent=1))
    gsrv.shutdown()
    asrv.shutdown()
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["demo"]:
        sys.exit(demo())
    print("usage: hsp_mockgw.py demo     (tests import start() from server/test_hsp_gateway.py)")
