#!/usr/bin/env python3
"""Paying client for HSK Meter (Swift Bot Labs): fetches a paid URL, answers the x402 v2 402 with a signed EIP-3009
authorization (or pays the contract itself), and verifies everything it is told against HashKey Chain.

  buyer.py URL --key-file F --expect-provider 0x... --expect-payee 0x... [--scheme exact|tx] [--hsp]
           [--max-price 0.05] [--session-cap 0.20] [--contract 0x...] [--pin-adapter 0x...] [--session-file F]
           [--journal-file F] [--new-purchase] [--time-budget 45]
  (--key-file: a file with HKPW_BUYER_PK=0x...; defaults: $HSK_METER_BUYER_KEY_FILE, $HSK_METER_PROVIDER,
   $HSK_METER_PAYEE; --journal-file: $HSK_METER_BUYER_JOURNAL or ops/state/buyer_runs.jsonl)
  As a library: quote() (price + guard + USD value from the APRO USDC/USD feed, signs nothing), buy() (one paid
  request), status() (ledger + balance);
  agent/mcp_server.py exposes them as MCP tools.

Spend guard (checked BEFORE signing anything): network eip155:177 only; asset USDC.e only; payTo must be the
expected contract (--contract, default ops/state/deployment.json) whose token() is USDC.e; the endpoint must belong to
the pinned provider and pay the pinned payee (anyone can register endpoints on the contract); the quoted amount must
equal the contract's on-chain price for that endpoint and be <= --max-price; the session ledger (spent + reserved)
must stay <= --session-cap, and the amount is RESERVED in the ledger before the authorization is signed or the pay
transaction sent (an optional payments-per-minute limit is kept in the same ledger, under the same lock); the requestId
and nonce are recomputed locally from the resource we asked for; the authorization is valid for 120 s only. A
reservation becomes spent when the payment is verified, and is released only when the chain proves nothing can be
charged any more (getPayment unpaid and, after validBefore, authorizationState == false). A refund the seller reports
as queued or pending is watched in the ledger and credited back once getPayment(requestId).refunded is true.
After paying: the Paid event in the settlement tx (payer = us, amount, request), getPayment(requestId), and for
refunds the Refunded event and our balance. With --hsp, the adapter receipt must be for OUR signed mandate and the
settlement tx we verified, and getPayment is re-read; a missing receipt is reported as MISSING. Redirects are refused.

Once the cap is reserved, buy() never raises: the run record says what happened ("payment": signed_unconfirmed |
settled | refunded | not_charged, "delivered", "refund": {status}). The signed request is written to the session
ledger BEFORE it is sent and stays there while the outcome is unknown: any answer without the data that is not a
definitive seller refusal (a lost answer, an RPC error, a redirect, a 5xx, a 200 without data or with a body that is
not JSON, the seller still settling). The next buy() of the same URL, from this process, another process on the same
ledger or after a restart, resends the SAME PAYMENT-SIGNATURE (or tx headers) for up to RESEND_KEEP_S (inside the
seller's 1 h re-serve window): the seller re-serves a paid request id, and a request id can be charged at most once
on-chain, so a retry never pays twice. A "not refunded: already served" answer (the seller served this payment
before, e.g. into an answer we lost, and its data source failed on this re-serve) keeps the entry too: retrying after
its Retry-After resends the same payment. The entry is dropped on a verified, pending or queued refund and on a
definitive refusal (e.g. 429 already_served, 410 payment_expired).
After a DELIVERED answer the signed request stays in the ledger for DELIVERED_KEEP_S (well inside the seller's 1 h /
3-serve window): the next buy() of the same URL resends it and the seller re-serves the paid request for free
("redelivery"), e.g. when an MCP client timed out or cancelled the first call and calls again. new_purchase=True
skips that and buys a new response; it never skips a payment whose outcome is still unknown.
Wall clock: one buy() is bounded by budget_s (default BUY_BUDGET_S, below typical MCP client timeouts): the paid
request's timeout, the seller's "still settling" retries and the receipt polls stop when the budget is used up, and
the result then says resend_on_retry instead of blocking. A cancel Event (an MCP notifications/cancelled) stops the
waits the same way; the pending resend is kept. Single RPC reads keep their own timeouts.
"""
import argparse
import contextlib
import fcntl
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
import chain as C        # noqa: E402
import hsp_draft as HSP  # noqa: E402
import oracle as O       # noqa: E402
import protocol as P     # noqa: E402
from eth_abi import encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402

STATE = os.path.join(ROOT, "ops", "state")
SESSION = os.path.join(STATE, "buyer_session.json")
JOURNAL = os.environ.get("HSK_METER_BUYER_JOURNAL") or os.path.join(STATE, "buyer_runs.jsonl")
AUTH_VALID_S = 120
TX_RESERVE_S = 600
RESEND_KEEP_S = 3000       # the seller re-serves a paid request id for 1 h (3 times): resend it, never buy it twice
DELIVERED_KEEP_S = 600     # a delivered payment is re-served (free) to a same-URL call for 10 min, unless new_purchase
BUY_BUDGET_S = 45          # wall-clock bound of one buy(); MCP clients commonly time a tool call out after ~60 s
MIN_SIGN_LEFT_S = 15       # never sign a payment with less of the budget left than this (nothing signed: try again)
MAX_RETRY_AFTER_S = 3600
MAX_RESENDS = 100
REFUND_WATCH_S = 7 * 86400  # a queued refund is normally sent the next UTC day; stop watching after a week
RECEIPT_POLLS, RECEIPT_WAIT_S = 20, 2
MAX_ERROR_CHARS = 4000
MAX_NOTE_CHARS = 300
REFUNDED_TOPIC = C.REFUNDED_TOPIC


class Refused(Exception):
    pass


class Cancelled(Exception):
    """The caller gave up on this call (an MCP notifications/cancelled)."""


class Budget:
    """The wall-clock budget of one buy() plus the caller's cancel signal (a threading.Event or None). Waits and polls
    end early once the budget is used up or the call is cancelled; the outcome then stays unknown (and resendable)."""

    def __init__(self, seconds=None, cancel=None):
        self.deadline = None if seconds is None else time.time() + seconds
        self.cancel = cancel

    def cancelled(self):
        return self.cancel is not None and self.cancel.is_set()

    def left(self):
        if self.cancelled():
            return 0.0
        return float("inf") if self.deadline is None else max(0.0, self.deadline - time.time())

    def sleep(self, s):
        """Sleep s seconds and return True; return False at once when that would run past the budget or the call
        is cancelled (also when it is cancelled during the sleep)."""
        if s > self.left():
            return False
        if self.cancel is not None:
            return not self.cancel.wait(s)
        time.sleep(s)
        return True

    def timeout(self, cap=120):
        """An HTTP timeout that ends with the budget (at least 1 s)."""
        return max(1.0, min(cap, self.left()))


class _OutOfTime(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise Refused("server tried to redirect; refused")


OPENER = urllib.request.build_opener(_NoRedirect)


def _json_or_raw(raw):
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {"raw": raw[:300].decode("utf-8", "replace")}


def http_get(url, headers, timeout=120):
    req = urllib.request.Request(url, headers={"user-agent": "hsk-meter-buyer/0.1", **headers})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), _json_or_raw(r.read(200_001)[:200_000])
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(200_001)[:200_000]
        finally:
            e.close()
        return e.code, dict(e.headers), _json_or_raw(raw)


def load_key(path):
    """Only HKPW_BUYER_PK is taken from the file."""
    with open(path) as f:
        for line in f:
            if line.startswith("HKPW_BUYER_PK="):
                return Account.from_key(line.strip().split("=", 1)[1])
    raise Refused("key file has no HKPW_BUYER_PK")


def journal(entry, path=None):
    """Append one run record (0600 JSONL). Best effort: a full disk must never turn a paid run into an exception."""
    path = path or JOURNAL
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}, default=str) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def _capped(obj, limit=MAX_ERROR_CHARS):
    """A seller-supplied object if its JSON fits in `limit` characters, else a marker (untrusted, never interpreted)."""
    try:
        return obj if len(json.dumps(obj, default=str)) <= limit else "(omitted: too large)"
    except (TypeError, ValueError, RecursionError):
        return "(omitted: not serializable)"


def _tx_hash(v):
    """A seller-supplied tx hash, only if it is well-formed (checked BEFORE any RPC call)."""
    return v.lower() if isinstance(v, str) and P.B32_RE.match(v) else None


class Session:
    """The spend-cap ledger, one JSON file read-modify-written under an exclusive flock (concurrent buyer runs and MCP
    servers on the same file share the cap and the payments-per-minute limit):
    {"spent_units", "reserved": {rid: {amount, scheme, nonce, expires}}, "paid": {rid: amount},
     "refund_pending": {rid: {amount, tx, since}}, "pay_times": [unix time of each reservation in the last minute],
     "resend": {"<payer> <url>": {url, rid, eid, amount, scheme, headers, mandate, msig, paymentId, pay_tx,
                                  settled_tx, delivered_at, until}}}
    "resend" holds signed payments whose outcome is not known yet (delivered_at null) and, for DELIVERED_KEEP_S,
    delivered ones a same-URL call gets re-served (see buy()); the file is kept at mode 0600."""

    def __init__(self, path):
        self.path = path

    @contextlib.contextmanager
    def _ledger(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            os.fchmod(f.fileno(), 0o600)        # it holds signed (unsettled) payment headers
            try:
                d = json.loads(f.read() or "{}")
            except ValueError:
                raise Refused("session ledger is corrupt; refusing to spend")
            d.setdefault("spent_units", 0)
            d.setdefault("reserved", {})
            d.setdefault("paid", {})
            d.setdefault("refund_pending", {})
            d.setdefault("pay_times", [])
            d.setdefault("resend", {})
            yield d
            f.seek(0)
            f.truncate()
            f.write(json.dumps(d, sort_keys=True))

    def total(self):
        with self._ledger() as d:
            return d["spent_units"] + sum(r["amount"] for r in d["reserved"].values())

    def totals(self):
        with self._ledger() as d:
            return d["spent_units"], sum(r["amount"] for r in d["reserved"].values())

    def reserve(self, rid, amount, cap, scheme, nonce, expires, pays_per_minute=None, now=None):
        now = now or time.time()
        with self._ledger() as d:
            if rid in d["reserved"] or rid in d["paid"]:
                raise Refused("this request id is already reserved or paid")
            used = d["spent_units"] + sum(r["amount"] for r in d["reserved"].values())
            if used + amount > cap:
                raise Refused(f"session spend cap reached ({used} spent or reserved + {amount} > {cap})")
            d["pay_times"] = [t for t in d["pay_times"] if isinstance(t, (int, float)) and now - 60 < t <= now + 5]
            if pays_per_minute is not None and len(d["pay_times"]) >= pays_per_minute:
                raise Refused(f"rate limit: at most {pays_per_minute} payments per minute")
            d["pay_times"].append(now)
            d["reserved"][rid] = {"amount": amount, "scheme": scheme, "nonce": nonce, "expires": int(expires)}

    def commit(self, rid):
        """The payment is verified on-chain: reserved -> spent."""
        with self._ledger() as d:
            r = d["reserved"].pop(rid, None)
            if r is not None:
                d["spent_units"] += r["amount"]
                d["paid"][rid] = r["amount"]
                while len(d["paid"]) > 1000:
                    d["paid"].pop(next(iter(d["paid"])))
            return d["spent_units"]

    def refunded(self, rid):
        """The refund is verified on-chain: nothing was spent for rid."""
        with self._ledger() as d:
            d["refund_pending"].pop(rid, None)
            if d["reserved"].pop(rid, None) is None and rid in d["paid"]:
                d["spent_units"] -= d["paid"].pop(rid)
            return d["spent_units"]

    def refund_pending(self, rid, amount, tx=None, now=None):
        """The seller says the refund is queued or sent but we could not verify it yet: watch it (see reconcile)."""
        with self._ledger() as d:
            d["refund_pending"][rid] = {"amount": amount, "tx": tx, "since": int(now or time.time())}
            while len(d["refund_pending"]) > 200:
                d["refund_pending"].pop(next(iter(d["refund_pending"])))

    # ---- signed payments whose outcome is unknown: resent (never re-bought) by the next buy() of the same URL
    @staticmethod
    def _live_resends(d, now):
        d["resend"] = {k: v for k, v in d["resend"].items()
                       if isinstance(v, dict) and isinstance(v.get("until"), (int, float)) and v["until"] >= now}
        return d["resend"]

    def resend_get(self, key, now=None):
        """The pending signed payment for `key` as a buy() context, or None (expired entries are pruned)."""
        with self._ledger() as d:
            v = self._live_resends(d, now or time.time()).get(key)
        return None if v is None else _ctx_from_entry(v)

    def resend_put(self, key, url, ctx):
        with self._ledger() as d:
            r = self._live_resends(d, time.time())
            r[key] = _ctx_to_entry(url, ctx)
            while len(r) > MAX_RESENDS:       # delivered entries go first: an unknown outcome is the one that costs money
                r.pop(min(r, key=lambda k: (r[k].get("delivered_at") is None, r[k]["until"])))

    def resend_drop(self, key, rid):
        with self._ledger() as d:
            v = d["resend"].get(key)
            if not isinstance(v, dict) or v.get("rid") == rid:
                d["resend"].pop(key, None)

    def resends(self, me, now=None, delivered=False):
        """[{url, requestId, until}] of `me`'s signed payments whose outcome is unknown, or with delivered=True the
        delivered ones a same-URL call gets re-served (+ deliveredAt). Never the headers."""
        with self._ledger() as d:
            rows = [(v, v.get("delivered_at")) for k, v in self._live_resends(d, now or time.time()).items()
                    if k.startswith(me.lower() + " ")]
        return [{"url": v.get("url"), "requestId": v.get("rid"), "until": int(v["until"]),
                 **({"deliveredAt": int(at)} if delivered else {})}
                for v, at in rows if (at is not None) == delivered and (at is None or isinstance(at, (int, float)))]

    def reconcile(self, rpc, contract, me, now=None):
        """Settle expired reservations and watched refunds from the chain. Returns [(rid, outcome)]. An entry whose
        chain read fails is kept as it is (fail closed) and retried on the next call."""
        now = now or time.time()
        me = me.lower()
        out = []
        with self._ledger() as d:
            for rid, r in list(d["reserved"].items()):
                if now < r["expires"] + 30:
                    continue
                try:
                    pay = C.read_payment(rpc, contract, rid)
                    if pay["payer"] == me:
                        d["reserved"].pop(rid)
                        if not pay["refunded"]:
                            d["spent_units"] += pay["amount"]
                            d["paid"][rid] = pay["amount"]
                        else:
                            d["refund_pending"].pop(rid, None)
                        out.append((rid, "refunded" if pay["refunded"] else "paid"))
                        continue
                    if r["scheme"] == "exact" and r.get("nonce"):
                        if C.authorization_state(rpc, P.USDCE, me, r["nonce"]):
                            out.append((rid, "kept: authorization used but no payment visible yet"))
                            continue
                except C.RpcError:
                    out.append((rid, "kept: chain unavailable"))
                    continue
                d["reserved"].pop(rid)
                d["refund_pending"].pop(rid, None)
                for k in [k for k, v in d["resend"].items() if isinstance(v, dict) and v.get("rid") == rid]:
                    d["resend"].pop(k)              # provably never charged: nothing left to resend
                out.append((rid, "released"))
            for rid, r in list(d["refund_pending"].items()):
                if rid not in d["paid"]:
                    if rid not in d["reserved"]:         # nothing left to give back
                        d["refund_pending"].pop(rid)
                    continue
                try:
                    block = "latest"
                    if _tx_hash(r.get("tx")):        # read at the refund's block: a lagging node cannot hide it
                        rc = rpc("eth_getTransactionReceipt", [r["tx"]], tries=2)
                        if isinstance(rc, dict) and rc.get("status") == "0x1" and rc.get("blockNumber"):
                            block = rc["blockNumber"]
                    pay = C.read_payment(rpc, contract, rid, block=block)
                except C.RpcError:
                    continue
                if pay["payer"] == me and pay["refunded"]:
                    d["spent_units"] -= d["paid"].pop(rid)
                    d["refund_pending"].pop(rid)
                    out.append((rid, "refunded"))
                elif now > r.get("since", 0) + REFUND_WATCH_S:
                    d["refund_pending"].pop(rid)
                    out.append((rid, "refund never seen on-chain; stays spent"))
        return out


def check_quote(rpc, body, url, me, contract, max_units, cap_units, scheme, session, expect_provider, expect_payee):
    if body.get("x402Version") != 2 or not isinstance(body.get("accepts"), list):
        raise Refused("not an x402 v2 PaymentRequired")
    want = "exact" if scheme == "exact" else "hsk-paywall-tx"
    acc = next((a for a in body["accepts"] if isinstance(a, dict) and a.get("scheme") == want), None)
    if acc is None:
        raise Refused(f"no {want} option offered")
    x = acc.get("extra") or {}
    if acc.get("network") != P.NETWORK:
        raise Refused("wrong network")
    if str(acc.get("asset", "")).lower() != P.USDCE.lower():
        raise Refused("asset is not USDC.e")
    if str(acc.get("payTo", "")).lower() != contract.lower():
        raise Refused("payTo is not the expected settlement contract")
    if str(x.get("payer", "")).lower() != me.lower():
        raise Refused("quote is bound to another payer")
    u = urllib.parse.urlsplit(url)
    if x.get("resource") != f"GET {u.path}?{u.query.lower()}":
        raise Refused("quote is for another resource")
    eid = int(x["endpointId"])
    amount = int(acc["amount"])
    e = C.read_endpoint(rpc, contract, eid)               # independent of the server
    if e["provider"] != expect_provider.lower():
        raise Refused(f"endpoint {eid} is not run by the pinned provider")
    if e["payee"] != expect_payee.lower():
        raise Refused(f"endpoint {eid} does not pay the pinned payee")
    if not e["active"] or e["price"] != amount:
        raise Refused(f"on-chain price {e['price']} != quoted {amount} (or inactive)")
    if amount > max_units:
        raise Refused(f"price {amount} above --max-price")
    if session.total() + amount > cap_units:
        raise Refused("session spend cap reached")
    _, rid = P.compute_request_id(P.CHAIN_ID, contract.lower(), eid, me.lower(), x["resource"], x["token"].lower())
    if rid != x["requestId"].lower():
        raise Refused("requestId does not match our own computation")
    if scheme == "exact" and P.authorization_nonce(contract.lower(), eid, rid) != str(x.get("nonce", "")).lower():
        raise Refused("nonce does not match authorizationNonce(endpointId, requestId)")
    return acc, eid, amount, rid, e


def _receipt(rpc, tx, polls=None, wait_s=None, budget=None):
    """The receipt of `tx`, polled (our RPC node can be a block behind the seller's). An RPC error counts as "not
    seen yet". Returns (receipt or None, the last RPC error or None); polling cut short by the budget (or a cancel)
    returns an _OutOfTime error, so the caller reports "unknown", never "missing". The first read always happens."""
    polls = RECEIPT_POLLS if polls is None else polls
    wait_s = RECEIPT_WAIT_S if wait_s is None else wait_s
    budget = budget or Budget()
    err = None
    for i in range(polls):
        try:
            rc = rpc("eth_getTransactionReceipt", [tx], tries=2)
            if isinstance(rc, dict) and rc:
                return rc, None
            err = None
        except C.RpcError as e:
            err = e
        if i + 1 < polls and not budget.sleep(wait_s):
            return None, _OutOfTime("not seen before the call's time budget ran out")
    return None, err


def verify_paid(rpc, contract, tx, rid, me, amount, eid, budget=None):
    """(True, why) when the Paid event matches; (False, why) when the tx is missing, reverted or does not pay this
    request; (None, why) when the chain could not be read (unknown, never an exception)."""
    if not _tx_hash(tx):
        return None, "malformed settlement tx hash from the seller (not looked up)"
    rc, err = _receipt(rpc, tx.lower(), budget=budget)
    if rc is None and err is not None:
        return None, "could not read the settlement receipt: " + str(err)[:120]
    if not rc or rc.get("status") != "0x1":
        return False, "settlement tx missing or reverted"
    try:
        logs = C.paid_logs(rc, contract, rid)
    except Exception:
        return False, "malformed settlement receipt"
    if len(logs) != 1 or logs[0]["payer"] != me.lower() or logs[0]["amount"] != amount or logs[0]["endpointId"] != eid:
        return False, "no matching Paid event"
    return True, "Paid event matches (payer, amount, endpoint, request)"


def verify_refund(rpc, contract, tx, rid, me, amount, state_tries=10, wait_s=1.5, budget=None):
    budget = budget or Budget()
    if not _tx_hash(tx):
        return None, "malformed refund tx hash from the seller (not looked up)"
    rc, err = _receipt(rpc, tx.lower(), budget=budget)
    if rc is None and err is not None:
        return None, "could not read the refund receipt: " + str(err)[:120]
    if not rc or rc.get("status") != "0x1":
        return False, "refund tx missing or reverted"
    for lg in rc.get("logs") or []:
        t = lg.get("topics") or []
        try:
            match = (str(lg.get("address", "")).lower() == contract.lower() and len(t) == 4
                     and t[0].lower() == REFUNDED_TOPIC and t[2].lower() == rid.lower()
                     and t[3][-40:].lower() == me.lower()[2:] and int(lg["data"], 16) == amount)
        except (AttributeError, KeyError, TypeError, ValueError):
            match = False
        if match:
            # read the state AT the refund's block: a lagging node answers stale or errors, so retry (seen live on
            # mainnet 2026-10-23: the event matched while getPayment().refunded still read False one block behind)
            for i in range(state_tries):
                try:
                    if C.read_payment(rpc, contract, rid, block=rc.get("blockNumber", "latest"))["refunded"]:
                        return True, "Refunded event matches; getPayment().refunded = True"
                except C.RpcError:
                    pass
                if i + 1 < state_tries and not budget.sleep(wait_s):
                    return None, "Refunded event matches; getPayment().refunded not confirmed within the time budget"
            return False, "Refunded event matches but getPayment().refunded is still False"
    return False, "no matching Refunded event"


def _onchain_hsp(rpc, contract, rid, me, amount, tries=3):
    """The relying party's own re-read for HSP.verify(): getPayment(requestId) is ours, for this amount, not refunded."""
    def check(receipt):
        for i in range(tries):
            try:
                pay = C.read_payment(rpc, contract, rid)
            except C.RpcError:
                return False, True, "chain unavailable"
            if pay["payer"] != P.ZERO:
                break
            if i + 1 < tries:
                time.sleep(1)
        if pay["payer"] == P.ZERO:
            return False, True, "payment not visible yet"
        if pay["refunded"]:
            return False, False, "payment was refunded"
        if pay["payer"] != me.lower() or pay["amount"] != amount or receipt["payer"].lower() != me.lower():
            return False, False, "getPayment disagrees with the receipt"
        return True, False, ""
    return check


def check_hsp(rpc, contract, me, ctx, h, settlement_tx, settlement_verified, pinned):
    """Verify the seller's HSP answer against OUR signed mandate and the settlement we verified on-chain.
    Returns (outcomeClass, reasons). Never raises: a missing receipt is MISSING, a malformed one PERMANENT."""
    if not isinstance(h, dict) or not h:
        return "MISSING", ["the seller's answer carries no HSP receipt"]
    if not (isinstance(pinned, str) and P.ADDR_RE.match(pinned)):
        return "PERMANENT", ["no valid adapter address to verify against"]
    try:
        receipt = HSP.receipt_from_json(h["receipt"])
        rsig = h["receiptSignature"]
        if not (isinstance(rsig, str) and P.SIG_RE.match(rsig)):
            raise ValueError("bad receipt signature encoding")
    except Exception as err:
        return "PERMANENT", [f"malformed HSP receipt ({type(err).__name__})"]
    mandate, msig = ctx["mandate"], ctx["msig"]
    reasons = []
    if "mandate" in h:
        try:
            same = HSP.mandate_from_json(h["mandate"]) == mandate
        except Exception:
            same = False
        if not same:
            reasons.append("the answer's mandate is not the one we signed")
    if "mandateSignature" in h and str(h["mandateSignature"]).lower() != msig.lower():
        reasons.append("the answer's mandate signature is not ours")
    if "0x" + receipt["mandateHash"].hex() != ctx["paymentId"]:
        reasons.append("receipt is for another mandate")
    if not settlement_tx or "0x" + receipt["txHash"].hex() != settlement_tx.lower():
        reasons.append("receipt is for another settlement transaction")
    if receipt["settlementContract"].lower() != contract.lower():
        reasons.append("receipt names another settlement contract")
    if reasons:
        return "PERMANENT", reasons
    if settlement_verified is not True:
        return "RETRYABLE", ["the settlement is not verified on-chain yet"]
    dec = HSP.verify(mandate=mandate, mandate_sig=msig, receipt=receipt, receipt_sig=rsig, chain_id=P.CHAIN_ID,
                     pinned_adapter=pinned, onchain=_onchain_hsp(rpc, contract, ctx["rid"], me, ctx["amount"]))
    return dec["outcomeClass"], list(dec.get("reasons") or [])[:5]


def check_target(rpc, url, contract):
    """Refuse a URL we must not talk to and a contract that does not settle in USDC.e."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or (u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost")):
        raise Refused("URL must be https (or http on localhost)")
    if C.eth_call(rpc, contract, "token()", [], [], ["address"])[0].lower() != P.USDCE.lower():
        raise Refused("contract does not settle in USDC.e")


def _payment_required(hdr, body):
    hreq = {k.lower(): v for k, v in hdr.items()}.get("payment-required")
    try:
        same = bool(hreq) and P.parse_b64json(hreq, max_chars=16384) == body
    except P.ProtocolError:
        same = False
    if not same:
        raise Refused("PAYMENT-REQUIRED header missing or different from the body")


def quote(url, *, me, rpc, contract, expect_provider, expect_payee, max_units, cap_units, session_file=SESSION,
          scheme="exact", http=http_get):
    """Ask the seller for its price and run the spend guard on it. Signs nothing and reserves nothing."""
    check_target(rpc, url, contract)
    st, hdr, body = http(url, {"X-Payer": me.address})
    out = {"url": url, "quoteStatus": st}
    if st != 402 or not isinstance(body, dict):
        return {**out, "guard": "no quote: the seller did not answer 402", "sellerAnswer": body}
    _payment_required(hdr, body)
    session = Session(session_file)
    acc = next((a for a in body.get("accepts") or [] if isinstance(a, dict) and a.get("scheme") == "exact"), {}) or {}
    x = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
    try:
        acc, eid, amount, rid, e = check_quote(rpc, body, url, me.address, contract, max_units, cap_units, scheme,
                                               session, expect_provider, expect_payee)
        guard = "pass"
    except (Refused, KeyError, ValueError, TypeError, AttributeError) as err:
        guard = "refused: " + (str(err) if isinstance(err, Refused) else "malformed quote")
        eid = x.get("endpointId")
        amount = int(acc["amount"]) if str(acc.get("amount", "")).isdigit() else None
        e = {}
    ext = body.get("extensions") if isinstance(body.get("extensions"), dict) else {}
    out.update({"guard": guard, "endpointId": int(eid) if str(eid).isdigit() else None, "amountUnits": amount,
                "amountUSDCe": str(Decimal(amount) / 10**6) if amount is not None else None,
                "resource": x.get("resource"), "network": acc.get("network"), "asset": acc.get("asset"),
                "settlementContract": acc.get("payTo"), "provider": e.get("provider"), "payee": e.get("payee"),
                "hspOffered": bool(ext.get("hsp-draft")),
                "sessionUsedUnits": session.total(), "sessionCapUnits": cap_units, "maxPriceUnits": max_units})
    out.update(usd_view(rpc, amount))
    return out


def usd_view(rpc, amount):
    """The quote's USD value from our OWN read of the APRO USDC/USD feed on HashKey Chain (the seller's
    extensions["usd-estimate"] is not trusted). Informational only: the spend guard works in USDC.e units."""
    if amount is None:
        return {"amountUsd": None}
    try:
        r = O.read_checked(rpc, "USDC/USD")
    except O.FeedError as e:
        return {"amountUsd": None, "usdFeed": {"available": False, "reason": str(e)[:120]}}
    return {"amountUsd": f"{O.usd_of_units(amount, r):f}", "usdFeed": O.public(r)}


def status(*, me, rpc, cap_units, session_file=SESSION, contract=None):
    """The spend ledger and the payer's USDC.e balance. With `contract`, expired reservations and watched refunds are
    reconciled from the chain first (so a refund the seller sent later gives the cap back)."""
    out = {"payer": me.address}
    if contract:
        rec = Session(session_file).reconcile(rpc, contract, me.address)
        if rec:
            out["ledgerReconciled"] = rec
    session = Session(session_file)
    with session._ledger() as d:
        spent, reserved = d["spent_units"], sum(r["amount"] for r in d["reserved"].values())
        watched = len(d["refund_pending"])
    resends = session.resends(me.address)
    redeliver = session.resends(me.address, delivered=True)
    bal = C.eth_call(rpc, P.USDCE, "balanceOf(address)", ["address"], [me.address], ["uint256"])[0]
    out.update({"spentUnits": spent, "reservedUnits": reserved, "capUnits": cap_units,
                "remainingUnits": max(0, cap_units - spent - reserved), "refundsWatched": watched,
                "pendingResends": resends, "reServable": redeliver, "usdceBalanceUnits": bal,
                "usdceBalance": str(Decimal(bal) / 10**6)})
    return out


# ---------------------------------------------------------------------- idempotent retries (kept in the ledger)
def _resend_key(me, url):
    return f"{me.lower()} {url}"


def _ctx_to_entry(url, ctx):
    """A buy() context as a JSON ledger entry (the HSP mandate as its JSON form)."""
    return {"url": url, "rid": ctx["rid"], "eid": ctx["eid"], "amount": ctx["amount"], "scheme": ctx["scheme"],
            "headers": ctx["headers"], "paymentId": ctx.get("paymentId"), "msig": ctx.get("msig"),
            "mandate": HSP.mandate_to_json(ctx["mandate"]) if ctx.get("mandate") is not None else None,
            "pay_tx": ctx.get("pay_tx"), "settled_tx": ctx.get("settled_tx"), "delivered_at": ctx.get("delivered_at"),
            "until": ctx["until"]}


def _ctx_from_entry(v):
    """A ledger entry back to a buy() context. A malformed entry fails closed (Refused) until it expires."""
    try:
        h = v["headers"]
        if not (isinstance(h, dict) and h and all(isinstance(a, str) and isinstance(b, str) for a, b in h.items())
                and isinstance(v["rid"], str) and P.B32_RE.match(v["rid"]) and v["scheme"] in ("exact", "tx")
                and type(v["eid"]) is int and type(v["amount"]) is int):
            raise ValueError("bad entry")
        mandate = HSP.mandate_from_json(v["mandate"]) if v.get("mandate") is not None else None
        if mandate is not None and not (isinstance(v.get("msig"), str) and isinstance(v.get("paymentId"), str)):
            raise ValueError("bad mandate entry")
        at = v.get("delivered_at")
        if at is not None and (type(at) not in (int, float)):
            raise ValueError("bad delivered_at")
    except (KeyError, TypeError, ValueError):
        raise Refused("a pending signed payment for this URL in the session ledger is malformed; refusing to pay it "
                      "again until it expires")
    return {"rid": v["rid"].lower(), "eid": v["eid"], "amount": v["amount"], "scheme": v["scheme"], "headers": h,
            "mandate": mandate, "msig": v.get("msig") if mandate is not None else None,
            "paymentId": v.get("paymentId") if mandate is not None else None, "pay_tx": _tx_hash(v.get("pay_tx")),
            "settled_tx": _tx_hash(v.get("settled_tx")), "not_charged": False, "delivered_at": at,
            "until": v["until"]}


def pending_resend(session_file, me, url, delivered=False):
    """The request id of the signed payment for `url` whose outcome is unknown (with delivered=True: the delivered
    one a same-URL call gets re-served), or None."""
    ctx = Session(session_file).resend_get(_resend_key(me, url))
    return ctx["rid"] if ctx and (ctx.get("delivered_at") is not None) == delivered else None


def _http_call(http, url, headers, budget):
    """http() with a timeout that ends with the budget. With a cancel signal the wait runs in a helper thread and is
    abandoned on cancel (Cancelled): the request may still reach the seller, so the outcome stays unknown and the next
    call resends the same payment."""
    if budget.cancelled():
        raise Cancelled("the client cancelled the call")
    timeout = budget.timeout()
    if budget.cancel is None:
        return http(url, headers, timeout=timeout)
    box, done = {}, threading.Event()

    def run():
        try:
            box["r"] = http(url, headers, timeout=timeout)
        except BaseException as e:     # handed to the caller's thread below
            box["e"] = e
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    while not done.wait(0.05):
        if budget.cancel.is_set():
            raise Cancelled("the client cancelled the call while the request was in flight")
    if "e" in box:
        raise box["e"]
    return box["r"]


def _send_paid(http, url, headers, scheme, budget=None):
    """Send the paid request; while the seller says it is still settling, resend the same headers (safe: a request id
    is charged at most once on-chain), as long as the call's time budget allows."""
    budget = budget or Budget()
    st, hdr, res = None, {}, {}
    for _ in range(8 if scheme == "exact" else 5):
        st, hdr, res = _http_call(http, url, headers, budget)
        retryable = isinstance(res, dict) and res.get("retryable")
        if scheme == "exact":
            if st not in (409, 503) or not retryable:
                break
            ra = str({k.lower(): v for k, v in hdr.items()}.get("retry-after", "3"))
            wait = min(int(ra), 10) if ra.isascii() and ra.isdigit() else 3
        else:
            if st != 402 or not retryable:
                break
            wait = 2
        if not budget.sleep(wait):
            break          # out of time or cancelled: the outcome stays unknown and the next call resends
    return st, hdr, res


def _refund_state(rpc, contract, rid, me, amount, r, session, budget=None):
    """The refund the seller reports, checked on-chain. Returns {status: verified|pending|queued|none, ...}; "none"
    means the seller served this payment before and will not refund it (it can still be re-served)."""
    r = r if isinstance(r, dict) else {}
    rtx = _tx_hash(r.get("tx"))
    note = str(r.get("note") or "")[:MAX_NOTE_CHARS]
    out = {"tx": rtx, "sellerSaysRefunded": r.get("refunded") is True, "note": note or None}
    if rtx:
        ok, why = verify_refund(rpc, contract, rtx, rid, me, amount, budget=budget)
    else:                         # no tx (e.g. already refunded earlier): read getPayment(requestId) directly
        try:
            pay = C.read_payment(rpc, contract, rid)
            ok = pay["payer"] == me.lower() and pay["refunded"]
            why = "getPayment().refunded = True" if ok else "getPayment().refunded is False"
        except C.RpcError as err:
            ok, why = None, "could not read getPayment: " + str(err)[:120]
    out["verified"], out["check"] = ok, why
    if ok:
        out["status"] = "verified"
    elif rtx:
        out["status"] = "pending"
    elif "already served" in note.lower():
        out["status"] = "none"
    else:
        out["status"] = "queued"
    return out


def _after_send(rpc, contract, me, session, ctx, out, st, hdr, res, pin_adapter, budget=None):
    """Everything after the paid request was answered. Never raises (the caller still guards it)."""
    out["status"] = st
    res = res if isinstance(res, dict) else {"raw": str(res)[:300]}
    hl = {str(k).lower(): v for k, v in hdr.items()} if isinstance(hdr, dict) else {}
    ra = hl.get("retry-after")
    if not (st == 200 and "data" in res) and isinstance(ra, str) and ra.isascii() and ra.isdigit():
        out["retry_after_s"] = min(int(ra), MAX_RETRY_AFTER_S)      # seller text: only a bounded integer is kept
    pr = hl.get("payment-response")
    pr_tx = None
    if pr:
        try:
            out["payment_response"] = P.parse_b64json(pr, max_chars=4096)
        except P.ProtocolError as err:          # informational only: a bad header is ignored, never fatal
            out["payment_response_ignored"] = str(err)[:120]
        if isinstance(out.get("payment_response"), dict) and out["payment_response"].get("success") is True:
            pr_tx = out["payment_response"].get("transaction")      # still verified on-chain below
    pay = res.get("payment") if isinstance(res.get("payment"), dict) else {}
    # the body's tx, else the PAYMENT-RESPONSE header's (e.g. a proxy replaced the body), else what we know already
    raw_tx = pay.get("tx") or pr_tx or ctx.get("pay_tx") or ctx.get("settled_tx")
    if raw_tx:
        tx = _tx_hash(raw_tx)
        ok, why = verify_paid(rpc, contract, tx or str(raw_tx)[:80], ctx["rid"], me, ctx["amount"], ctx["eid"],
                              budget=budget)
        out["settlement_tx"], out["settlement_verified"], out["settlement_check"] = tx, ok, why
        if ok:
            ctx["settled_tx"] = tx
            out["session_spent_units"] = session.commit(ctx["rid"])
    if st == 200 and "data" in res:
        out["data"] = res.get("data")
        out["delivered"] = True
        if ctx.get("mandate") is not None:
            pinned = pin_adapter or (res["hsp"].get("adapter") if isinstance(res.get("hsp"), dict) else None)
            dec, reasons = check_hsp(rpc, contract, me, ctx, res.get("hsp"), out.get("settlement_tx"),
                                     out.get("settlement_verified"), pinned)
            out["hsp_buyer_decision"] = dec
            if reasons:
                out["hsp_reasons"] = [str(x)[:200] for x in reasons]
            out["hsp_adapter_pinned"] = pinned if pin_adapter else f"{pinned} (from server: pass --pin-adapter to pin)"
        return
    out["delivered"] = False
    err = {k: v for k, v in res.items() if k != "refund"}
    if isinstance(err.get("message"), str):
        err["message"] = err["message"][:MAX_NOTE_CHARS]
    out["error"] = _capped(err)
    if "refund" in res or res.get("error") == "payment_refunded":
        rs = _refund_state(rpc, contract, ctx["rid"], me, ctx["amount"], res.get("refund"), session, budget)
        out["refund"] = rs
        out["refund_tx"], out["refund_verified"], out["refund_check"] = rs["tx"], rs["verified"], rs["check"]
        if rs["status"] == "verified":
            out["session_spent_units"] = session.refunded(ctx["rid"])
        elif rs["status"] in ("pending", "queued"):
            session.refund_pending(ctx["rid"], ctx["amount"], rs["tx"])
        out["message"] = str(res.get("message") or "")[:MAX_NOTE_CHARS] or None


def _definitive_refusal(st, res):
    """The seller's own final answer to the paid request: a JSON 4xx error that is neither retryable nor a rate limit.
    Anything else without the data (no answer, a redirect, a 5xx, a 200 without data or with a body that is not JSON,
    a proxy's page) leaves the outcome unknown."""
    return (isinstance(st, int) and 400 <= st < 500 and isinstance(res, dict) and "raw" not in res
            and isinstance(res.get("error"), str) and not res.get("retryable") and res.get("error") != "rate_limited")


def _outcome_unknown(out, ctx, st, res):
    """True when the signed payment must stay in the ledger to be resent: not delivered, possibly or surely charged,
    no refund on its way, and no definitive seller refusal. A refund status "none" (the seller served this payment
    before, e.g. into an answer we lost, and will not refund it) keeps it: the seller still re-serves the request.
    A verified, pending or queued refund, 429 already_served or 410 payment_expired / payment_refunded drop it."""
    rs = (out.get("refund") or {}).get("status")
    return (not out.get("delivered") and rs in (None, "none")
            and out.get("payment") in ("signed_unconfirmed", "settled")
            and ctx.get("headers") is not None and not _definitive_refusal(st, res))


def _payment_state(out, ctx):
    if out.get("refund", {}).get("status") == "verified":
        return "refunded"
    if ctx.get("not_charged") and not out.get("settlement_verified"):
        return "not_charged"
    if out.get("settlement_verified") is True:
        return "settled"
    return "signed_unconfirmed"


def buy(url, *, me, rpc, contract, expect_provider, expect_payee, max_units, cap_units, scheme="exact", hsp=False,
        pin_adapter=None, session_file=SESSION, http=http_get, journal_file=None, pays_per_minute=None,
        new_purchase=False, budget_s=None, cancel=None):
    """Pay for one request and verify what we are told against the chain. Returns the run record (as printed by the
    CLI). Raises Refused (guard) or RpcError only BEFORE anything is signed; a non-402 first answer is returned with
    quote_status and body. Once the cap is reserved it never raises: see "payment", "delivered" and "refund".
    A same-URL call within DELIVERED_KEEP_S of a delivery resends that payment ("redelivery") unless new_purchase;
    budget_s (default BUY_BUDGET_S) bounds the call's waits, `cancel` (a threading.Event) ends them early."""
    budget = Budget(BUY_BUDGET_S if budget_s is None else budget_s, cancel)
    t0 = time.time()
    check_target(rpc, url, contract)
    session = Session(session_file)
    jpath = journal_file or JOURNAL
    out = {"url": url, "scheme": scheme, "payer": me.address}
    rec = session.reconcile(rpc, contract, me.address)
    if rec:
        out["ledger_reconciled"] = rec
    key = _resend_key(me.address, url)
    ctx = session.resend_get(key)       # kept in the ledger: survives a restart, shared by every process on it
    if ctx is not None and new_purchase:
        if ctx.get("delivered_at") is not None:
            ctx = None                  # asked for explicitly: buy a new response (the delivered entry is replaced)
            out["new_purchase"] = True
        else:
            out["new_purchase_ignored"] = ("an earlier signed payment for this URL has no final outcome yet; it is "
                                           "resent instead of paying again")
    bal_before = None
    if ctx is not None:                 # an earlier signed payment for this URL: resend it, never re-buy
        out.update({"scheme": ctx["scheme"], "quote_status": 402, "endpointId": ctx["eid"], "amount": ctx["amount"],
                    "requestId": ctx["rid"], "resent": True})
        if ctx.get("paymentId"):
            out["paymentId"] = ctx["paymentId"]
        if ctx.get("delivered_at") is not None:      # it was delivered already: the seller re-serves it for free
            out["redelivery"], out["delivered_at"] = True, int(ctx["delivered_at"])
    else:
        st, hdr, body = _http_call(http, url, {"X-Payer": me.address}, budget)
        out["quote_status"] = st
        if st != 402 or not isinstance(body, dict):
            out["body"] = body
            return out
        _payment_required(hdr, body)
        try:
            acc, eid, amount, rid, e = check_quote(rpc, body, url, me.address, contract, max_units, cap_units, scheme,
                                                   session, expect_provider, expect_payee)
        except (Refused, KeyError, ValueError, TypeError, AttributeError) as err:
            msg = str(err) if isinstance(err, Refused) else f"malformed quote ({type(err).__name__})"
            journal({**out, "refused": msg}, jpath)
            raise Refused(msg)
        x = acc["extra"]
        out.update({"endpointId": eid, "amount": amount, "requestId": rid})
        ext = body.get("extensions") if isinstance(body.get("extensions"), dict) else {}
        if hsp and not ext.get("hsp-draft"):
            raise Refused("server offered no hsp-draft extension")
        bal_before = C.eth_call(rpc, P.USDCE, "balanceOf(address)", ["address"], [me.address], ["uint256"])[0]
        if budget.left() < MIN_SIGN_LEFT_S:
            msg = ("the call was cancelled" if budget.cancelled() else "the call's time budget is used up") + \
                  " before signing; nothing was signed, try again"
            journal({**out, "refused": msg}, jpath)
            raise Refused(msg)
        ctx = {"rid": rid, "eid": eid, "amount": amount, "scheme": scheme, "mandate": None, "msig": None,
               "paymentId": None, "headers": None, "not_charged": True, "delivered_at": None,
               "until": time.time() + RESEND_KEEP_S}
        now = int(time.time())
        try:   # counted against the cap (and the per-minute limit) BEFORE the signature or the pay tx exists
            if scheme == "exact":
                session.reserve(rid, amount, cap_units, "exact", x["nonce"].lower(), now + AUTH_VALID_S,
                                pays_per_minute=pays_per_minute)
            else:
                session.reserve(rid, amount, cap_units, "tx", None, now + TX_RESERVE_S, pays_per_minute=pays_per_minute)
        except Refused as err:
            journal({**out, "refused": str(err)}, jpath)
            raise
    # ---- from here on a payment may exist: never raise, always return (and journal) what is known
    st, res = None, None
    try:
        if ctx["headers"] is None and scheme == "exact":
            td = P.receive_auth_typed_data(me.address, contract, amount, 0, now + AUTH_VALID_S, x["nonce"])
            sig = "0x" + me.sign_message(encode_typed_data(full_message=td)).signature.hex()
            payload = {"x402Version": 2, "resource": body.get("resource"), "accepted": acc,
                       "payload": {"signature": sig, "authorization": {
                           "from": me.address, "to": contract, "value": str(amount), "validAfter": "0",
                           "validBefore": str(now + AUTH_VALID_S), "nonce": x["nonce"]}}}
            if hsp:
                mandate = HSP.mandate_message(payer=me.address, payee=e["payee"], token=P.USDCE, amount=amount,
                                              reference=rid, required_caps=[HSP.CAP_SETTLED], deadline=now + 900)
                msig = HSP.sign_mandate(me, mandate, P.CHAIN_ID)
                payload["extensions"] = {"hsp-draft": {"mandate": HSP.mandate_to_json(mandate), "signature": msig}}
                ctx.update({"mandate": mandate, "msig": msig, "paymentId": HSP.mandate_hash(mandate, P.CHAIN_ID)})
                out["paymentId"] = ctx["paymentId"]
            ctx["headers"] = {"X-Payer": me.address, "PAYMENT-SIGNATURE": P.b64json(payload)}
        elif ctx["headers"] is None:
            allowance = C.eth_call(rpc, P.USDCE, "allowance(address,address)", ["address", "address"], [me.address, contract], ["uint256"])[0]
            sender = C.Sender(rpc, me, P.CHAIN_ID, budgets={"settle": 5 * 10**17})
            if allowance < amount:
                rc = sender.send(P.USDCE, C.selector("approve(address,uint256)") + encode(["address", "uint256"], [contract, amount]))
                out["approve_tx"] = rc["transactionHash"]
                for _ in range(15):   # wait until the RPC node we read from sees the allowance (nodes can lag a block)
                    if C.eth_call(rpc, P.USDCE, "allowance(address,address)", ["address", "address"], [me.address, contract], ["uint256"])[0] >= amount:
                        break
                    time.sleep(2)
            try:                    # (an RpcError here means pay() was never sent: nothing charged)
                rc = sender.send(contract, C.selector("pay(uint256,bytes32,uint256)") + encode(
                    ["uint256", "bytes32", "uint256"], [eid, bytes.fromhex(rid[2:]), amount]), lag_retries=2)
            except C.PendingTx as pt:           # sent, no receipt yet: the seller (and the reconciler) look it up
                rc = {"transactionHash": pt.tx, "status": None}
            out["pay_tx"] = ctx["pay_tx"] = rc["transactionHash"]
            if rc.get("status") not in (None, "0x1"):
                out["post_payment_error"] = "pay() reverted on-chain; nothing was charged"
            else:
                ctx["not_charged"] = False
                ctx["headers"] = {"X-Payer": me.address, "X-Paywall-Token": x["token"], "X-Paywall-Tx": rc["transactionHash"]}
        if ctx["headers"] is not None:
            if not out.get("resent"):
                # write-ahead: a crash or restart while the request is in flight still resends instead of re-buying
                # (exact: if this fails nothing is sent; tx: pay() is already on its way, so carry on)
                try:
                    session.resend_put(key, url, ctx)
                except Exception:
                    if ctx["scheme"] == "exact":
                        raise
            ctx["not_charged"] = False          # the signed payment left this process: it may be charged
            st, hdr, res = _send_paid(http, url, ctx["headers"], ctx["scheme"], budget)
            _after_send(rpc, contract, me.address, session, ctx, out, st, hdr, res, pin_adapter, budget)
    except Exception as err:            # transport error, lost answer, RPC error, malformed answer, cancel, ...
        out["post_payment_error"] = f"{type(err).__name__}: {str(err)[:200]}"
    out.setdefault("delivered", False)
    out["payment"] = _payment_state(out, ctx)
    out["ms"] = int((time.time() - t0) * 1000)
    # Unknown outcome (see _outcome_unknown): keep the signed request and the next buy() of this URL resends it.
    # Delivered: keep it for DELIVERED_KEEP_S so a same-URL retry is re-served, not bought again. Otherwise: done.
    try:
        if _outcome_unknown(out, ctx, st, res):
            session.resend_put(key, url, ctx)
            out["resend_on_retry"], out["resend_until"] = True, int(ctx["until"])
        elif out["delivered"] and ctx.get("headers") is not None:
            if ctx.get("delivered_at") is None:
                ctx["delivered_at"] = int(time.time())
                ctx["until"] = min(ctx["until"], time.time() + DELIVERED_KEEP_S)   # never past the first window
            session.resend_put(key, url, ctx)
            out["redeliver_until"] = int(ctx["until"])
        elif ctx.get("headers") is not None:
            session.resend_drop(key, ctx["rid"])
    except Exception as err:
        out["resend_store_error"] = f"{type(err).__name__}: {str(err)[:120]}"
    try:
        out["session_spent_units"], out["session_reserved_units"] = session.totals()
    except Exception:
        pass
    try:
        bal_after = C.eth_call(rpc, P.USDCE, "balanceOf(address)", ["address"], [me.address], ["uint256"])[0]
        for _ in range(5):   # the RPC node we read from can be a block behind the settlement
            if bal_before is None or not out.get("settlement_verified") or out.get("refund_verified") or bal_after != bal_before:
                break
            if not budget.sleep(1.5):
                break
            bal_after = C.eth_call(rpc, P.USDCE, "balanceOf(address)", ["address"], [me.address], ["uint256"])[0]
        out["usdce_after"] = bal_after / 1e6
    except Exception:
        out["usdce_after"] = None
    out["usdce_before"] = bal_before / 1e6 if bal_before is not None else None
    journal({k: v for k, v in out.items() if k != "data"}, jpath)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--scheme", choices=["exact", "tx"], default="exact")
    ap.add_argument("--hsp", action="store_true", help="also sign the HSP-draft mandate and verify the receipt")
    ap.add_argument("--max-price", default="0.05")
    ap.add_argument("--session-cap", default="0.20")
    ap.add_argument("--contract", default=None)
    ap.add_argument("--pin-adapter", default=None, help="HSP adapter address pinned out-of-band")
    ap.add_argument("--key-file", default=os.environ.get("HSK_METER_BUYER_KEY_FILE"),
                    help="file with HKPW_BUYER_PK=0x... (or $HSK_METER_BUYER_KEY_FILE)")
    ap.add_argument("--expect-provider", default=os.environ.get("HSK_METER_PROVIDER"),
                    help="the provider address the endpoint must belong to (or $HSK_METER_PROVIDER)")
    ap.add_argument("--expect-payee", default=os.environ.get("HSK_METER_PAYEE"),
                    help="the payee address the endpoint must pay (or $HSK_METER_PAYEE)")
    ap.add_argument("--session-file", default=SESSION)
    ap.add_argument("--journal-file", default=None, help="run journal (default $HSK_METER_BUYER_JOURNAL or "
                                                         "ops/state/buyer_runs.jsonl)")
    ap.add_argument("--new-purchase", action="store_true",
                    help=f"pay again for a URL delivered in the last {DELIVERED_KEEP_S // 60} min (default: the "
                         "seller re-serves that payment for free); a payment with no final outcome is always resent")
    ap.add_argument("--time-budget", type=float, default=None,
                    help=f"wall-clock bound of the run in seconds (default {BUY_BUDGET_S}); an unfinished payment is "
                         "then reported for a resend")
    a = ap.parse_args()
    for flag, v in (("--key-file", a.key_file), ("--expect-provider", a.expect_provider), ("--expect-payee", a.expect_payee)):
        if not v:
            sys.exit(f"{flag} is required")
    for flag, v in (("--expect-provider", a.expect_provider), ("--expect-payee", a.expect_payee)):
        if not P.ADDR_RE.match(v):
            sys.exit(f"{flag} must be an address")
    contract = a.contract or json.load(open(os.path.join(STATE, "deployment.json")))["address"]
    try:
        out = buy(a.url, me=load_key(a.key_file), rpc=C.Rpc("https://mainnet.hsk.xyz"), contract=contract,
                  expect_provider=a.expect_provider, expect_payee=a.expect_payee,
                  max_units=int(Decimal(a.max_price) * 10**6), cap_units=int(Decimal(a.session_cap) * 10**6),
                  scheme=a.scheme, hsp=a.hsp, pin_adapter=a.pin_adapter, session_file=a.session_file,
                  journal_file=a.journal_file, new_purchase=a.new_purchase, budget_s=a.time_budget)
    except Refused as err:
        sys.exit(f"REFUSED: {err}")
    print(json.dumps(out, indent=1, default=str))
    if out.get("quote_status") != 402:
        sys.exit("expected 402")
    if out.get("resend_on_retry"):
        sys.exit(f"not delivered, outcome not final: do NOT buy again; run the same command again to resend the SAME "
                 f"signed payment {out.get('requestId')} (it cannot be charged twice)")
    if out.get("payment") == "signed_unconfirmed" and not out.get("delivered"):
        sys.exit(f"payment {out.get('requestId')} signed but not confirmed: do NOT buy this URL again; the amount stays "
                 "reserved until the chain settles it")


if __name__ == "__main__":
    main()
