"""Persistent seller state (Swift Bot Labs): an append-only JSONL journal, replayed at startup.

Every state change the seller makes goes through apply(): the in-memory state is exactly the replay of the journal,
so a restart keeps settled/served/refunded payments, pending settlements, HSP receipts, payer bans, per-payer refund
counts and today's gas spending. The journal never holds secrets: no payment authorizations (EIP-3009 signatures),
no paywall tokens, no keys. It does keep the one HSP-draft receipt of a payment (adapter-signed, already given to the
payer) together with the payer's mandate signature, so that the same receipt can be returned again.

Events (all carry "ts", a unix time):
  settled      rid payer endpointId amount tx block logIndex payee paidAt feeWei [selfPaid]
  pending      rid payer endpointId amount tx until        (settlement sent, receipt not seen yet)
  pending_done rid
  served       rid
  refund       rid tx feeWei                               (refunded on-chain; tx may be null if found later)
  refund_sent  rid payer day                               (a refund tx was sent: counts against the payer's cap)
  refund_failed rid why
  refund_deferred rid why                                  (cap or budget: the reconciler sends it later)
  hsp_receipt  rid mandateHash hsp                         (the one receipt of this payment)
  ban          payer until why
  gas          kind wei day
  alert        kind detail
"""
import json
import os
import threading
import time

DAY = 86400


def utc_day(ts=None):
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))


class SellerState:
    def __init__(self, path, keep_s=3 * DAY, max_bytes=8_000_000):
        self.path, self.keep_s, self.max_bytes = path, keep_s, max_bytes
        self.lock = threading.RLock()
        self.recs = {}          # rid -> record
        self.bans = {}          # payer -> until
        self.refund_days = {}   # (payer, day) -> set(rid)
        self.gas = {}           # (day, kind) -> wei
        self.alerts = []
        if path:
            self._load()

    # ---- journal
    def _write(self, ev):
        if not self.path:
            return
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(ev, separators=(",", ":"), sort_keys=True) + "\n")

    def record(self, event, **fields):
        """Apply one event and append it to the journal (under the lock)."""
        ev = {"event": event, "ts": round(time.time(), 3), **fields}
        with self.lock:
            self._apply(ev)
            self._write(ev)
        return ev

    def _load(self):
        if not os.path.exists(self.path):
            return
        events = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue            # a torn last line after a crash
                if isinstance(ev, dict) and isinstance(ev.get("event"), str):
                    events.append(ev)
        for ev in events:
            try:
                self._apply(ev)
            except (KeyError, TypeError, ValueError):
                continue
        if os.path.getsize(self.path) > self.max_bytes:
            self._compact(events)

    def _compact(self, events):
        now = time.time()
        keep_rids = {rid for rid, r in self.recs.items() if now - r.get("ts", now) < self.keep_s
                     or r.get("pending") or r.get("refund_failed") or r.get("refund_deferred")}
        out = []
        for ev in events:
            rid = ev.get("rid")
            if rid is not None:
                if rid in keep_rids:
                    out.append(ev)
            elif ev["event"] == "ban":
                if ev.get("until", 0) > now:
                    out.append(ev)
            elif ev["event"] == "gas":
                if ev.get("day") == utc_day(now):
                    out.append(ev)
        tmp = self.path + ".compact"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            for ev in out:
                f.write(json.dumps(ev, separators=(",", ":"), sort_keys=True) + "\n")
        os.replace(tmp, self.path)
        self.recs = {rid: r for rid, r in self.recs.items() if rid in keep_rids}

    # ---- the state machine
    def _rec(self, rid):
        return self.recs.setdefault(rid, {"rid": rid, "served": 0, "refunded": False, "ts": time.time()})

    def _apply(self, ev):
        e = ev["event"]
        ts = float(ev.get("ts", time.time()))
        if e == "settled":
            r = self._rec(ev["rid"])
            r.update({k: ev[k] for k in ("payer", "endpointId", "amount", "tx", "block", "logIndex", "payee", "paidAt")})
            r.update(settled=True, feeWei=ev.get("feeWei", 0), selfPaid=bool(ev.get("selfPaid")), ts=ts)
            r.pop("pending", None)
        elif e == "pending":
            r = self._rec(ev["rid"])
            r.update(payer=ev["payer"], endpointId=ev["endpointId"], amount=ev["amount"], ts=ts,
                     pending={"tx": ev["tx"], "until": ev["until"]})
        elif e == "pending_done":
            self._rec(ev["rid"]).pop("pending", None)
        elif e == "served":
            r = self._rec(ev["rid"])
            r["served"] = r.get("served", 0) + 1
        elif e == "refund":
            r = self._rec(ev["rid"])
            r.update(refunded=True, refund_tx=ev.get("tx"), refund_failed=False, refund_deferred=False)
        elif e == "refund_sent":
            self.refund_days.setdefault((ev["payer"], ev["day"]), set()).add(ev["rid"])
        elif e == "refund_failed":
            r = self._rec(ev["rid"])
            r["refund_failed"] = True
            r["refund_attempts"] = r.get("refund_attempts", 0) + 1
            r["refund_why"] = ev.get("why")
        elif e == "refund_deferred":
            r = self._rec(ev["rid"])
            r["refund_deferred"] = True
            r["refund_why"] = ev.get("why")
        elif e == "hsp_receipt":
            r = self._rec(ev["rid"])
            if not r.get("hsp"):
                r["hsp"] = {"mandateHash": ev["mandateHash"], **ev["hsp"]}
        elif e == "ban":
            self.bans[ev["payer"]] = max(self.bans.get(ev["payer"], 0), float(ev["until"]))
        elif e == "gas":
            k = (ev["day"], ev["kind"])
            self.gas[k] = self.gas.get(k, 0) + int(ev["wei"])
        elif e == "alert":
            self.alerts = (self.alerts + [{"kind": ev.get("kind"), "detail": ev.get("detail"), "ts": ts}])[-20:]

    # ---- queries (callers hold no lock: each read is atomic enough for a dict lookup; mutations go via record())
    def banned_until(self, payer, now=None):
        until = self.bans.get(payer, 0)
        return until if until > (now or time.time()) else 0

    def refunds_today(self, payer, now=None):
        return len(self.refund_days.get((payer, utc_day(now)), ()))

    def gas_today(self):
        day = utc_day()
        return {kind: wei for (d, kind), wei in self.gas.items() if d == day}
