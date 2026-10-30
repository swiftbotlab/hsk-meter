#!/usr/bin/env python3
"""HSP QA payment client for HSK Meter (official HSP Agentic Payment MCP server).

  pay.py wallets [--create]                 show (or create) the dedicated QA payer + merchant addresses
  pay.py health                             one rate-limited GET /health on the live QA server
  pay.py discover [--mock]                  initialize, tools/list (8 tools required), get_chain_config
  pay.py pay [--mock] [--dry] [--count N] [--amount USD] [--network N] [--coin C] [--yes] [--wait-finalized]
  pay.py query --flow-id F --payment-request-id P [--wait-finalized]   resume polling a payment (live)
  pay.py balance --chain-id 133 --token 0x...                          payer token balance via pinned RPC
  pay.py probe [--auto-pay]                 timer job: rate-limited health; when allowlisted: discover + dry run
                                            (+ one real payment with --auto-pay once the payer is funded)

Live mode talks only to https://mcp-qa.hashkeymerchant.com/. --mock starts a local mock server in-process and uses
an ephemeral payer key. Keys are never printed. Receipts: receipts.jsonl (live), receipts.mock.jsonl (mock).
"""
import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import chain  # noqa: E402
import config  # noqa: E402
import flow  # noqa: E402
import wallets  # noqa: E402
from mcp_client import McpBlocked, McpClient, McpError, health  # noqa: E402


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _deadline_ts():
    return datetime.fromisoformat(config.PROBE_DEADLINE_UTC.replace("Z", "+00:00")).timestamp()


# ------------------------------------------------------------------ live-contact discipline
class State:
    def __init__(self):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        self.path = os.path.join(config.STATE_DIR, "probe.json")
        self.lockf = open(os.path.join(config.STATE_DIR, "probe.lock"), "a")

    def __enter__(self):
        fcntl.flock(self.lockf, fcntl.LOCK_EX)
        try:
            with open(self.path) as f:
                self.d = json.load(f)
        except (FileNotFoundError, ValueError):
            self.d = {}
        return self

    def __exit__(self, *a):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f, indent=1)
        os.replace(tmp, self.path)
        fcntl.flock(self.lockf, fcntl.LOCK_UN)
        self.lockf.close()


def plog(msg):
    line = f"{_now_iso()} {msg}"
    print(line)
    with open(config.PROBE_LOG, "a") as f:
        f.write(line + "\n")


def guard_live():
    """Refuse a live contact if the endpoint answered 403 less than PROBE_MIN_INTERVAL_S ago."""
    with State() as s:
        b = s.d.get("last_blocked_ts")
        if b and time.time() - b < config.PROBE_MIN_INTERVAL_S:
            mins = int((config.PROBE_MIN_INTERVAL_S - (time.time() - b)) / 60) + 1
            raise SystemExit(f"endpoint answered 403 at {s.d.get('last_blocked_iso')}; not contacting it again for "
                             f"~{mins} min (no hammering a blocked endpoint)")


def record_blocked(where):
    with State() as s:
        s.d["last_blocked_ts"] = time.time()
        s.d["last_blocked_iso"] = _now_iso()
        s.d["last_blocked_where"] = where


# ------------------------------------------------------------------ setup helpers
class Ephemeral:
    """In-memory key for --mock runs (never written anywhere)."""

    def __new__(cls):
        from eth_account import Account
        return Account.from_key(os.urandom(32))


def make_hsp(args, mock_state=None):
    if args.mock:
        import mock_server
        srv, st, url = mock_server.make_server(0, mock_state)
        mcp = McpClient(url, allow_loopback=True)
        return flow.Hsp(mcp), st, srv
    guard_live()
    return flow.Hsp(McpClient(config.MCP_URL)), None, None


def discover(hsp, save=False):
    tools = hsp.discover()
    cfg = hsp.call("get_chain_config")
    if save:
        with open(config.LIVE_TOOLS_FILE, "w") as f:
            json.dump({"fetched": _now_iso(), "server_info": hsp.mcp.server_info, "tools": tools}, f, indent=1)
        with open(config.LIVE_CHAIN_CONFIG_FILE, "w") as f:
            json.dump({"fetched": _now_iso(), "config": cfg}, f, indent=1)
    rows, rejects = flow.parse_chain_config(cfg)
    return tools, cfg, rows, rejects


def approver(args):
    def approve(stage, s):
        print(f"\n--- approve {stage}? recipient={s.get('recipient')} payer={s.get('payer')} "
              f"token_total_raw={s.get('token_total_raw')} (raw units; ~{s.get('amount_human')} {s.get('coin')}) "
              f"network={s.get('network')} chain={s.get('chain_id')} token={s.get('token')} "
              f"valid_until={s.get('valid_until')} flow_id={s.get('flow_id')}")
        if args.yes:
            print("    approved by --yes")
            return True
        if sys.stdin.isatty():
            return input("    type 'yes' to approve: ").strip() == "yes"
        print("    not approved (no --yes and no terminal)")
        return False
    return approve


def _amount(s):
    try:
        a = Decimal(s)
    except InvalidOperation:
        raise SystemExit(f"bad amount {s!r}") from None
    if not (config.MIN_PAYMENT_USD <= a <= config.PER_PAYMENT_CAP_USD) or a != a.quantize(Decimal("0.01")):
        raise SystemExit(f"amount must be {config.MIN_PAYMENT_USD}..{config.PER_PAYMENT_CAP_USD} USD in cents")
    return a


# ------------------------------------------------------------------ commands
def cmd_wallets(args):
    if args.create:
        for name, label in ((config.PAYER_ENV, "payer"), (config.MERCHANT_ENV, "merchant / pay_to")):
            addr, created = wallets.create(name, label)
            print(f"{label}: {addr} ({'created' if created else 'exists'}) -> secrets/{name} (mode 600)")
    else:
        print(f"payer:    {wallets.payer_address()}")
        print(f"merchant: {wallets.merchant_address()}")
    return 0


def cmd_health(args):
    if time.time() > _deadline_ts() and not args.force_after_deadline:
        print(f"past the probe deadline {config.PROBE_DEADLINE_UTC}; not probing")
        return 0
    with State() as s:
        last = s.d.get("last_probe_ts", 0)
        if time.time() - last < config.PROBE_MIN_INTERVAL_S:
            print(f"last probe {s.d.get('last_probe_iso')} is < 30 min ago; not probing (result was "
                  f"{s.d.get('last_probe_status')})")
            return 5
        s.d["last_probe_ts"], s.d["last_probe_iso"] = time.time(), _now_iso()
        try:
            st, body = health()
        except Exception as e:
            st, body = None, f"{type(e).__name__}"
        s.d["last_probe_status"] = st
        s.d["last_probe_body"] = body[:120]
        if st == 403:
            s.d["last_blocked_ts"], s.d["last_blocked_iso"], s.d["last_blocked_where"] = (
                time.time(), _now_iso(), "health")
        ok = st == 200 and '"ok"' in body.replace(" ", "")
        if ok and not s.d.get("allowlisted_iso"):
            s.d["allowlisted_iso"] = _now_iso()
    plog(f"health: HTTP {st} {body[:120]!r} -> {'ALLOWLISTED' if ok else 'not reachable'}")
    return 0 if ok else 3


def cmd_discover(args):
    try:
        hsp, _, srv = make_hsp(args)
        tools, cfg, rows, rejects = discover(hsp, save=not args.mock)
    except McpBlocked as e:
        record_blocked("discover")
        print(f"BLOCKED (IP not allowlisted?): {e}")
        return 3
    except flow.MissingTools as e:
        print(f"STOP: {e}")
        return 2
    print(f"server: {hsp.mcp.server_info}  protocol {hsp.mcp.protocol_version}  session "
          f"{'yes' if hsp.mcp.session_id else 'no'}")
    print("tools:", ", ".join(sorted(t["name"] for t in tools)))
    for t in tools:
        props = sorted(((t.get("inputSchema") or {}).get("properties") or {}).keys())
        print(f"  {t['name']}: {props}")
    print(f"chain config rows ({len(rows)} usable, {len(rejects)} rejected):")
    for r in rows:
        mark = "testnet" if r.chain_id in config.TESTNET_CHAIN_IDS else "MAINNET (refused)"
        print(f"  {r.network} chain {r.chain_id} {r.coin} {r.address} dec {r.decimals} {r.protocol} [{mark}]"
              + (f" eip712=({r.eip712_name},{r.eip712_version})" if r.eip712_name else "")
              + (f" permit2={r.permit2_address}" if r.permit2_address else "")
              + (f" spenders={list(r.spenders)}" if r.spenders else ""))
    for x in rejects:
        print(f"  rejected: {x}")
    pick = flow.select_token(rows, args.network, args.coin)
    print(f"selected: {pick.network} {pick.coin} {pick.address} {pick.protocol}" if pick
          else "selected: none (no testnet USD stablecoin row)")
    if not args.mock:
        print(f"saved {config.LIVE_TOOLS_FILE} and {config.LIVE_CHAIN_CONFIG_FILE}")
    return 0


def _pay(args, hsp, rows, receipts, account, pay_to, balance_fn, allowance_fn, ctx_overrides=None):
    token = flow.select_token(rows, args.network, args.coin)
    if not token:
        print("STOP: get_chain_config offers no testnet USD stablecoin matching the filters")
        return 2
    amount = _amount(args.amount)
    if amount * args.count > config.TOTAL_CAP_USD:
        print(f"STOP: {args.count} x {amount} exceeds the {config.TOTAL_CAP_USD} USD total cap")
        return 2
    ctx = flow.Ctx(hsp=hsp, account=account, pay_to=pay_to, receipts=receipts, approve=approver(args),
                   balance_fn=balance_fn, allowance_fn=allowance_fn,
                   extra_permit2_spenders=tuple(args.permit2_spender or ()))
    for k, v in (ctx_overrides or {}).items():
        setattr(ctx, k, v)
    print(f"token: {token.network} (chain {token.chain_id}) {token.coin} {token.address} via {token.protocol}")
    print(f"payer {account.address} -> pay_to {pay_to}; merchant_name {config.MERCHANT_NAME!r}")
    rc = 0
    for i in range(args.count):
        try:
            res = flow.run_payment(ctx, token, amount, dry=args.dry, wait_finalized=args.wait_finalized)
        except flow.FlowStop as e:
            print(f"STOP [{e.reason}]: {json.dumps(e.info, default=str)}")
            return 2
        except (flow.HspError, flow.SchemaMismatch, McpError) as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            return 4
        if res["status"] == "dry":
            s = res["summary"]
            print(f"DRY RUN stopped before signing. flow_id={res['flow_id']} payment_request_id="
                  f"{res['payment_request_id']}\n  firewall OK: recipient={s['recipient']} payer={s['payer']} "
                  f"token_total_raw={s['token_total_raw']} (~{s['amount_human']} {s['coin']}) valid_until="
                  f"{s['valid_until']}\n  funding: {json.dumps(res.get('funding'))}")
        else:
            print(flow.report(res, token))
            if res["status"] not in ("payment-safe", "payment-finalized"):
                rc = 2
    return rc


def cmd_pay(args):
    if args.mock:
        account = Ephemeral()
        pay_to = Ephemeral().address
        receipts = flow.Receipts(args.receipts or config.RECEIPTS_MOCK, "mock")
        hsp, st, srv = make_hsp(args)
        _, _, rows, _ = discover(hsp)
        return _pay(args, hsp, rows, receipts, account, pay_to, lambda c, t, o: 10 ** 12,
                    lambda c, t, o, s: 10 ** 12, {"poll_interval": 0.05, "pending_report_s": 5, "max_wait": 20})
    account = wallets.load_payer()
    pay_to = wallets.merchant_address()
    receipts = flow.Receipts(args.receipts or config.RECEIPTS_LIVE, "live")
    try:
        hsp, _, _ = make_hsp(args)
        _, _, rows, rejects = discover(hsp, save=True)
    except McpBlocked as e:
        record_blocked("pay")
        print(f"BLOCKED (IP not allowlisted?): {e}")
        return 3
    except flow.MissingTools as e:
        print(f"STOP: {e}")
        return 2
    return _pay(args, hsp, rows, receipts, account, pay_to, chain.balance_of, chain.allowance)


def cmd_query(args):
    receipts = flow.Receipts(args.receipts or config.RECEIPTS_LIVE, "live")
    try:
        hsp, _, _ = make_hsp(args)
        hsp.discover()
    except McpBlocked as e:
        record_blocked("query")
        print(f"BLOCKED: {e}")
        return 3
    ctx = flow.Ctx(hsp=hsp, account=None, pay_to=None, receipts=receipts, approve=lambda *a: False)
    res = flow.poll(ctx, {"flow_id": args.flow_id, "payment_request_id": args.payment_request_id},
                    wait_finalized=args.wait_finalized)
    print(flow.report(res))
    return 0


def cmd_balance(args):
    owner = args.owner or wallets.payer_address()
    try:
        bal = chain.balance_of(args.chain_id, args.token, owner)
    except chain.ChainError as e:
        print(f"balance check failed: {e}")
        return 4
    print(f"{owner} holds {bal} raw units of {args.token} on chain {args.chain_id}")
    return 0


def cmd_probe(args):
    """Timer job. Health first (rate-limited); after allowlisting: discovery + one dry run once; with --auto-pay,
    one real payment once the payer holds enough of the selected token (balance read first, no order created)."""
    if time.time() > _deadline_ts():
        print(f"past the probe deadline {config.PROBE_DEADLINE_UTC}; nothing to do")
        return 0
    with State() as s:
        allowlisted = bool(s.d.get("allowlisted_iso"))
        dry_done, auto_paid = s.d.get("dry_done_iso"), s.d.get("auto_paid_iso")
    if not allowlisted:
        rc = cmd_health(argparse.Namespace(force_after_deadline=False))
        with State() as s:
            allowlisted = bool(s.d.get("allowlisted_iso"))
        if rc != 0 or not allowlisted:      # 3 = blocked, 5 = skipped (rate limit): never go further
            return 0
    if auto_paid:
        plog("probe: real payment already made; nothing to do")
        return 0
    ns = argparse.Namespace(mock=False, network=None, coin=None, amount="0.01", count=1, dry=True, yes=False,
                            wait_finalized=True, receipts=None, permit2_spender=None)
    try:
        hsp, _, _ = make_hsp(ns)
        tools, cfg, rows, rejects = discover(hsp, save=True)
    except SystemExit as e:
        plog(f"probe: {e}")
        return 0
    except McpBlocked as e:
        record_blocked("probe-discover")
        plog(f"probe: discover BLOCKED: {e}")
        return 0
    except (McpError, flow.MissingTools, flow.HspError) as e:
        plog(f"probe: discover failed: {e}")
        return 0
    token = flow.select_token(rows)
    plog(f"probe: discovered {len(tools)} tools; token "
         + (f"{token.network}/{token.chain_id} {token.coin} {token.address} {token.protocol}" if token else "NONE"))
    if not token:
        return 0
    account = wallets.load_payer()
    if not dry_done:
        rc = _pay(ns, hsp, rows, flow.Receipts(config.RECEIPTS_LIVE, "live"), account, wallets.merchant_address(),
                  chain.balance_of, chain.allowance)   # dry: records the real balance, never signs
        plog(f"probe: dry run rc={rc}")
        if rc == 0:
            with State() as s:
                s.d["dry_done_iso"] = _now_iso()
    if not args.auto_pay:
        return 0
    try:
        bal = chain.balance_of(token.chain_id, token.address, account.address)
    except chain.ChainError as e:
        plog(f"probe: balance check failed: {e}")
        return 0
    need = 2 * 10 ** token.decimals // 100   # 0.02 token covers a 0.01 USD payment plus any fee
    plog(f"probe: payer balance {bal} raw (want >= {need}) of {token.coin} {token.address}")
    if bal < need:
        return 0
    ns.dry, ns.yes = False, True
    rc = _pay(ns, hsp, rows, flow.Receipts(config.RECEIPTS_LIVE, "live"), account, wallets.merchant_address(),
              chain.balance_of, chain.allowance)
    plog(f"probe: REAL payment rc={rc} (see receipts.jsonl)")
    with State() as s:
        s.d["auto_paid_iso"] = _now_iso()
        s.d["auto_paid_rc"] = rc
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wallets")
    w.add_argument("--create", action="store_true")
    h = sub.add_parser("health")
    h.add_argument("--force-after-deadline", action="store_true")
    for name in ("discover", "pay"):
        p = sub.add_parser(name)
        p.add_argument("--mock", action="store_true")
        p.add_argument("--network")
        p.add_argument("--coin")
        if name == "pay":
            p.add_argument("--dry", action="store_true", help="stop before signing")
            p.add_argument("--count", type=int, default=1)
            p.add_argument("--amount", default=str(config.DEFAULT_AMOUNT_USD))
            p.add_argument("--yes", action="store_true", help="approve signing and submission non-interactively")
            p.add_argument("--wait-finalized", action="store_true", help="keep polling past payment-safe")
            p.add_argument("--receipts")
            p.add_argument("--permit2-spender", action="append",
                           help="extra Permit2 spender to trust (only after reviewing get_chain_config)")
    q = sub.add_parser("query")
    q.add_argument("--flow-id", required=True)
    q.add_argument("--payment-request-id", required=True)
    q.add_argument("--wait-finalized", action="store_true")
    q.add_argument("--receipts")
    q.set_defaults(mock=False)
    b = sub.add_parser("balance")
    b.add_argument("--chain-id", type=int, required=True)
    b.add_argument("--token", required=True)
    b.add_argument("--owner")
    pr = sub.add_parser("probe")
    pr.add_argument("--auto-pay", action="store_true")
    args = ap.parse_args(argv)
    if getattr(args, "count", 1) < 1 or getattr(args, "count", 1) > 20:
        raise SystemExit("--count must be 1..20")
    return {"wallets": cmd_wallets, "health": cmd_health, "discover": cmd_discover, "pay": cmd_pay,
            "query": cmd_query, "balance": cmd_balance, "probe": cmd_probe}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
