"""HSP Agentic Payment flow over the official MCP tools.

get_chain_config -> create_order -> get_order -> (estimate_fee) -> prepare_payment -> FIREWALL -> balance check ->
sign -> finalize_payment -> submit_payment -> query_payment every POLL_INTERVAL_S until payment-safe / -finalized /
-failed. Quote expiry or error 30043 -> re-prepare and re-sign (never resubmit an old signature). An uncertain
submission is queried, never blindly resubmitted. Every step is appended to a receipts JSONL file.
"""
import fcntl
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

import config
import firewall
from mcp_client import McpError, McpRpcError, McpTimeout, McpToolError

ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
ADDR_RE = firewall.ADDR_RE

ALIASES = {
    "merchant_name": ("merchant_name", "merchantName"),
    "pay_to": ("pay_to", "payTo"),
    "cart_mandate": ("cart_mandate", "cartMandate"),
    "flow_id": ("flow_id", "flowId"),
    "payer": ("payer", "payer_address", "payerAddress"),
    "protocol": ("protocol", "payment_protocol", "paymentProtocol"),
    "signature": ("signature", "wallet_signature"),
    "payment_payload": ("payment_payload", "paymentPayload"),
    "payment_mandate_contents": ("payment_mandate_contents", "paymentMandateContents"),
    "cart_hash": ("cart_hash", "cartHash"),
    "payment_mandate_json": ("payment_mandate_json", "paymentMandateJson", "payment_mandate"),
    "payment_request_id": ("payment_request_id", "paymentRequestId"),
    "expected_merchant_name": ("expected_merchant_name", "expectedMerchantName"),
}


class HspError(Exception):
    def __init__(self, code, message, raw=None):
        super().__init__(f"HSP error {code}: {message}")
        self.code = None if code is None else str(code)
        self.message = message
        self.raw = raw


class MissingTools(Exception):
    pass


class SchemaMismatch(Exception):
    pass


class FlowStop(Exception):
    """Stop the run deliberately (firewall refusal, funding needed, approval withheld, caps)."""

    def __init__(self, reason, **info):
        super().__init__(reason)
        self.reason = reason
        self.info = info


def utc_iso(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snip(v, n=300):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    s = "".join(ch if ch.isprintable() else "?" for ch in s)
    return s[:n] + ("..." if len(s) > n else "")


def _h(v):
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- errors / envelopes
_ENVELOPE = {"code", "msg", "message", "data", "success", "trace_id", "traceId", "request_id", "requestId", "error"}
_OK_CODES = {"0", "200", "success", "SUCCESS", "ok", "OK"}


def unwrap(payload):
    """Strip a {code,msg,data} envelope; raise HspError for a non-zero code. Other shapes pass through."""
    if isinstance(payload, dict) and "code" in payload and set(payload) <= _ENVELOPE:
        if str(payload.get("code")) in _OK_CODES and payload.get("success") is not False:
            d = payload.get("data")
            return d if isinstance(d, dict) else ({"value": d} if d is not None else {})
        raise HspError(payload.get("code"), _snip(payload.get("msg") or payload.get("message") or ""), payload)
    return payload


def as_hsp_error(exc):
    """Map an MCP-level error to HspError, extracting an HSP numeric code where one is present."""
    if isinstance(exc, HspError):
        return exc
    if isinstance(exc, McpToolError):
        p = exc.payload if isinstance(exc.payload, dict) else {}
        code = None
        for k in ("code", "err_code", "error_code", "errorCode"):
            if p.get(k) is not None:
                code = p[k]
                break
        if code is None and isinstance(p.get("error"), dict):
            code = p["error"].get("code")
        err = p.get("error") if isinstance(p.get("error"), dict) else {}
        msg = p.get("msg") or p.get("message") or err.get("message") or p.get("text") or ""
        if code is None:
            m = re.search(r"\b(\d{5})\b", json.dumps(p, default=str))
            code = m.group(1) if m else None
        return HspError(code, _snip(msg or json.dumps(p, default=str)), p)
    if isinstance(exc, McpRpcError):
        code = None
        if isinstance(exc.data, dict):
            code = exc.data.get("code")
        if code is None:
            m = re.search(r"\b(\d{5})\b", f"{exc.message} {json.dumps(exc.data, default=str)}")
            code = m.group(1) if m else exc.code
        return HspError(code, _snip(exc.message), exc.data)
    return HspError(None, _snip(str(exc)))


def is_quote_error(err):
    if err.code == "30043":
        return True
    m = (err.message or "").lower()
    return "quote" in m and ("expire" in m or "invalid" in m)


# ---------------------------------------------------------------- schema-adaptive tool access
def _coerce(v, prop):
    t = prop.get("type") if isinstance(prop, dict) else None
    if t == "string" and isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    if t == "object" and isinstance(v, str):
        try:
            p = json.loads(v)
            return p if isinstance(p, dict) else v
        except ValueError:
            return v
    return v


class Hsp:
    def __init__(self, mcp):
        self.mcp = mcp
        self.tools = {}

    def discover(self):
        tools = self.mcp.list_tools()
        self.tools = {t["name"]: t for t in tools}
        missing = [n for n in config.REQUIRED_TOOLS if n not in self.tools]
        if missing:
            raise MissingTools(f"required HSP tools missing: {missing}")
        return tools

    def args_for(self, tool, logical, need):
        schema = (self.tools.get(tool) or {}).get("inputSchema") or {}
        props = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(props, dict) or not props:
            return {k: v for k, v in logical.items() if v is not None}
        out, unmapped = {}, []
        for k, v in logical.items():
            if v is None:
                continue
            name = next((a for a in ALIASES.get(k, (k,)) if a in props), None)
            if name is None:
                if k in need:
                    unmapped.append(k)
                continue
            out[name] = _coerce(v, props[name])
        req = [r for r in (schema.get("required") or []) if isinstance(r, str)]
        missing = [r for r in req if r not in out]
        if unmapped or missing:
            raise SchemaMismatch(f"{tool}: cannot map {unmapped} / schema requires {missing}; live properties "
                                 f"{sorted(props)[:20]}")
        return out

    def call(self, tool, need=(), total_timeout=None, **logical):
        args = self.args_for(tool, logical, need)
        try:
            payload = self.mcp.call_tool(tool, args, total_timeout=total_timeout)
        except (McpToolError, McpRpcError) as e:
            raise as_hsp_error(e) from None
        return unwrap(payload)


# ---------------------------------------------------------------- chain config
def _chain_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.split(":", 1)[1] if v.startswith("eip155:") else v
        if s.isdigit():
            return int(s)
        if re.match(r"^0x[0-9a-fA-F]+$", s):
            return int(s, 16)
    return None


def _extra_addrs(d, words=("spender", "proxy", "facilitator")):
    out = []
    for k, v in d.items():
        if isinstance(v, str) and ADDR_RE.match(v) and any(w in k.lower() for w in words):
            out.append(v)
    return out


def _permit2_addr(d):
    for k, v in d.items():
        if isinstance(v, str) and ADDR_RE.match(v) and "permit2" in k.lower() and not any(
                w in k.lower() for w in ("spender", "proxy", "facilitator")):
            return v
    return None


def _find_networks(obj, depth=0):
    if depth > 4:
        return None
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj) and any(isinstance(x.get("tokens"), list) for x in obj):
            return obj
        return None
    if isinstance(obj, dict):
        if isinstance(obj.get("tokens"), list):
            return [obj]
        for k in ("networks", "chains", "data", "config", "result", "supported_networks", "chain_config"):
            if k in obj:
                r = _find_networks(obj[k], depth + 1)
                if r:
                    return r
    return None


def parse_chain_config(cfg):
    """Return (rows: [TokenRow], rejects: [str]). Tolerant to the exact wrapping; strict about each value."""
    nets = _find_networks(cfg) or []
    rows, rejects = [], []
    for net in nets[:50]:
        network = net.get("network") or net.get("network_name") or net.get("name")
        chain_id = _chain_int(net.get("chain_id", net.get("chainId")))
        for t in (net.get("tokens") or [])[:50]:
            if not isinstance(t, dict):
                continue
            coin = t.get("coin") or t.get("symbol") or t.get("name")
            addr = t.get("address") or t.get("contract_address") or t.get("contractAddress")
            dec = t.get("decimals")
            proto = (t.get("payment_protocol") or t.get("protocol") or "")
            proto = proto.lower() if isinstance(proto, str) else ""
            cid = _chain_int(t.get("chain_id", t.get("chainId"))) or chain_id
            tnet = t.get("network") or network
            e = t.get("eip712") if isinstance(t.get("eip712"), dict) else {}
            name = e.get("name", t.get("eip712_name", t.get("domain_name")))
            ver = e.get("version", t.get("eip712_version", t.get("domain_version")))
            why = None
            if not isinstance(tnet, str) or not tnet or len(tnet) > 64:
                why = "network"
            elif cid is None:
                why = "chain_id"
            elif not isinstance(coin, str) or not coin or len(coin) > 16:
                why = "coin"
            elif not isinstance(addr, str) or not ADDR_RE.match(addr):
                why = "address"
            elif isinstance(dec, bool) or not isinstance(dec, int) or not 0 <= dec <= 36:
                why = "decimals"
            elif proto not in ("eip3009", "permit2"):
                why = "payment_protocol"
            if why:
                rejects.append(f"{_snip(tnet, 40)}/{_snip(coin, 16)}: bad {why}")
                continue
            rows.append(firewall.TokenRow(
                network=tnet, chain_id=cid, coin=coin, address=addr, decimals=dec, protocol=proto,
                eip712_name=name if isinstance(name, str) else None,
                eip712_version=ver if isinstance(ver, str) else None,
                permit2_address=_permit2_addr(t) or _permit2_addr(net),
                spenders=tuple(_extra_addrs(t) + _extra_addrs(net)), raw=t))
    return rows, rejects


def select_token(rows, network=None, coin=None, protocol=None):
    """Pick one testnet stablecoin row. Explicit filters first; otherwise prefer eip3009, HashKey testnet, USDC."""
    cand = [r for r in rows if r.chain_id in config.TESTNET_CHAIN_IDS and r.coin.upper() in config.USD_STABLECOINS]
    if network:
        cand = [r for r in cand if r.network == network or str(r.chain_id) == str(network)]
    if coin:
        cand = [r for r in cand if r.coin.upper() == coin.upper()]
    if protocol:
        cand = [r for r in cand if r.protocol == protocol]
    if not cand:
        return None
    cand.sort(key=lambda r: (r.protocol != "eip3009", r.chain_id != 133, r.coin.upper() != "USDC", r.chain_id))
    return cand[0]


# ---------------------------------------------------------------- receipts
class Receipts:
    def __init__(self, path, mode):
        self.path = path
        self.mode = mode

    def log(self, step, **fields):
        row = {"ts": utc_iso(), "mode": self.mode, "step": step}
        for k, v in fields.items():
            if v is None:
                continue
            row[k] = v if isinstance(v, (bool, int, float)) else _snip(v if isinstance(v, str) else
                                                                       json.dumps(v, default=str), 600)
        line = json.dumps(row, separators=(",", ":")) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return row

    def rows(self):
        try:
            with open(self.path) as f:
                return [json.loads(x) for x in f if x.strip()]
        except FileNotFoundError:
            return []

    def committed_usd(self):
        """USD of payments that reached submit (or an uncertain submit) in this receipts file."""
        tot = Decimal(0)
        for r in self.rows():
            if r.get("step") in ("submit_payment", "submit_uncertain", "submit_error") and r.get("mode") == self.mode:
                try:
                    tot += Decimal(str(r.get("amount_usd", "0")))
                except Exception:
                    pass
        return tot


# ---------------------------------------------------------------- flow
@dataclass
class Ctx:
    hsp: Hsp
    account: object                     # eth_account LocalAccount
    pay_to: str
    receipts: Receipts
    approve: Callable[[str, dict], bool]
    balance_fn: Optional[Callable] = None     # (chain_id, token, owner) -> int; None = skip (mock/explicit)
    allowance_fn: Optional[Callable] = None   # (chain_id, token, owner, spender) -> int
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    poll_interval: float = config.POLL_INTERVAL_S
    pending_report_s: float = config.PENDING_REPORT_S
    max_wait: float = config.MAX_WAIT_S
    out: Callable[[str], None] = print
    extra_permit2_spenders: tuple = ()


def container_with(obj, key, depth=0):
    """The (sub)dict that holds `key`, searching nested dicts breadth-first up to depth 3; else the object itself.
    Lets the flow cope with servers that wrap results (e.g. {"data": {...}}) without guessing field names."""
    level = [obj]
    for _ in range(4):
        nxt = []
        for o in level:
            if isinstance(o, dict):
                if key in o:
                    return o
                nxt.extend(v for v in o.values() if isinstance(v, dict))
        level = nxt
    return obj


def _find_key(obj, key, depth=0):
    if depth > 4:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key, depth + 1)
            if r is not None:
                return r
    return None


def build_cart(token, pay_to, amount_usd, now, label="HSK Meter metered API call (HSP QA)"):
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    cart_id = f"HSKM-QA-{stamp}-{secrets.token_hex(3)}"
    pr_id = f"HSKM-PR-{stamp}-{secrets.token_hex(3)}"
    value = format(Decimal(amount_usd).quantize(Decimal("0.01")), "f")
    contents = {
        "id": cart_id,
        "user_cart_confirmation_required": False,
        "payment_request": {
            "method_data": [{"supported_methods": "https://www.x402.org/",
                             "data": {"x402Version": 2, "network": token.network, "chain_id": token.chain_id,
                                      "contract_address": token.address, "pay_to": pay_to, "coin": token.coin}}],
            "details": {"id": pr_id,
                        "display_items": [{"label": label, "amount": {"currency": "USD", "value": value}}],
                        "total": {"label": "Total", "amount": {"currency": "USD", "value": value}}},
        },
        "cart_expiry": utc_iso(now + 3600),
        "merchant_name": config.MERCHANT_NAME,
    }
    return {"contents": contents}, cart_id, pr_id


def _rfc(s):
    """RFC3339 string -> epoch seconds; also accepts epoch seconds / milliseconds (int or digit string)."""
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)) or (isinstance(s, str) and s.isdigit() and len(s) <= 16):
        n = int(s)
        return n // 1000 if n > 10 ** 12 else n
    if not isinstance(s, str) or len(s) > 40 or not re.match(r"^\d{4}-\d{2}-\d{2}T", s):
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _status_of(q):
    q = container_with(q, "status") if "payment" not in q else q
    pay = q.get("payment") if isinstance(q.get("payment"), dict) else {}
    st = q.get("status") or pay.get("status")
    return st if isinstance(st, str) else None, pay


def sign_typed(account, typed):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    msg = encode_typed_data(full_message=typed)
    sig = Account.sign_message(msg, account.key).signature
    sig_hex = "0x" + bytes(sig).hex()
    if len(sig) != 65 or Account.recover_message(msg, signature=sig) != account.address:
        raise FlowStop("self_check_failed")
    return sig_hex


def run_payment(ctx, token, amount_usd, *, dry=False, wait_finalized=False):
    amount_usd = Decimal(str(amount_usd))
    if not (config.MIN_PAYMENT_USD <= amount_usd <= config.PER_PAYMENT_CAP_USD):
        raise FlowStop("cap", detail=f"amount {amount_usd} outside [{config.MIN_PAYMENT_USD}, "
                                     f"{config.PER_PAYMENT_CAP_USD}]")
    if not dry and ctx.receipts.committed_usd() + amount_usd > config.TOTAL_CAP_USD:
        raise FlowStop("cap", detail=f"lifetime total would exceed {config.TOTAL_CAP_USD} USD")
    payer = ctx.account.address
    R = ctx.receipts
    run_id = uuid.uuid4().hex[:12]
    base = {"run_id": run_id, "merchant_name": config.MERCHANT_NAME, "amount_usd": str(amount_usd)}
    now = ctx.clock()
    cart, cart_id, our_pr = build_cart(token, ctx.pay_to, amount_usd, now)
    R.log("start", **base, network=token.network, chain_id=token.chain_id, token=token.address, coin=token.coin,
          protocol=token.protocol, payer=payer, pay_to=ctx.pay_to, cart_id=cart_id, dry=dry)

    order = container_with(ctx.hsp.call("create_order", need=("merchant_name", "pay_to", "cart_mandate"),
                                        merchant_name=config.MERCHANT_NAME, pay_to=ctx.pay_to, cart_mandate=cart),
                           "flow_id")
    flow_id, pr_id = order.get("flow_id"), order.get("payment_request_id")
    if not (isinstance(flow_id, str) and ID_RE.match(flow_id)) or not (isinstance(pr_id, str) and ID_RE.match(pr_id)):
        R.log("create_order_bad", **base, response=order)
        raise FlowStop("create_order returned no usable flow_id/payment_request_id")
    purl = order.get("payment_url")
    R.log("create_order", **base, flow_id=flow_id, payment_request_id=pr_id,
          payment_url=purl.split("?")[0] if isinstance(purl, str) else None)
    ids = {"flow_id": flow_id, "payment_request_id": pr_id}

    got = ctx.hsp.call("get_order", need=("flow_id",), flow_id=flow_id)
    for key, ours in (("pay_to", ctx.pay_to), ("merchant_name", config.MERCHANT_NAME)):
        v = _find_key(got, key)
        if isinstance(v, str) and v and (v.lower() != ours.lower()):
            R.log("get_order_mismatch", **base, **ids, field=key, got=v)
            raise FlowStop(f"get_order {key} differs from what we created", **ids)
    gfa = _find_key(got, "gas_fee_advanced")
    gfa = gfa if isinstance(gfa, bool) else None
    R.log("get_order", **base, **ids, status=_find_key(got, "status"), gas_fee_advanced=gfa)

    try:
        fee = ctx.hsp.call("estimate_fee", flow_id=flow_id, payer=payer, protocol=token.protocol)
        R.log("estimate_fee", **base, **ids, gas_fee=_find_key(fee, "gas_fee"),
              gas_fee_advanced=_find_key(fee, "gas_fee_advanced"))
    except (HspError, SchemaMismatch, McpError) as e:
        R.log("estimate_fee_error", **base, **ids, error=str(e))

    submitted = False
    sub_status = None
    for attempt in range(1, config.MAX_PREPARE_ATTEMPTS + 1):
        prep = container_with(ctx.hsp.call("prepare_payment", need=("flow_id", "payer", "protocol"),
                                           flow_id=flow_id, payer=payer, protocol=token.protocol), "eip712")
        qexp = _rfc(prep.get("quote_expires_at")) if prep.get("quote_expires_at") else None
        R.log("prepare_payment", **base, **ids, attempt=attempt, token_total_raw=str(prep.get("token_total_raw")),
              quote_expires_at=prep.get("quote_expires_at"), cart_hash=prep.get("cart_hash"),
              eip712_hash=_h(prep.get("eip712")))
        if qexp is not None and qexp - ctx.clock() < config.QUOTE_REPREPARE_MARGIN_S:
            R.log("quote_expired_before_sign", **base, **ids, attempt=attempt)
            continue
        exp = firewall.Expectation(payer=payer, pay_to=ctx.pay_to, token=token, order_usd=amount_usd,
                                   gas_fee_advanced=gfa, payment_request_id=pr_id,
                                   extra_permit2_spenders=ctx.extra_permit2_spenders)
        try:
            verdict = firewall.check(prep, exp, now=ctx.clock(), quote_expires_at=qexp)
        except firewall.Refused as e:
            R.log("firewall_refused", **base, **ids, rule=e.rule, detail=e.detail)
            raise FlowStop("firewall_refused", rule=e.rule, detail=e.detail, **ids) from None
        s = verdict.summary
        R.log("firewall_ok", **base, **ids, recipient=s["recipient"], token_total_raw=s["token_total_raw"],
              valid_until=s["valid_until"], bound_raw=s["bound_raw"])
        need_raw = verdict.token_total_raw
        funding = {}
        if ctx.balance_fn is None and not dry:
            raise FlowStop("no_balance_check", detail="refusing to sign without a balance check", **ids)
        if ctx.balance_fn is not None:
            try:
                bal = ctx.balance_fn(token.chain_id, token.address, payer)
            except Exception as e:      # chain.ChainError or transport
                R.log("balance_error", **base, **ids, error=str(e))
                if not dry:
                    raise FlowStop("balance_check_failed", detail=str(e), **ids) from None
                bal = None
            if bal is not None:
                R.log("balance", **base, **ids, balance_raw=str(bal), need_raw=str(need_raw))
                funding = {"balance_raw": str(bal), "need_raw": str(need_raw), "funded": bal >= need_raw}
                if bal < need_raw and not dry:
                    raise FlowStop("funding_needed", balance_raw=bal, need_raw=need_raw, token=token.address,
                                   chain_id=token.chain_id, coin=token.coin, payer=payer, **ids)
            if token.protocol == "permit2" and ctx.allowance_fn is not None:
                p2 = token.permit2_address or config.CANONICAL_PERMIT2
                try:
                    al = ctx.allowance_fn(token.chain_id, token.address, payer, p2)
                except Exception as e:
                    if not dry:
                        raise FlowStop("allowance_check_failed", detail=str(e), **ids) from None
                    al = None
                R.log("permit2_allowance", **base, **ids, allowance_raw=str(al), spender=p2)
                funding["permit2_allowance_raw"] = str(al)
                if al is not None and al < need_raw and not dry:
                    raise FlowStop("permit2_approval_needed", allowance_raw=al, need_raw=need_raw, spender=p2, **ids)
        if dry:
            R.log("dry_stop", **base, **ids, **funding)
            return {"status": "dry", **ids, "summary": s, "funding": funding}
        if not ctx.approve("sign", {**s, **ids}):
            R.log("not_approved", **base, **ids, stage="sign")
            raise FlowStop("not_approved_sign", **ids)
        sig = sign_typed(ctx.account, verdict.typed_data)
        R.log("signed", **base, **ids, signature_hash=_h(sig))
        try:
            fin = ctx.hsp.call("finalize_payment",
                               need=("signature", "payment_payload", "payment_mandate_contents", "cart_hash"),
                               signature=sig, payment_payload=prep.get("payment_payload"),
                               payment_mandate_contents=prep.get("payment_mandate_contents"),
                               cart_hash=prep.get("cart_hash"))
        except HspError as e:
            R.log("finalize_error", **base, **ids, code=e.code, error=e.message, attempt=attempt)
            if is_quote_error(e):
                continue
            raise
        except McpTimeout as e:
            R.log("finalize_timeout", **base, **ids, error=str(e), attempt=attempt)
            continue                     # nothing was submitted; a fresh quote is safe
        pmj = container_with(fin, "payment_mandate_json").get("payment_mandate_json")
        if not pmj:
            R.log("finalize_bad", **base, **ids, keys=sorted(fin)[:20])
            raise FlowStop("finalize_payment returned no payment_mandate_json", **ids)
        R.log("finalize_payment", **base, **ids, payment_mandate_hash=_h(pmj))
        if not ctx.approve("submit", {**s, **ids}):
            R.log("not_approved", **base, **ids, stage="submit")
            raise FlowStop("not_approved_submit", **ids)
        try:
            sub = ctx.hsp.call("submit_payment", need=("payment_mandate_json",), payment_mandate_json=pmj,
                               total_timeout=config.SUBMIT_TIMEOUT_S)
        except HspError as e:
            R.log("submit_error", **base, **ids, code=e.code, error=e.message, attempt=attempt)
            if is_quote_error(e):
                continue
            # A definite gateway error: query once (never resubmit) and report what the gateway says.
            r = poll(ctx, ids, base, wait_finalized=False, max_wait=0)
            r.update(submit_status=f"error {e.code}", submit_error=e.message, token_total_raw=s["token_total_raw"])
            return r
        except McpError as e:
            R.log("submit_uncertain", **base, **ids, error=str(e))
            submitted = True
            sub_status = "uncertain"
            break
        sub_status, _ = _status_of(sub)
        R.log("submit_payment", **base, **ids, status=sub_status,
              tx_signature=_find_key(sub, "tx_signature") or None)
        submitted = True
        break
    if not submitted:
        R.log("gave_up", **base, **ids, reason="quote kept expiring")
        raise FlowStop("quote_retries_exhausted", **ids)
    result = poll(ctx, ids, base, wait_finalized=wait_finalized)
    result["submit_status"] = sub_status
    result["token_total_raw"] = s["token_total_raw"]
    return result


def query_once(ctx, ids):
    return ctx.hsp.call("query_payment", need=("expected_merchant_name",), flow_id=ids.get("flow_id"),
                        payment_request_id=ids.get("payment_request_id"),
                        expected_merchant_name=config.MERCHANT_NAME)


def poll(ctx, ids, base=None, *, wait_finalized=False, max_wait=None):
    base = base or {"merchant_name": config.MERCHANT_NAME}
    max_wait = ctx.max_wait if max_wait is None else max_wait
    R = ctx.receipts
    start = ctx.clock()
    last_status, reported_pending, errors = None, False, 0
    last = {}
    while True:
        try:
            q = query_once(ctx, ids)
            errors = 0
        except (HspError, McpError) as e:
            errors += 1
            R.log("query_error", **base, **ids, error=str(e), consecutive=errors)
            ctx.out(f"query_payment failed: {e}")
            if errors >= 5 or max_wait == 0:
                return {"status": "query_failing", **ids, **last}
            ctx.sleep(ctx.poll_interval)
            continue
        status, pay = _status_of(q)
        att = _find_key(q, "attribution_verified")
        last = {"payment_status": status, "attribution_verified": att,
                "tx_signature": pay.get("tx_signature") or q.get("tx_signature") or "",
                "network": pay.get("network"), "to_pay_address": pay.get("to_pay_address"),
                "payer_address": pay.get("payer_address"), "status_reason": pay.get("status_reason")}
        if status != last_status:
            R.log("query_payment", **base, **ids, status=status, attribution_verified=att,
                  tx_signature=last["tx_signature"] or None, status_reason=last["status_reason"])
            ctx.out(f"status: {status} (attribution_verified={att})")
            last_status = status
        if status == "payment-finalized" or status == "payment-failed" or (
                status == "payment-safe" and not wait_finalized):
            return {"status": status, **ids, **last}
        if status not in config.POLLING_STATUSES and status != "payment-safe":
            ctx.out(f"unrecognised status {_snip(status, 60)!r}; still polling the same payment")
        elapsed = ctx.clock() - start
        if elapsed >= ctx.pending_report_s and not reported_pending:
            ctx.out(f"Pending confirmation. flow_id={ids['flow_id']} payment_request_id={ids['payment_request_id']}")
            R.log("pending_confirmation", **base, **ids, status=status)
            reported_pending = True
        if elapsed >= max_wait:
            R.log("poll_stopped", **base, **ids, status=status, waited_s=int(elapsed))
            return {"status": status or "unknown", "stopped_polling": True, **ids, **last}
        ctx.sleep(ctx.poll_interval)


def report(result, token=None):
    tx = result.get("tx_signature") or "unavailable"
    lines = ["HSP Agentic Payment — QA Result",
             f"Payment status: {result.get('status')}",
             f"Hackathon team: {config.MERCHANT_NAME}",
             f"Team attribution verified: {result.get('attribution_verified', 'unavailable')}",
             f"Network: {result.get('network') or (token.network if token else 'unavailable')}",
             f"Raw authorization amount (raw units): {result.get('token_total_raw', 'unavailable')}",
             f"Recipient address: {result.get('to_pay_address') or 'unavailable'}",
             f"Payer address: {result.get('payer_address') or 'unavailable'}",
             f"Transaction hash: {tx}",
             f"flow_id: {result.get('flow_id')}  payment_request_id: {result.get('payment_request_id')}"]
    st = result.get("status")
    if st == "payment-safe":
        lines.append("The transaction is on-chain and has received half of the required block confirmations. "
                     "It is not yet finalized.")
    elif st == "payment-finalized":
        lines.append("The payment has reached final confirmation.")
    elif st == "payment-failed":
        lines.append(f"Failure reason: {result.get('status_reason') or 'unavailable'}")
    elif result.get("stopped_polling"):
        lines.append("Pending confirmation (resume with: pay.py query --flow-id ... --payment-request-id ...)")
    return "\n".join(lines)
