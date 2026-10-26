#!/usr/bin/env python3
"""Agent B: a second x402 v2 seller on HashKey Chain (chain 177) that demonstrates a real agent-to-agent (A2A)
payment chain by Swift Bot Labs.

Agent B sells ONE paid endpoint, a "HashKey address risk brief" (GET /v1/hsk/risk-brief?address=0x..., on-chain
endpoint id 3). To PRODUCE its answer, Agent B itself acts as a BUYER and pays Agent A (the existing HSK Meter
seller, server/seller.py) over x402 for the raw data: one call to Agent A's address-profile (endpoint 1). So one
end-user request to Agent B triggers a SECOND, nested x402 settlement from Agent B's own wallet to Agent A. This
yields distinct on-chain roles: the end-buyer pays Agent B's payee; Agent B's operator wallet pays Agent A's payee.

This module REUSES server/seller.py's Seller class, HTTP handler, settlement/refund logic and the EIP-3009/x402
machinery unchanged. Agent B only:
  * replaces the module globals S.RESOURCES / S.WORK so the seller advertises ONLY endpoint 3 (/v1/hsk/risk-brief)
    and its WORK function does the upstream purchase, and
  * pays Agent A through agent/buyer.py's buy() (never a raw tx), from Agent B's own buyer key.

If the upstream purchase cannot deliver (buyer refused before signing, or paid-but-not-delivered / refunded),
risk_brief raises chain.RpcError. seller.Seller.deliver() turns that into an on-chain refund of the END-buyer
(see deliver(): `except C.RpcError as err: ... return self.refund(..., status=502)`), so an end-buyer who cannot be
served gets their money back and Agent B keeps nothing.

Security notes:
  * The upstream profile body is UNTRUSTED data: risk_brief parses it defensively (type checks, bounded values,
    an allow-listed explorer host) and never interprets it as instructions.
  * Agent B's own seller keys (provider/adapter/token) load exactly like seller.py, from --key-dir (0600 files).
  * Agent B's buyer key loads from --b-buyer-key-file (exactly one NAME=0x... line, mode 0600, no symlink). The
    private key is never printed, logged or placed in a shell argument.
"""
import argparse
import json
import os
import stat
import sys
import threading
import urllib.parse
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))   # seller.py inserts its own dir on import; mirror that here
sys.path.insert(0, HERE)
import seller as S        # noqa: E402  the real HSK Meter seller (Seller, make_handler, Server, RESOURCES, WORK)
import protocol as P      # noqa: E402
import buyer as BUY       # noqa: E402  the real paying client (buy/quote/status, guard + on-chain verification)
from eth_account import Account  # noqa: E402

VERSION = "0.1.0"
RISK_PATH = "/v1/hsk/risk-brief"
RISK_ENDPOINT_ID = 3
UPSTREAM_PATH = "/v1/hsk/address-profile"      # Agent A's endpoint 1
RISK_RESOURCE = {
    RISK_PATH: {"endpointId": RISK_ENDPOINT_ID, "param": "address", "re": P.ADDR_RE,
                "desc": "HashKey Chain address risk brief: on-chain profile plus explainable heuristic risk signals, "
                        "composed by paying an upstream data provider over x402 (nested agent-to-agent payment)."},
}

# Upstream (Agent A) buyer configuration, read by risk_brief() at request time. Set by configure_upstream()/main().
_UPSTREAM = None


# --------------------------------------------------------------------------- the paid work: a composed risk brief
def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _compose(address, profile, out):
    """Build a deterministic, explainable heuristic risk brief from an (untrusted) upstream address profile and the
    verified upstream payment record `out` (buyer.buy()'s return dict). Higher riskScore = more caution warranted."""
    profile = profile if isinstance(profile, dict) else {}
    balances = profile.get("balances") if isinstance(profile.get("balances"), dict) else {}
    hsk, usdce = _num(balances.get("HSK")), _num(balances.get("USDC.e"))
    usdt, whsk = _num(balances.get("USDT")), _num(balances.get("WHSK"))
    stable = usdce + usdt
    nonce = profile.get("nonce")
    nonce = nonce if isinstance(nonce, int) and 0 <= nonce < 10**18 else None
    is_contract = profile.get("type") == "contract"
    code_size = profile.get("codeSize") if isinstance(profile.get("codeSize"), int) and profile.get("codeSize") >= 0 else None

    factors, score = [], 50

    def add(impact, factor, detail):
        nonlocal score
        score += impact
        factors.append({"factor": factor, "impact": impact, "detail": detail})

    if is_contract:
        add(+15, "is_contract", "Address holds bytecode (a contract, codeSize=%s). Verify the contract's source and "
                                "behaviour before sending it funds." % code_size)
    else:
        add(-5, "is_eoa", "Address is a plain externally-owned account (EOA), not a contract.")

    if nonce is None:
        add(+5, "activity_unknown", "Outbound transaction count could not be read.")
    elif nonce == 0:
        add(+20, "no_activity", "No outbound transactions (nonce 0): a brand-new or never-used address.")
    elif nonce < 10:
        add(+8, "low_activity", "Few outbound transactions (nonce %d): limited history to judge by." % nonce)
    elif nonce >= 100:
        add(-15, "established", "Long outbound-transaction history (nonce %d)." % nonce)
    else:
        add(-5, "some_activity", "Some outbound-transaction history (nonce %d)." % nonce)

    if hsk == 0 and stable == 0 and whsk == 0:
        add(+15, "empty_balance", "No HSK, USDC.e, USDT or WHSK held: a dormant, drained or throwaway address.")
    else:
        if hsk == 0:
            add(+5, "no_gas", "Holds no HSK for gas; cannot transact on its own without being funded first.")
        if stable > 0:
            add(-10, "holds_stablecoins", "Holds %.6f in USDC.e + USDT: a funded, economically active address." % stable)

    score = max(0, min(100, score))
    label = "low" if score < 34 else ("moderate" if score < 67 else "elevated")

    amount_units = out.get("amount")
    explorer = profile.get("explorer")
    if not (isinstance(explorer, str) and explorer.startswith("https://hsk.blockscout.com/")):
        explorer = None
    upstream_payment = {
        "provider": "HSK Meter data (Agent A)",
        "endpoint": "address-profile",
        "endpointId": out.get("endpointId"),
        "requestId": out.get("requestId"),
        "tx": out.get("settlement_tx"),
        "verifiedOnChain": out.get("settlement_verified"),
        "amountUnits": amount_units,
        "amount": ("%.6f" % (amount_units / 1e6)) if isinstance(amount_units, int) else None,
        "asset": "USDC.e",
    }
    return {
        "address": address,
        "chainId": P.CHAIN_ID,
        "riskScore": score,
        "riskLabel": label,
        "riskScale": "0 = low concern, 100 = high concern (heuristic, not a probability)",
        "riskFactors": factors,
        "basis": {
            "type": profile.get("type") if profile.get("type") in ("contract", "eoa") else None,
            "codeSize": code_size,
            "nonce": nonce,
            "balances": {k: (balances.get(k) if isinstance(balances.get(k), str) else None)
                         for k in ("HSK", "USDC.e", "USDT", "WHSK")},
            "block": profile.get("block") if isinstance(profile.get("block"), int) else None,
            "explorer": explorer,
        },
        "upstreamPayments": [upstream_payment],
        "composedBy": "agent-b/risk-brief",
        "disclaimer": "Heuristic signal derived from public on-chain data obtained via a paid upstream data provider. "
                      "NOT financial, legal, investment or compliance advice.",
    }


def risk_brief(rpc, arg):
    """WORK function for /v1/hsk/risk-brief (Seller.deliver calls WORK[path](self.rpc, arg)).

    `rpc`  - Agent B's own chain reader (used by buyer.buy for on-chain verification reads only; in the "exact"
             scheme the buyer signs an EIP-3009 authorization and Agent A submits the settlement, so Agent B sends
             no transaction here).
    `arg`  - the validated `address` (seller.handle_paid already matched P.ADDR_RE and lower-cased it).

    Pays Agent A over x402 for the raw address profile (a nested settlement from Agent B's wallet), then composes the
    brief. Raises chain.RpcError if the upstream purchase cannot deliver, so Seller.deliver() refunds the end-buyer.
    """
    cfg = _UPSTREAM
    if cfg is None:
        raise S.C.RpcError("agent-b upstream data provider is not configured")
    url = cfg["profile_url"] + "?address=" + arg
    try:
        out = BUY.buy(url=url, me=cfg["buyer"], rpc=rpc, contract=cfg["contract"],
                      expect_provider=cfg["a_provider"], expect_payee=cfg["a_payee"],
                      max_units=cfg["max_units"], cap_units=cfg["cap_units"], scheme="exact",
                      session_file=cfg["session_file"], pays_per_minute=cfg["pays_per_minute"],
                      journal_file=cfg["journal_file"], budget_s=cfg["budget_s"])
    except Exception as err:   # buyer raised BEFORE anything was signed (Refused / RpcError / malformed quote / ...)
        raise S.C.RpcError("upstream data unavailable: %s: %s" % (type(err).__name__, str(err)[:120]))
    # buy() never raises once a payment may exist; it reports the outcome via delivered / payment / refund.
    if not out.get("delivered") or not isinstance(out.get("data"), dict):
        raise S.C.RpcError("upstream did not deliver (payment=%s, refund=%s)"
                           % (out.get("payment"), (out.get("refund") or {}).get("status")))
    return _compose(arg, out["data"], out)


# --------------------------------------------------------------------------- configuration / resource installation
def configure_upstream(*, agent_a_url, a_provider, a_payee, contract, buyer_account, session_file,
                       max_units, cap_units, pays_per_minute=None, journal_file=None, budget_s=None):
    """Set the upstream (Agent A) buyer config that risk_brief() reads. Returns the stored dict."""
    global _UPSTREAM
    for name, v in (("--a-provider", a_provider), ("--a-payee", a_payee), ("--a-contract", contract)):
        if not (isinstance(v, str) and P.ADDR_RE.match(v)):
            raise ValueError("%s must be a 0x address" % name)
    u = urllib.parse.urlsplit(agent_a_url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("--agent-a-url must be an http(s) URL")
    _UPSTREAM = {
        "profile_url": agent_a_url.rstrip("/") + UPSTREAM_PATH,
        "agent_a_host": (u.hostname, u.port or (443 if u.scheme == "https" else 80)),
        "a_provider": a_provider.lower(), "a_payee": a_payee.lower(), "contract": contract.lower(),
        "buyer": buyer_account, "session_file": session_file,
        "max_units": int(max_units), "cap_units": int(cap_units), "pays_per_minute": pays_per_minute,
        "journal_file": journal_file, "budget_s": budget_s,
    }
    return _UPSTREAM


def install_resources():
    """Replace the seller's advertised resources/work with ONLY endpoint 3 (risk-brief). The Seller reads S.RESOURCES
    and S.WORK at request time, and the handler + the startup endpoint-provider check iterate S.RESOURCES, so after
    this Agent B advertises only /v1/hsk/risk-brief on /.well-known/x402 and checks only endpoint 3 at startup."""
    S.RESOURCES = dict(RISK_RESOURCE)
    S.WORK = {RISK_PATH: risk_brief}


# --------------------------------------------------------------------------- buyer key loading (hardened)
def load_buyer_key(path):
    """Agent B's buyer key: a file with exactly one NAME=0x<64 hex> line, a regular file, mode 0600, not a symlink.
    The value is never printed. Mirrors seller.read_key_file's safety checks."""
    try:
        st_ = os.lstat(path)
    except OSError:
        raise S.KeyFileError("buyer key file missing: %s" % os.path.basename(path))
    if stat.S_ISLNK(st_.st_mode) or not stat.S_ISREG(st_.st_mode):
        raise S.KeyFileError("buyer key file must be a regular file: %s" % os.path.basename(path))
    if st_.st_mode & 0o077:
        raise S.KeyFileError("buyer key file must be mode 0600: %s" % os.path.basename(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        raw = f.read(4096)
    vals = {}
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    if len(vals) != 1:
        raise S.KeyFileError("buyer key file must hold exactly one NAME=0x... line")
    value = next(iter(vals.values()))
    hexpart = value[2:] if isinstance(value, str) and value[:2].lower() == "0x" else value
    if not (isinstance(hexpart, str) and len(hexpart) == 64 and all(c in "0123456789abcdefABCDEF" for c in hexpart)):
        raise S.KeyFileError("buyer key must be a 32-byte hex private key (64 hex, optional 0x prefix)")
    return Account.from_key(value)


# --------------------------------------------------------------------------- CLI (mainnet)
def _units(dec_str, flag):
    try:
        return int(Decimal(dec_str) * 10**6)
    except (InvalidOperation, ValueError):
        sys.exit("%s must be a decimal USDC.e amount" % flag)


def main():
    ap = argparse.ArgumentParser(description="Agent B: x402 risk-brief seller that pays Agent A over x402 (A2A).")
    # --- Agent B's own seller side (same knobs as server/seller.py) ---
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8403)
    ap.add_argument("--contract", default=None, help="settlement contract (default: ops/state/deployment.json)")
    ap.add_argument("--network", choices=["mainnet", "local"], default="mainnet")
    ap.add_argument("--rpc", default="https://mainnet.hsk.xyz")
    ap.add_argument("--chain-id", type=int, default=P.CHAIN_ID)
    ap.add_argument("--key-dir", default=os.environ.get("AGENT_B_KEY_DIR") or os.environ.get("HSK_METER_KEY_DIR"),
                    help="dir with provider.env, adapter.env, token.env (one value each, 0600) for Agent B's seller")
    ap.add_argument("--state-dir", default=os.environ.get("AGENT_B_STATE_DIR") or os.path.join(ROOT, "run", "agent_b"))
    ap.add_argument("--settle-budget-hsk", type=float, default=2.0)
    ap.add_argument("--refund-budget-hsk", type=float, default=0.5)
    ap.add_argument("--hsk-usd", type=float, default=0.10)
    ap.add_argument("--no-price-feeds", action="store_true")
    ap.add_argument("--no-egress-guard", action="store_true", help="local development only")
    ap.add_argument("--smoke-refund-address", default=None, help="loopback-only operator smoke test")
    # --- upstream (Agent A) buyer side ---
    ap.add_argument("--agent-a-url", required=True, help="base URL of Agent A (the HSK Meter seller), e.g. https://a.example")
    ap.add_argument("--a-provider", required=True, help="Agent A's provider (settler) address to pin")
    ap.add_argument("--a-payee", required=True, help="Agent A's payee address to pin")
    ap.add_argument("--a-contract", default=None, help="settlement contract Agent A uses (default: --contract)")
    ap.add_argument("--b-buyer-key-file", default=os.environ.get("AGENT_B_BUYER_KEY_FILE"),
                    help="file with one NAME=0x<private key> line (0600) for Agent B's buyer wallet")
    ap.add_argument("--b-session-file", default=os.environ.get("AGENT_B_SESSION_FILE")
                    or os.path.join(ROOT, "run", "agent_b", "buyer_session.json"))
    ap.add_argument("--upstream-max-price", default="0.05", help="max USDC.e Agent B will pay Agent A per request")
    ap.add_argument("--upstream-cap", default="5.00", help="USDC.e spend cap across Agent B's buyer session ledger")
    ap.add_argument("--upstream-pays-per-minute", type=int, default=None)
    a = ap.parse_args()

    if not a.key_dir:
        sys.exit("--key-dir (or $AGENT_B_KEY_DIR) is required")
    if not a.b_buyer_key_file:
        sys.exit("--b-buyer-key-file (or $AGENT_B_BUYER_KEY_FILE) is required")
    contract = a.contract or json.load(open(os.path.join(ROOT, "ops", "state", "deployment.json")))["address"]
    a_contract = a.a_contract or contract
    for name, v in (("--contract", contract), ("--a-contract", a_contract), ("--a-provider", a.a_provider),
                    ("--a-payee", a.a_payee)):
        if not P.ADDR_RE.match(v):
            sys.exit("%s must be a 0x address" % name)
    if a.network == "mainnet" and (a.chain_id != P.CHAIN_ID or a.rpc != "https://mainnet.hsk.xyz"):
        sys.exit("mainnet uses chain 177 and the official RPC")
    u = urllib.parse.urlsplit(a.agent_a_url)
    if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")):
        sys.exit("--agent-a-url must be https (or http on localhost)")
    if a.smoke_refund_address and (a.host not in S.LOOPBACK or not P.ADDR_RE.match(a.smoke_refund_address)):
        sys.exit("--smoke-refund-address needs an address and a loopback --host")
    if a.network == "mainnet" and a.no_egress_guard:
        sys.exit("the egress guard cannot be disabled on mainnet")

    try:
        buyer_account = load_buyer_key(a.b_buyer_key_file)
    except S.KeyFileError as e:
        sys.exit(str(e))

    install_resources()
    configure_upstream(agent_a_url=a.agent_a_url, a_provider=a.a_provider, a_payee=a.a_payee, contract=a_contract,
                       buyer_account=buyer_account, session_file=a.b_session_file,
                       max_units=_units(a.upstream_max_price, "--upstream-max-price"),
                       cap_units=_units(a.upstream_cap, "--upstream-cap"),
                       pays_per_minute=a.upstream_pays_per_minute,
                       journal_file=os.path.join(a.state_dir, "agent_b_buyer_runs.jsonl"))

    if not a.no_egress_guard:
        import egress
        ru = urllib.parse.urlparse(a.rpc)
        allow = [(ru.hostname, ru.port or (443 if ru.scheme == "https" else 80)), _UPSTREAM["agent_a_host"]]
        egress.install(allow)   # Agent B must reach BOTH the chain RPC and Agent A's URL

    os.makedirs(a.state_dir, mode=0o700, exist_ok=True)
    os.makedirs(os.path.dirname(a.b_session_file) or ".", mode=0o700, exist_ok=True)
    cfg = {"network": a.network, "rpc": a.rpc, "chain_id": a.chain_id, "contract": contract, "key_dir": a.key_dir,
           "state_dir": a.state_dir, "settle_budget_wei": int(a.settle_budget_hsk * 1e18),
           "refund_budget_wei": int(a.refund_budget_hsk * 1e18), "hsk_usd": a.hsk_usd, "bind_host": a.host,
           "smoke_refund_address": a.smoke_refund_address,
           "price_feeds": not a.no_price_feeds and a.network == "mainnet"}
    try:
        seller = S.Seller(cfg)
    except S.KeyFileError as e:
        sys.exit(str(e))

    chain_id = int(seller.rpc("eth_chainId", [], tries=3), 16)
    if chain_id != a.chain_id:
        sys.exit("RPC chain id %d != %d" % (chain_id, a.chain_id))
    if len(seller.rpc("eth_getCode", [contract, "latest"], tries=3)) <= 2:
        sys.exit("no contract code at the configured address")
    if S.C.eth_call(seller.rpc, contract, "token()", [], [], ["address"])[0].lower() != P.USDCE.lower():
        sys.exit("contract does not settle in USDC.e")
    # Agent B's own endpoint (3) must be run by Agent B's provider key (only ep3 is in S.RESOURCES now).
    for path, spec in S.RESOURCES.items():
        e = seller.endpoint(spec["endpointId"])
        if e["provider"] != seller.provider.address.lower():
            sys.exit("endpoint %d is not provided by this server's provider key" % spec["endpointId"])
    if seller.feeds:
        seller.feeds.refresh()
    seller.check_balance()

    srv = S.Server((a.host, a.port), S.make_handler(seller))
    threading.Thread(target=S.reconciler, args=(seller,), daemon=True).start()
    print(json.dumps({"agent": "agent-b", "version": VERSION, "listening": "http://%s:%d" % (a.host, a.port),
                      "contract": contract, "network": P.NETWORK, "endpoint": RISK_PATH,
                      "settler": seller.provider.address, "hspAdapter": seller.adapter.address,
                      "buyer": buyer_account.address, "agentAUrl": a.agent_a_url,
                      "aProvider": a.a_provider, "aPayee": a.a_payee, "egressGuard": not a.no_egress_guard,
                      "endpoints": seller.info()}, default=str), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
