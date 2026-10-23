"""Read-only APRO price feeds on HashKey Chain for HSK Meter (Swift Bot Labs).

APRO is one of the oracles HashKey Chain lists for mainnet (docs.hskchain.net, Build on HashKey Chain > Tools > Oracle).
Its feeds are push-model, Chainlink-compatible AggregatorV3 proxies. HSK Meter reads two of them:
  USDC/USD  0x244Ce344df8837c9d938867E2Ffbf0E4B0169B56   the USD value of a USDC.e price (quotes, dashboard)
  HSK/USD   0x86CE42c1b714149Dc3A7b17169EF67b5F78A224b   the gas guard: settlement gas must stay below the sale's value
Both addresses were checked on-chain before use: verified EACAggregatorProxy source on hsk.blockscout.com,
description() is the pair, decimals() is 8, version() is 4, and both were deployed by the same account as the BTC/USD
and USDT/USD feeds on the same docs page. The feeds' read access controller lets externally owned callers read
(msg.sender == tx.origin), which is what an eth_call is; a contract could not read them without being allow-listed.
Observed cadence on mainnet: one round at least every 24 h (heartbeat) plus deviation rounds in between, so a round
up to MAX_AGE_S old is current.

Everything here is a read: nothing is signed or sent. A feed that cannot be read, reports another pair or other
decimals, an unfinished round, a non-positive or out-of-range answer, a timestamp in the future or a round older than
MAX_AGE_S is unavailable, and callers fall back to safe behaviour: a quote leaves out its USD estimate, and the gas
guard keeps the operator's HSK price floor (a feed can make the guard stricter, never looser).
"""
import threading
import time
from decimal import Decimal

from eth_abi import decode
from eth_utils import keccak

MAX_AGE_S = 26 * 3600          # 24 h heartbeat + 2 h margin
FUTURE_SKEW_S = 300
TTL_S = 120                    # a reading is reused for 2 min before it is read again
FAIL_BACKOFF_S = 30            # after a failed read, wait before trying again (no RPC storm while a feed is down)
FEEDS = {
    # pair: (proxy address, decimals, (min, max) sane answer in USD)
    "USDC/USD": ("0x244Ce344df8837c9d938867E2Ffbf0E4B0169B56", 8, (Decimal("0.5"), Decimal("1.5"))),
    "HSK/USD": ("0x86CE42c1b714149Dc3A7b17169EF67b5F78A224b", 8, (Decimal("0.0001"), Decimal("1000"))),
}
SOURCE = "APRO price feed, HashKey Chain mainnet"
EXPLORER = "https://hsk.blockscout.com/address/"
_SEL = {s: "0x" + keccak(text=s)[:4].hex() for s in ("latestRoundData()", "decimals()", "description()")}


class FeedError(RuntimeError):
    def __init__(self, msg, transient=False):
        super().__init__(msg)
        self.transient = transient         # True: the chain could not be read (a cached reading stays usable)


def _call(rpc, to, sig):
    res = rpc("eth_call", [{"to": to, "data": _SEL[sig]}, "latest"], tries=1)
    if not isinstance(res, str) or not res.startswith("0x") or len(res) > 2 + 64 * 8:
        raise FeedError("unexpected eth_call answer")
    if len(res) == 2:
        raise FeedError("no data (no contract at the feed address?)")
    return bytes.fromhex(res[2:])


def check_meta(rpc, pair):
    """decimals() and description() of the proxy must match what HSK Meter pins for `pair`."""
    addr, dec, _ = FEEDS[pair]
    try:
        d = decode(["uint8"], _call(rpc, addr, "decimals()"))[0]
        desc = decode(["string"], _call(rpc, addr, "description()"))[0]
    except FeedError:
        raise
    except Exception as e:  # RpcError, malformed ABI data
        raise FeedError(f"{pair}: metadata unreadable ({type(e).__name__})", transient=True)
    if d != dec or desc != pair:
        raise FeedError(f"{pair}: feed reports {desc!r} with {d} decimals")


def parse_round(pair, raw, now=None):
    """Validate one latestRoundData() answer; returns the reading dict or raises FeedError."""
    addr, dec, (lo, hi) = FEEDS[pair]
    try:
        rid, answer, _started, updated, answered_in = decode(["uint80", "int256", "uint256", "uint256", "uint80"], raw)
    except Exception:
        raise FeedError(f"{pair}: malformed round data")
    now = int(time.time() if now is None else now)
    if rid == 0 or updated == 0 or answered_in < rid:
        raise FeedError(f"{pair}: round not finished")
    if answer <= 0:
        raise FeedError(f"{pair}: non-positive answer")
    if updated > now + FUTURE_SKEW_S:
        raise FeedError(f"{pair}: round from the future")
    age = max(0, now - updated)
    if age > MAX_AGE_S:
        raise FeedError(f"{pair}: round is {age // 3600} h old")
    price = Decimal(answer) / (Decimal(10) ** dec)
    if not (lo <= price <= hi):
        raise FeedError(f"{pair}: answer {price} outside the sane range")
    return {"pair": pair, "feed": addr, "answer": answer, "decimals": dec, "price": price, "roundId": rid,
            "updatedAt": updated, "ageSeconds": age}


def read(rpc, pair, now=None):
    try:
        raw = _call(rpc, FEEDS[pair][0], "latestRoundData()")
    except FeedError:
        raise
    except Exception as e:
        raise FeedError(f"{pair}: unreadable ({type(e).__name__})", transient=True)
    return parse_round(pair, raw, now)


def read_checked(rpc, pair, now=None):
    """check_meta + read: for one-off readers without a cache (the buyer's quote)."""
    check_meta(rpc, pair)
    return read(rpc, pair, now)


def public(r):
    """A reading as JSON for clients (strings for big numbers, ISO time)."""
    return {"pair": r["pair"], "feed": r["feed"], "price": f"{r['price']:f}", "decimals": r["decimals"],
            "roundId": str(r["roundId"]), "updatedAt": r["updatedAt"],
            "updatedAtIso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["updatedAt"])),
            "explorer": EXPLORER + r["feed"]}


def usd_of_units(units, reading):
    """USDC.e base units (6 decimals) at the feed's USDC/USD answer -> Decimal USD, rounded down to 6 decimals."""
    exact = Decimal(int(units)) * Decimal(reading["answer"]) / (Decimal(10) ** (6 + reading["decimals"]))
    return exact.quantize(Decimal("0.000001"), rounding="ROUND_DOWN")


class Feeds:
    """Cached readings for the seller. get() may read the chain (quote path, background refresh); cached() never does
    (settlement path). A reading is served while it is younger than TTL_S and its round is still current."""

    def __init__(self, rpc, ttl_s=TTL_S, clock=time.time, mono=time.monotonic):
        self.rpc, self.ttl, self.clock, self.mono = rpc, ttl_s, clock, mono
        self._cache = {}          # pair -> (reading, read_at_mono)
        self._failed = {}         # pair -> (reason, failed_at_mono)
        self._meta_ok = set()
        self._busy = set()
        self._lock = threading.Lock()

    def cached(self, pair):
        with self._lock:
            c = self._cache.get(pair)
        if not c:
            return None
        r, at = c
        if self.mono() - at > self.ttl * 5 or self.clock() - r["updatedAt"] > MAX_AGE_S:
            return None
        return r

    def get(self, pair):
        with self._lock:
            c = self._cache.get(pair)
            if c and self.mono() - c[1] <= self.ttl:
                return c[0]
            f = self._failed.get(pair)
            if f and self.mono() - f[1] < FAIL_BACKOFF_S:
                return self._usable(pair)
            if pair in self._busy:               # another thread is reading it: use what we have
                return self._usable(pair)
            self._busy.add(pair)
        try:
            if pair not in self._meta_ok:
                check_meta(self.rpc, pair)
                self._meta_ok.add(pair)
            r = read(self.rpc, pair, now=self.clock())
            with self._lock:
                self._cache[pair] = (r, self.mono())
                self._failed.pop(pair, None)
            return r
        except FeedError as e:
            with self._lock:
                self._failed[pair] = (str(e), self.mono())
                if not e.transient:
                    self._cache.pop(pair, None)    # a feed that now fails validation is never served from cache
                return self._usable(pair)
        finally:
            with self._lock:
                self._busy.discard(pair)

    def _usable(self, pair):
        c = self._cache.get(pair)
        if c and self.clock() - c[0]["updatedAt"] <= MAX_AGE_S:
            return c[0]
        return None

    def refresh(self):
        for pair in FEEDS:
            self.get(pair)

    def status(self):
        out = {}
        for pair in FEEDS:
            r = self.cached(pair)
            if r:
                out[pair] = public(r)
            else:
                with self._lock:
                    f = self._failed.get(pair)
                out[pair] = {"pair": pair, "feed": FEEDS[pair][0], "available": False,
                             "reason": f[0] if f else "not read yet"}
        return out
