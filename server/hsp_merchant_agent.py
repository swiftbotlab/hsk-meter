#!/usr/bin/env python3
"""HSK Meter receiving agent on the HashKey Merchant (HSP) gateway rail (Swift Bot Labs).

The same paid resources as server/seller.py (address profile, transaction check), sold through the HSP gateway
instead of our own settlement contract:

  1. GET a paid path with X-Payer -> 402 with extensions["hsp-gateway"]: the request is mapped to a Cart Mandate
     (one display item = the exact resource, total = the endpoint price in USD, one x402 method_data row per configured
     network, from the documented token table), signed with our ES256K merchant key and registered at the gateway
     (POST /api/v1/merchant/orders, HMAC-signed). The answer carries the signed cart, payment_url, flow_id and our
     public key. An unpaid open order for the same payer + resource is re-offered instead of creating another.
  2. The payer's agent (the payment agent, agent/hsp_payer.py) verifies the Cart Mandate, signs a Payment Mandate
     and submits it to the gateway, which executes the transfer on-chain.
  3. GET again with X-Payer + X-HSP-Flow -> served only when the payment for THIS order is confirmed: by a verified
     webhook (POST /hsp/webhook, HMAC "t=,v1=", idempotent journal) or by the HMAC-signed merchant query
     (GET /api/v1/merchant/payments?flow_id=...), with the business fields checked (cart id, token, payee, chain,
     amount, tx hash, payer = X-Payer) and, with --verify-onchain, the token Transfer found in the transaction on
     the testnet chain itself. A paid order is served at most 3 times within 1 h (lost-answer retries).
HSP mode is OFF unless secrets/hsp/hsp.env holds APP_KEY / APP_SECRET / BASE_URL. Listens on loopback only; the
gateway needs an HTTPS webhook URL, so a tunnel in front of /hsp/webhook is an operator step (not done here).

  hsp_merchant_agent.py [--port 8403] [--state-dir DIR] [--verify-onchain] [--rpc https://mainnet.hsk.xyz]
"""
import argparse
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import hsp_gateway as G  # noqa: E402

PATHS = {   # same resources and on-chain prices (USDC.e base units) as server/seller.py endpoints 1 and 2
    "/v1/hsk/address-profile": {"eid": 1, "param": "address", "re": re.compile(r"^0x[0-9a-fA-F]{40}$"), "units": 30000},
    "/v1/hsk/tx-receipt": {"eid": 2, "param": "hash", "re": re.compile(r"^0x[0-9a-fA-F]{64}$"), "units": 25000},
}
MAX_SERVES, SERVE_WINDOW = 3, 3600
ORDER_TTL = 1800


class Limiter:
    def __init__(self, limit, window):
        self.limit, self.window, self.hits, self.lock = limit, window, {}, threading.Lock()

    def hit(self, key):
        now = time.time()
        with self.lock:
            if len(self.hits) > 5000:
                self.hits.clear()
            q = [t for t in self.hits.get(key, []) if now - t < self.window]
            if len(q) >= self.limit:
                self.hits[key] = q
                return int(self.window - (now - q[0])) + 1
            q.append(now)
            self.hits[key] = q
            return 0


class MerchantAgent:
    def __init__(self, cfg, gateway, *, work, state_dir, chain_check=None, now=time.time):
        self.cfg, self.gw, self.work, self.chain_check, self.now = cfg, gateway, work, chain_check, now
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        self.orders_path = os.path.join(state_dir, "hsp_orders.jsonl")
        self.journal = G.WebhookJournal(os.path.join(state_dir, "hsp_webhooks.jsonl"))
        self.lock = threading.Lock()
        self.orders, self.by_open = {}, {}         # flow_id -> order ; (payer, resource) -> flow_id
        self.per_ip, self.per_payer, self.global_orders = Limiter(60, 60), Limiter(10, 60), Limiter(60, 60)
        self.paid_checks = Limiter(30, 60)        # each X-HSP-Flow check may query the gateway
        if os.path.exists(self.orders_path):
            with open(self.orders_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("kind") == "order":
                        self.orders[row["order"]["flow_id"]] = row["order"]
                        self.by_open[(row["order"]["payer"], row["order"]["resource"])] = row["order"]["flow_id"]
                    elif row.get("kind") == "served" and row["flow_id"] in self.orders:
                        self.orders[row["flow_id"]].setdefault("served", []).append(row["t"])
                        self.orders[row["flow_id"]]["paid"] = True

    def _append(self, row):
        fd = os.open(self.orders_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    # ------------------------------------------------------------ requests
    def handle_get(self, path, query, headers, ip):
        spec = PATHS[path]
        q = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=3)
        if list(q) != [spec["param"]] or len(q[spec["param"]]) != 1 or not spec["re"].match(q[spec["param"]][0]):
            return 400, {"ok": False, "error": "bad_request", "message": f"GET {path}?{spec['param']}=<0x...>"}, {}
        arg = q[spec["param"]][0].lower()
        resource = f"GET {path}?{spec['param']}={arg}"
        payer = headers.get("X-Payer")
        if not payer or not G.ADDR_RE.match(payer):
            return 400, {"ok": False, "error": "payer_required", "message": "send X-Payer: 0x..."}, {}
        payer = payer.lower()
        flow = headers.get("X-HSP-Flow")
        if flow is not None:
            if not G.FLOW_RE.match(flow):
                return 400, {"ok": False, "error": "bad_flow"}, {}
            wait = self.paid_checks.hit(ip)
            if wait:
                return 429, {"ok": False, "error": "rate_limited"}, {"Retry-After": str(wait)}
            if not self.proof_ok(payer, flow, resource, headers.get("X-HSP-Proof")):
                return 401, {"ok": False, "error": "proof_required",
                             "message": "X-HSP-Proof: EIP-191 signature by X-Payer over the delivery message"}, {}
            return self.serve_paid(path, arg, resource, payer, flow)
        wait = self.per_ip.hit(ip)
        if wait:
            return 429, {"ok": False, "error": "rate_limited"}, {"Retry-After": str(wait)}
        return self.offer(path, spec, resource, payer)

    @staticmethod
    def proof_ok(payer, flow, resource, proof):
        """Only the wallet that paid collects: an EIP-191 signature by X-Payer over flow id + resource."""
        if not isinstance(proof, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", proof):
            return False
        from eth_account import Account
        from eth_account.messages import encode_defunct
        msg = f"HSK Meter HSP delivery\nflow: {flow}\nresource: {resource}"
        try:
            return Account.recover_message(encode_defunct(text=msg), signature=proof).lower() == payer
        except Exception:
            return False

    def offer(self, path, spec, resource, payer):
        now = self.now()
        with self.lock:
            fid = self.by_open.get((payer, resource))
            o = self.orders.get(fid) if fid else None
            if o and not o.get("paid") and o["expires"] - now > 120:
                return 402, self._offer_body(o), {}
        for lim, key in ((self.per_payer, payer), (self.global_orders, "all")):
            wait = lim.hit(key)
            if wait:
                return 429, {"ok": False, "error": "rate_limited", "message": "too many new orders"}, {"Retry-After": str(wait)}
        usd = G.usd_from_units(spec["units"], 6)
        tag = secrets.token_hex(8)
        methods = [G.method_data(n, "USDC", self.cfg.pay_to) for n in self.cfg.networks]
        contents = G.build_cart_contents(cart_id=f"hskm-{spec['eid']}-{tag}", payment_request_id=f"pr-{spec['eid']}-{tag}",
                                         merchant_name=self.cfg.merchant_name, items=[(resource, usd)],
                                         methods=methods, expiry_s=ORDER_TTL, now=now)
        try:
            r = self.gw.create_order(contents)
        except G.HspApiError as e:
            return 502, {"ok": False, "error": "gateway_error", "code": e.code, "message": e.msg}, {"Retry-After": "5"}
        if not r["flow_id"]:
            return 502, {"ok": False, "error": "gateway_error", "message": "payment_url carries no flow id"}, {}
        o = {"flow_id": r["flow_id"], "payment_url": r["payment_url"], "payment_request_id": r["payment_request_id"],
             "cart_id": contents["id"], "cart_mandate": r["cart_mandate"], "payer": payer, "resource": resource,
             "path": path, "usd": usd, "expires": int(G.rfc3339_ts(contents["cart_expiry"])),
             "methods": [{"chain_id": m["data"]["chain_id"], "contract": m["data"]["contract_address"],
                          "pay_to": m["data"]["pay_to"], "decimals": G.token_for(m["data"]["network"], "USDC")["decimals"],
                          "network": m["data"]["network"]} for m in methods]}
        with self.lock:
            if len(self.orders) > 20000:          # forget orders that can no longer be paid or served (memory bound)
                for fid in [f for f, x in self.orders.items() if x["expires"] + 2 * SERVE_WINDOW < now]:
                    x = self.orders.pop(fid)
                    if self.by_open.get((x["payer"], x["resource"])) == fid:
                        del self.by_open[(x["payer"], x["resource"])]
            self.orders[o["flow_id"]] = o
            self.by_open[(payer, resource)] = o["flow_id"]
            self._append({"kind": "order", "order": o})
        return 402, self._offer_body(o), {}

    def _offer_body(self, o):
        return {"ok": False, "error": "payment_required", "x402Version": 2,
                "accepts": [{"scheme": "hsp-gateway", "network": f"eip155:{m['chain_id']}", "asset": m["contract"],
                             "payTo": m["pay_to"], "amountUsd": o["usd"]} for m in o["methods"]],
                "extensions": {"hsp-gateway": {
                    "version": "hsp-gateway/1", "gateway": self.cfg.base_url, "payment_url": o["payment_url"],
                    "flow_id": o["flow_id"], "payment_request_id": o["payment_request_id"], "multi_pay": False,
                    "cart_mandate": o["cart_mandate"], "merchant_public_key": self.gw.key.public_pem(),
                    "resource": o["resource"], "retry_header": "X-HSP-Flow", "proof_header": "X-HSP-Proof",
                    "proof_message": "HSK Meter HSP delivery\nflow: <flow_id>\nresource: <resource> (EIP-191, by X-Payer)",
                    "how": "verify the Cart Mandate, sign the Payment Mandate, pay through the gateway, then GET again "
                           "with X-HSP-Flow: <flow_id> and X-HSP-Proof"}}}

    def payment_for(self, o):
        """(record, source): a verified webhook first, else the HMAC-signed merchant query."""
        for ev in self.journal.by_cart(o["cart_id"]):
            ok, _, _ = G.payment_record_ok(ev, o, self.cfg.min_status, o["payer"])
            if ok:
                return ev, "webhook"
        try:
            rec = self.gw.query_payments(flow_id=o["flow_id"])
        except G.HspApiError as e:
            if e.code == 10003:
                return None, "none"
            raise
        return rec, "query"

    def serve_paid(self, path, arg, resource, payer, flow):
        with self.lock:
            o = self.orders.get(flow)
        if not o or o["payer"] != payer or o["resource"] != resource:
            return 404, {"ok": False, "error": "unknown_order", "message": "no order of ours for this payer and resource"}, {}
        now = self.now()
        served = o.get("served") or []
        if served and (len(served) >= MAX_SERVES or now - served[0] > SERVE_WINDOW):
            return 429, {"ok": False, "error": "already_served", "message": f"served {len(served)} times"}, {}
        try:
            rec, src = self.payment_for(o)
        except G.HspApiError as e:
            return 502, {"ok": False, "error": "gateway_error", "code": e.code}, {"Retry-After": "3"}
        ok, why, method = G.payment_record_ok(rec, o, self.cfg.min_status, payer)
        if not ok:
            if why.startswith("pending") or rec is None or why == "no transaction hash yet":
                return 402, {"ok": False, "error": "payment_pending", "status": (rec or {}).get("status")}, {"Retry-After": "2"}
            return 402 if why == "payment-failed" else 409, {"ok": False, "error": "payment_not_accepted", "reason": why}, {}
        if self.chain_check:
            try:
                units = G.units_from_usd(o["usd"], method["decimals"])
                good, cwhy = self.chain_check(method["chain_id"], rec["tx_signature"], method["contract"], payer,
                                              method["pay_to"], units)
            except G.HspError as e:
                good, cwhy = False, str(e)
            if not good:
                return 409, {"ok": False, "error": "onchain_check_failed", "reason": cwhy}, {"Retry-After": "5"}
        try:
            data = self.work(path, arg)
        except Exception as e:      # paid but not deliverable: no refund API is documented, so the order stays servable
            return 502, {"ok": False, "error": "upstream_unavailable", "paid": True, "retryable": True,
                         "message": type(e).__name__}, {"Retry-After": "5"}
        with self.lock:
            o["paid"] = True
            o.setdefault("served", []).append(now)
            self._append({"kind": "served", "flow_id": flow, "t": now})
        return 200, {"ok": True, "data": data,
                     "payment": {"rail": "hsp-gateway", "flow_id": flow, "payment_request_id": o["payment_request_id"],
                                 "cart_mandate_id": o["cart_id"], "status": rec["status"], "tx": rec["tx_signature"],
                                 "network": method["network"], "confirmed_by": src + ("+chain" if self.chain_check else "")}}, {}

    def webhook(self, raw, header):
        try:
            ev, sig = G.verify_webhook(raw, header, self.cfg.app_secret)
        except G.HspError as e:
            msg = str(e)
            return (401 if "signature" in msg or "timestamp" in msg or "X-Signature" in msg else 400), {"code": 1, "msg": msg[:80]}
        result = self.journal.apply(ev, sig)
        return 200, {"code": 0, "result": result}


def make_handler(agent):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version, timeout = "HTTP/1.1", 10

        def log_message(self, *a):
            pass

        def _send(self, st, body, hdr=None):
            raw = json.dumps(body, default=str).encode()
            self.send_response(st)
            for k, v in {"Content-Type": "application/json", "Content-Length": str(len(raw)), "Cache-Control": "no-store",
                         "X-Content-Type-Options": "nosniff", **(hdr or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            try:
                if len(self.path) > 400:
                    self._send(414, {"ok": False, "error": "uri_too_long"})
                elif u.path in PATHS:
                    self._send(*agent.handle_get(u.path, u.query, self.headers, self.client_address[0]))
                elif u.path == "/health":
                    self._send(200, {"ok": True, "rail": "hsp-gateway", "gateway": agent.cfg.base_url,
                                     "networks": agent.cfg.networks, "merchant": agent.cfg.merchant_name})
                else:
                    self._send(404, {"ok": False, "error": "not_found"})
            except Exception as e:      # never leak internals
                self._send(500, {"ok": False, "error": "internal", "type": type(e).__name__})

        def do_POST(self):
            if urllib.parse.urlsplit(self.path).path != "/hsp/webhook":
                return self._send(404, {"code": 1, "msg": "not found"})
            n = self.headers.get("Content-Length")
            if not n or not n.isdigit() or int(n) > G.MAX_WEBHOOK_BODY:
                return self._send(413, {"code": 1, "msg": "body missing or too large"})
            raw = self.rfile.read(int(n))
            self._send(*agent.webhook(raw, self.headers.get("X-Signature")))

    return H


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    request_queue_size = 64


def serve(agent, port=0):
    srv = Server(("127.0.0.1", port), make_handler(agent))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8403)
    ap.add_argument("--env", default=G.DEFAULT_ENV)
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "run", "hsp"))
    ap.add_argument("--rpc", default="https://mainnet.hsk.xyz", help="where the sold data is read (HashKey mainnet)")
    ap.add_argument("--verify-onchain", action="store_true", help="also find the token Transfer on the testnet chain")
    a = ap.parse_args()
    cfg = G.load_config(a.env)
    if cfg is None:
        sys.exit("HSP mode is off: fill APP_KEY, APP_SECRET and BASE_URL in " + a.env)
    import chain as C
    import seller as S
    import egress
    rpc = C.Rpc(a.rpc)
    hosts = [(urllib.parse.urlsplit(a.rpc).hostname, 443), (urllib.parse.urlsplit(cfg.base_url).hostname, 443)]
    if a.verify_onchain:
        hosts += [(urllib.parse.urlsplit(u).hostname, 443) for u in G.TESTNET_RPC.values()]
    egress.install(hosts)
    agent = MerchantAgent(cfg, G.Gateway(cfg), work=lambda path, arg: S.WORK[path](rpc, arg), state_dir=a.state_dir,
                          chain_check=G.onchain_transfer_ok if a.verify_onchain else None)
    srv = Server(("127.0.0.1", a.port), make_handler(agent))
    print(json.dumps({"listening": f"127.0.0.1:{a.port}", "gateway": cfg.base_url, "networks": cfg.networks}), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
