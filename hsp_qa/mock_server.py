"""Local mock of the HSP QA MCP server (tests and --mock runs only; binds 127.0.0.1).

Implements MCP streamable HTTP (initialize + Mcp-Session-Id, notifications -> 202, tools/list, tools/call, JSON or
SSE responses) and the eight HSP tools with the shapes the guide documents: create_order -> flow_id /
payment_request_id / payment_url, prepare_payment -> eip712 / cart_hash / token_total_raw / quote_expires_at /
payment_payload / payment_mandate_contents, finalize_payment -> payment_mandate_json, submit_payment ->
payment-submitted, query_payment -> statuses + attribution_verified + payment.tx_signature. Error 30043 on an
expired quote. Knobs on MockState let tests tamper payloads, delay tools, force HTTP errors and so on.

Run standalone:  python3 mock_server.py [port]
"""
import hashlib
import json
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CANONICAL_PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
MOCK_PERMIT2_SPENDER = "0x4020615294c913F045dc10f0a5cdEbd86c280001"
X402 = "https://www.x402.org/"


def _iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def default_chain_config():
    return {"networks": [
        {"network": "hashkey-testnet", "chain_id": 133, "tokens": [
            {"coin": "USDC", "address": "0x8FE3cB719Ee4410E236Cd6b72ab1fCDC06eF53c6", "decimals": 6,
             "payment_protocol": "eip3009", "eip712": {"name": "USD Coin", "version": "2"}},
            {"coin": "USDT", "address": "0x372325443233fEbaC1F6998aC750276468c83CC6", "decimals": 6,
             "payment_protocol": "permit2", "permit2_spender": MOCK_PERMIT2_SPENDER}]},
        {"network": "hashkey", "chain_id": 177, "tokens": [
            {"coin": "USDC", "address": "0x054ed45810DbBAb8B27668922D110669c9D88D0a", "decimals": 6,
             "payment_protocol": "eip3009"}]},
    ]}


DEFAULT_SEQ = ["payment-submitted", "payment-verified", "payment-processing", "payment-included",
               "payment-finalized"]


class MockState:
    def __init__(self):
        self.lock = threading.Lock()
        self.chain_config = default_chain_config()
        self.sessions = set()
        self.orders, self.quotes, self.payments = {}, {}, {}
        self.calls = []
        self.quote_ttl = 300
        self.valid_s = 300                 # validBefore = now + valid_s
        self.gas_fee_advanced = True
        self.gas_fee_usd = Decimal("0.002")
        self.tamper = None                 # callable(prep) -> None, mutates prepare_payment output
        self.submit_30043 = 0
        self.finalize_30043 = 0
        self.status_seq = list(DEFAULT_SEQ)
        self.delay = {}                    # tool name -> seconds
        self.sse = False
        self.structured = True
        self.error_style = "isError"       # or "envelope"
        self.force_http = None             # e.g. 403
        self.redirect = False
        self.oversize = False
        self.schema_variant = "default"    # "alt": payer_address + string-typed payloads
        self.query_errors = 0
        self.clock = time.time
        self.hits = 0
        self.hide_tools = set()

    def now(self):
        return self.clock()


class ToolErr(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def _schemas(variant):
    alt = variant == "alt"
    obj = {"type": "string"} if alt else {"type": "object"}
    s = lambda props, req: {"type": "object", "properties": props, "required": req}
    return [
        {"name": "get_chain_config", "description": "Supported networks/tokens", "inputSchema": s({}, [])},
        {"name": "create_order", "description": "Create an order",
         "inputSchema": s({"merchant_name": {"type": "string"}, "pay_to": {"type": "string"},
                           "cart_mandate": {"type": "object"}}, ["merchant_name", "pay_to", "cart_mandate"])},
        {"name": "get_order", "inputSchema": s({"flow_id": {"type": "string"}}, ["flow_id"])},
        {"name": "prepare_payment",
         "inputSchema": s({"flow_id": {"type": "string"},
                           ("payer_address" if alt else "payer"): {"type": "string"},
                           "protocol": {"type": "string", "enum": ["eip3009", "permit2"]}},
                          ["flow_id", "payer_address" if alt else "payer", "protocol"])},
        {"name": "finalize_payment",
         "inputSchema": s({"signature": {"type": "string"}, "payment_payload": obj,
                           "payment_mandate_contents": obj, "cart_hash": {"type": "string"}},
                          ["signature", "payment_payload", "payment_mandate_contents", "cart_hash"])},
        {"name": "estimate_fee", "inputSchema": s({"flow_id": {"type": "string"}}, ["flow_id"])},
        {"name": "submit_payment",
         "inputSchema": s({"payment_mandate_json": {"type": "string"}}, ["payment_mandate_json"])},
        {"name": "query_payment",
         "inputSchema": s({"flow_id": {"type": "string"}, "payment_request_id": {"type": "string"},
                           "expected_merchant_name": {"type": "string"}}, ["expected_merchant_name"])},
    ]


def _token_for(st, network, contract):
    for n in st.chain_config["networks"]:
        for t in n["tokens"]:
            if n["network"] == network and t["address"].lower() == contract.lower():
                return n, t
    raise ToolErr(10001, "token not supported")


def _as_obj(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            raise ToolErr(10001, "expected JSON") from None
    return v


# ----------------------------------------------------------------- tools
def t_get_chain_config(st, a):
    return st.chain_config


def t_create_order(st, a):
    name, pay_to, cart = a.get("merchant_name"), a.get("pay_to"), _as_obj(a.get("cart_mandate"))
    if not isinstance(name, str) or not name:
        raise ToolErr(10001, "merchant_name required")
    if not isinstance(pay_to, str) or len(pay_to) != 42 or not pay_to.startswith("0x"):
        raise ToolErr(10001, "pay_to must be a 42-character 0x address")
    if not isinstance(cart, dict) or "merchant_authorization" in cart:
        raise ToolErr(10001, "cart_mandate invalid (do not provide merchant_authorization)")
    c = cart.get("contents") or {}
    pr = c.get("payment_request") or {}
    md = pr.get("method_data") or []
    tot = ((pr.get("details") or {}).get("total") or {}).get("amount") or {}
    if not c.get("id") or not c.get("cart_expiry") or not md or tot.get("currency") != "USD":
        raise ToolErr(10001, "cart_mandate missing required fields")
    for m in md:
        if m.get("supported_methods") != X402 or (m.get("data") or {}).get("x402Version") != 2:
            raise ToolErr(10001, "method_data must be x402 v2")
        m["data"]["pay_to"] = pay_to
    d = md[0]["data"]
    _token_for(st, d.get("network"), d.get("contract_address"))
    flow_id = "flow_" + secrets.token_hex(8)
    pr_id = "PR" + secrets.token_hex(8)
    st.orders[flow_id] = {"flow_id": flow_id, "payment_request_id": pr_id, "merchant_name": name, "pay_to": pay_to,
                          "usd": Decimal(str(tot.get("value"))), "method": d, "contents": c}
    return {"flow_id": flow_id, "payment_request_id": pr_id, "payment_url": f"https://mock.invalid/pay/{flow_id}"}


def _order(st, a):
    o = st.orders.get(a.get("flow_id")) or next(
        (x for x in st.orders.values() if x["payment_request_id"] == a.get("payment_request_id")), None)
    if not o:
        raise ToolErr(40004, "order not found")
    return o


def t_get_order(st, a):
    o = _order(st, a)
    return {"flow_id": o["flow_id"], "payment_request_id": o["payment_request_id"], "merchant_name": o["merchant_name"],
            "pay_to": o["pay_to"], "status": "payment-required",
            "amount": {"currency": "USD", "value": format(o["usd"], "f")},
            "network": o["method"]["network"], "coin": o["method"]["coin"], "gas_fee_advanced": st.gas_fee_advanced}


def t_estimate_fee(st, a):
    _order(st, a)
    return {"gas_fee": format(st.gas_fee_usd, "f"), "gas_fee_advanced": st.gas_fee_advanced}


def t_prepare_payment(st, a):
    o = _order(st, a)
    payer = a.get("payer") or a.get("payer_address")
    proto = a.get("protocol")
    if not isinstance(payer, str) or len(payer) != 42:
        raise ToolErr(10001, "payer required")
    net, tok = _token_for(st, o["method"]["network"], o["method"]["contract_address"])
    if proto != tok["payment_protocol"]:
        raise ToolErr(10001, f"protocol must be {tok['payment_protocol']}")
    now = int(st.now())
    usd = o["usd"] + (Decimal(0) if st.gas_fee_advanced else st.gas_fee_usd)
    raw = int(usd * (Decimal(10) ** tok["decimals"]))
    chain_id = net["chain_id"]
    dom_t = [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
             {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}]
    if proto == "eip3009":
        e = tok.get("eip712") or {"name": "USD Coin", "version": "2"}
        auth = {"from": payer, "to": o["pay_to"], "value": str(raw), "validAfter": str(now - 5),
                "validBefore": str(now + st.valid_s), "nonce": "0x" + secrets.token_hex(32)}
        eip712 = {"domain": {"name": e["name"], "version": e["version"], "chainId": chain_id,
                             "verifyingContract": tok["address"]},
                  "types": {"EIP712Domain": dom_t,
                            "TransferWithAuthorization": [
                                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
                  "primaryType": "TransferWithAuthorization", "message": dict(auth)}
        payload_inner = {"signature": "", "authorization": auth}
    else:
        spender = tok.get("permit2_spender", MOCK_PERMIT2_SPENDER)
        eip712 = {"domain": {"name": "Permit2", "chainId": chain_id, "verifyingContract": CANONICAL_PERMIT2},
                  "types": {"EIP712Domain": [dom_t[0], dom_t[2], dom_t[3]],
                            "PermitTransferFrom": [{"name": "permitted", "type": "TokenPermissions"},
                                                   {"name": "spender", "type": "address"},
                                                   {"name": "nonce", "type": "uint256"},
                                                   {"name": "deadline", "type": "uint256"}],
                            "TokenPermissions": [{"name": "token", "type": "address"},
                                                 {"name": "amount", "type": "uint256"}]},
                  "primaryType": "PermitTransferFrom",
                  "message": {"permitted": {"token": tok["address"], "amount": str(raw)}, "spender": spender,
                              "nonce": str(int.from_bytes(secrets.token_bytes(8), "big")),
                              "deadline": str(now + st.valid_s)}}
        payload_inner = {"signature": "", "permit2Authorization": eip712["message"]}
    cart_hash = hashlib.sha256(_canon(o["contents"]).encode()).hexdigest()
    pdid = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d%H%M%S") + f"{secrets.randbelow(10**6):06d}"
    prep = {
        "eip712": eip712, "cart_hash": cart_hash, "token_total_raw": str(raw),
        "quote_expires_at": _iso(now + st.quote_ttl), "pay_to": o["pay_to"], "payer": payer,
        "payment_request_id": o["payment_request_id"],
        "payment_payload": {"x402Version": 2, "payload": payload_inner,
                            "accepted": {"scheme": "exact", "network": f"eip155:{chain_id}", "asset": tok["address"],
                                         "amount": str(raw), "payTo": o["pay_to"], "maxTimeoutSeconds": st.valid_s,
                                         "extra": {"protocol": proto}}},
        "payment_mandate_contents": {"payment_mandate_id": o["payment_request_id"], "payment_details_id": pdid,
                                     "payment_details_total": {"label": "Total",
                                                               "amount": {"currency": "USD", "value": str(usd)}},
                                     "merchant_agent": "HashkeyMerchant", "timestamp": _iso(now)},
    }
    st.quotes[o["flow_id"]] = {"expires": now + st.quote_ttl, "cart_hash": cart_hash, "payer": payer,
                               "eip712": json.loads(json.dumps(eip712)), "raw": raw}
    if st.tamper:
        st.tamper(prep)
    st.quotes[o["flow_id"]]["eip712"] = prep["eip712"]
    return prep


def _verify_sig(eip712, sig):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    return Account.recover_message(encode_typed_data(full_message=eip712), signature=sig)


def t_finalize_payment(st, a):
    sig, cart_hash = a.get("signature"), a.get("cart_hash")
    pp, pmc = _as_obj(a.get("payment_payload")), _as_obj(a.get("payment_mandate_contents"))
    flow = next((f for f, q in st.quotes.items() if q["cart_hash"] == cart_hash), None)
    if not flow:
        raise ToolErr(10001, "unknown cart_hash")
    q = st.quotes[flow]
    if st.finalize_30043 > 0:
        st.finalize_30043 -= 1
        raise ToolErr(30043, "quote expired, prepare again")
    if st.now() > q["expires"]:
        raise ToolErr(30043, "quote expired, prepare again")
    if not isinstance(sig, str) or len(sig) != 132 or not sig.startswith("0x"):
        raise ToolErr(10001, "signature must be 65 bytes 0x-hex")
    try:
        signer = _verify_sig(q["eip712"], sig)
    except Exception:
        raise ToolErr(30010, "signature invalid") from None
    if signer.lower() != q["payer"].lower():
        raise ToolErr(30010, "signature not from payer")
    if not isinstance(pp, dict) or (pp.get("payload") or {}).get("signature") not in ("", None):
        raise ToolErr(10001, "payment_payload must be unchanged")
    if not isinstance(pmc, dict) or not pmc.get("payment_mandate_id"):
        raise ToolErr(10001, "payment_mandate_contents must be unchanged")
    pp = json.loads(json.dumps(pp))
    pp["payload"]["signature"] = sig
    mandate = {"flow_id": flow, "payment_mandate_contents": pmc, "payment_payload": pp,
               "payment_hash": hashlib.sha256(_canon(pmc).encode()).hexdigest(),
               "user_authorization": "eyJhbGciOiJFUzI1NksifQ.mock." + secrets.token_hex(16)}
    q["finalized"] = True
    return {"payment_mandate_json": json.dumps(mandate, separators=(",", ":"))}


def t_submit_payment(st, a):
    m = _as_obj(a.get("payment_mandate_json"))
    if not isinstance(m, dict) or m.get("flow_id") not in st.quotes:
        raise ToolErr(10001, "payment_mandate_json invalid")
    flow = m["flow_id"]
    q = st.quotes[flow]
    if not q.get("finalized"):
        raise ToolErr(10001, "not finalized")
    if flow in st.payments:
        raise ToolErr(30050, "payment already submitted for this flow")
    if st.submit_30043 > 0:
        st.submit_30043 -= 1
        raise ToolErr(30043, "quote expired, prepare again")
    if st.now() > q["expires"]:
        raise ToolErr(30043, "quote expired, prepare again")
    o = st.orders[flow]
    st.payments[flow] = {"idx": 0, "tx": "0x" + secrets.token_hex(32), "payer": q["payer"], "order": o}
    return {"status": "payment-submitted", "flow_id": flow, "payment_request_id": o["payment_request_id"]}


def t_query_payment(st, a):
    if st.query_errors > 0:
        st.query_errors -= 1
        raise ToolErr(50000, "temporary query failure")
    o = _order(st, a)
    p = st.payments.get(o["flow_id"])
    if not p:
        return {"status": "payment-required", "attribution_verified": False,
                "payment": {"status": "payment-required", "tx_signature": ""}}
    status = st.status_seq[min(p["idx"], len(st.status_seq) - 1)]
    p["idx"] += 1
    included = st.status_seq.index(status) >= (st.status_seq.index("payment-included")
                                               if "payment-included" in st.status_seq else 0)
    return {"status": status, "flow_id": o["flow_id"], "payment_request_id": o["payment_request_id"],
            "attribution_verified": a.get("expected_merchant_name") == o["merchant_name"],
            "payment": {"status": status, "tx_signature": p["tx"] if included and status != "payment-failed" else "",
                        "network": o["method"]["network"], "to_pay_address": o["pay_to"],
                        "payer_address": p["payer"],
                        "status_reason": "insufficient balance" if status == "payment-failed" else ""}}


TOOLS = {f.__name__[2:]: f for f in (t_get_chain_config, t_create_order, t_get_order, t_estimate_fee,
                                      t_prepare_payment, t_finalize_payment, t_submit_payment, t_query_payment)}


# ----------------------------------------------------------------- HTTP / MCP
class Handler(BaseHTTPRequestHandler):
    st = None  # set by make_server
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        st = self.st
        if st.force_http:
            return self._send(st.force_http, b"<html>Forbidden</html>", "text/html")
        if self.path == "/health":
            return self._send(200, b'{"status":"ok"}')
        self._send(405, b'{"error":"method"}')

    def do_DELETE(self):
        self.st.sessions.discard(self.headers.get("Mcp-Session-Id"))
        self._send(204)

    def do_POST(self):
        st = self.st
        st.hits += 1
        if st.force_http:
            return self._send(st.force_http, b"<html>Forbidden</html>", "text/html")
        if st.redirect:
            return self._send(302, b"", headers={"Location": "https://evil.invalid/"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            return self._send(413, b'{"error":"too large"}')
        try:
            msg = json.loads(self.rfile.read(n))
        except ValueError:
            return self._send(400, b'{"error":"bad json"}')
        method, rid = msg.get("method"), msg.get("id")
        if rid is None:                                  # notification
            return self._send(202)
        headers = {}
        if method == "initialize":
            sid = "mock-" + secrets.token_hex(12)
            st.sessions.add(sid)
            headers["Mcp-Session-Id"] = sid
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "hsp-qa-mock", "version": "1"}}
            return self._reply(rid, result=result, headers=headers)
        if self.headers.get("Mcp-Session-Id") not in st.sessions:
            return self._send(404, b'{"error":"unknown session"}')
        if method == "tools/list":
            return self._reply(rid, result={"tools": [t for t in _schemas(st.schema_variant)
                                                      if t["name"] not in st.hide_tools]})
        if method != "tools/call":
            return self._reply(rid, error={"code": -32601, "message": "method not found"})
        p = msg.get("params") or {}
        name, args = p.get("name"), p.get("arguments") or {}
        fn = TOOLS.get(name)
        if not fn:
            return self._reply(rid, error={"code": -32602, "message": f"unknown tool {name}"})
        with st.lock:
            st.calls.append((name, json.loads(json.dumps(args))))
        if st.delay.get(name):
            time.sleep(st.delay[name])
        try:
            with st.lock:
                out = fn(st, args)
            text = json.dumps(out)
            if st.oversize:
                text = json.dumps({"pad": "x" * 3_000_000})
            result = {"content": [{"type": "text", "text": text}]}
            if st.structured and not st.oversize:
                result["structuredContent"] = out
        except ToolErr as e:
            err = {"code": e.code, "msg": e.msg}
            if st.error_style == "envelope":
                result = {"content": [{"type": "text", "text": json.dumps({**err, "data": None})}]}
            else:
                result = {"isError": True, "content": [{"type": "text", "text": json.dumps(err)}]}
        return self._reply(rid, result=result)

    def _reply(self, rid, result=None, error=None, headers=None):
        msg = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        body = json.dumps(msg).encode()
        if self.st.sse:
            note = json.dumps({"jsonrpc": "2.0", "method": "notifications/message",
                               "params": {"level": "info", "data": "working"}})
            sse = (": ping\n\nevent: message\ndata: " + note + "\n\nevent: message\ndata: " + body.decode()
                   + "\n\n").encode()
            return self._send(200, sse, "text/event-stream", headers)
        return self._send(200, body, "application/json", headers)


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            super().handle_error(request, client_address)   # clients that time out and hang up are expected


def make_server(port=0, state=None):
    st = state or MockState()
    handler = type("BoundHandler", (Handler,), {"st": st})
    srv = _Server(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, st, f"http://127.0.0.1:{srv.server_address[1]}/"


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv, st, url = make_server(port)
    print(f"mock HSP MCP server at {url}  (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
