"""Otari gateway client for HSK Meter's paid AI endpoint (Swift Bot Labs).

Otari (otari.ai, by Mozilla.ai) is an OpenAI-compatible gateway in front of many model providers. HSK Meter sends
every model call through it, so routing and fallback, spend limits, per-key model allow-lists and usage records live
in the gateway and not in this code:
  - model names are Otari routing policies (hsk-guard, hsk-route, hsk-answer, hsk-verify): each one picks a model and
    falls over to the next provider when the first fails; the response headers say who answered
    (Otari-Provider, Otari-Model, Otari-Fallback, Otari-Attempt-Count) and the body carries usage.cost_usd;
  - every call is made with an Otari API key whose spend ceiling and allowed_models are set in the gateway; a key
    whose ceiling is used up is refused by Otari with 403 budget_exceeded, which the seller turns into an on-chain
    refund;
  - non-streaming calls carry an Idempotency-Key derived from the on-chain requestId, so re-serving a paid request
    returns the stored answer without calling (or billing) a provider again;
  - metadata tags (buyer, endpoint, request id prefix) make every call filterable in Otari's usage records.

Keys: one Otari API key per purpose, each in its own file holding exactly ONE variable, OTARI_API_KEY, mode 0600:
  <key-dir>/screen.env                     pre-payment screening (guard + router models only)
  <key-dir>/pool.env                       answers for buyers without a key of their own (one shared ceiling)
  <key-dir>/buyers/0x<40 hex>.env          a buyer's own key: its ceiling is that buyer's AI budget
Keys are read once at start, never logged, printed, or put in an exception message or a response.
Only https://otari.ai and https://api.otari.ai are allowed as the gateway (or http on loopback for tests); redirects
are refused and response bodies are capped.
"""
import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "swiftbotlab-hsk-meter-otari/1.0"
ALLOWED_HOSTS = {"otari.ai", "api.otari.ai"}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
DEFAULT_BASE = "https://otari.ai/api/v1"
MAX_BODY = 1_000_000
KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{16,200}$")
ADDR_FILE_RE = re.compile(r"^(0x[0-9a-f]{40})\.env$")
HDRS = ("otari-request-id", "otari-provider", "otari-model", "otari-attempt-count", "otari-fallback",
        "otari-response-duration-ms", "otari-error-code", "otari-guardrails", "retry-after")


class OtariError(RuntimeError):
    """A refused or failed gateway call. `kind` is one of: budget, rate_limited, guardrail, forbidden, auth,
    rejected, unavailable, bad_response. The message never contains a key."""

    def __init__(self, kind, status=None, code=None, detail=None, request_id=None, retry_after=None):
        self.kind, self.status, self.code = kind, status, code
        self.detail = (str(detail) if detail is not None else "")[:200]
        self.request_id, self.retry_after = request_id, retry_after
        super().__init__(f"otari {kind} (HTTP {status}, {code or '-'}): {self.detail}")


class KeyFileError(RuntimeError):
    pass


def _read_one(path, var="OTARI_API_KEY"):
    """The value of `var` from a file that holds only that variable: regular file, not a symlink, mode 0600."""
    try:
        st = os.lstat(path)
    except OSError:
        raise KeyFileError(f"key file missing: {os.path.basename(path)}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise KeyFileError(f"key file must be a regular file: {os.path.basename(path)}")
    if st.st_mode & 0o077:
        raise KeyFileError(f"key file must be mode 0600: {os.path.basename(path)}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        raw = f.read(4096)
    vals = {}
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    if set(vals) != {var}:
        raise KeyFileError(f"{os.path.basename(path)} must hold exactly one variable, {var}")
    if not KEY_RE.match(vals[var]):
        raise KeyFileError(f"{os.path.basename(path)}: {var} does not look like an Otari key")
    return vals[var]


class KeyRing:
    """Which Otari key pays for what. `screen` and `pool` are required; buyers/ holds optional per-buyer keys."""

    def __init__(self, key_dir=None, *, screen=None, pool=None, buyers=None):
        if key_dir is not None:
            screen = _read_one(os.path.join(key_dir, "screen.env"))
            pool = _read_one(os.path.join(key_dir, "pool.env"))
            buyers = {}
            bdir = os.path.join(key_dir, "buyers")
            if os.path.isdir(bdir) and not os.path.islink(bdir):
                for name in sorted(os.listdir(bdir))[:1000]:
                    m = ADDR_FILE_RE.match(name)
                    if m:
                        buyers[m.group(1)] = _read_one(os.path.join(bdir, name))
        if not screen or not pool:
            raise KeyFileError("screen and pool keys are required")
        self._screen, self._pool, self._buyers = screen, pool, dict(buyers or {})

    def screen(self):
        return self._screen

    def for_buyer(self, payer):
        """(key, tier): the buyer's own key when it has one, else the shared pool key."""
        k = self._buyers.get((payer or "").lower())
        return (k, "dedicated") if k else (self._pool, "pool")

    def has_buyer(self, payer):
        return (payer or "").lower() in self._buyers

    def __repr__(self):     # never the keys themselves
        return f"KeyRing(buyers={len(self._buyers)})"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise urllib.error.HTTPError(a[1] if len(a) > 1 else "", 399, "redirects refused", None, None)


def _classify(status, code, detail):
    code = (code or "").lower()
    if "budget" in code or status == 402:
        return "budget"
    if "guardrail" in code:
        return "guardrail"
    if status == 429:
        return "rate_limited"
    if status == 401:
        return "auth"
    if status == 403:
        return "forbidden"
    if status in (400, 404, 409, 413, 422):
        return "rejected"
    return "unavailable"


class Otari:
    """Minimal Chat Completions client. `opener` is injectable (tests); default refuses redirects."""

    def __init__(self, base=DEFAULT_BASE, timeout=45, allow_local=False, opener=None, max_body=MAX_BODY):
        u = urllib.parse.urlparse(base)
        remote_ok = u.scheme == "https" and u.hostname in ALLOWED_HOSTS and not u.username and not u.port
        local_ok = allow_local and u.scheme == "http" and u.hostname in LOCAL_HOSTS
        if not (remote_ok or local_ok) or not u.path.rstrip("/").endswith("/api/v1"):
            raise ValueError(f"Otari base URL not allowed: {u.scheme}://{u.hostname}{u.path}")
        self.base = base.rstrip("/")
        self.host, self.port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        self.timeout, self.max_body = timeout, max_body
        self.opener = opener or urllib.request.build_opener(_NoRedirect)
        self.lock = threading.Lock()
        self.calls = 0

    def chat(self, key, body, *, idempotency_key=None, timeout=None):
        """POST /chat/completions. Returns a dict: content, model, provider, servedModel, fallback, attempts,
        requestId, costUsd, usage, toolCalls (Otari's own tool reports), raw message. Raises OtariError."""
        data = json.dumps(body, separators=(",", ":")).encode()
        if len(data) > 200_000:
            raise OtariError("rejected", detail="request body too large")
        h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA,
             "Authorization": "Bearer " + key}
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(self.base + "/chat/completions", data=data, method="POST", headers=h)
        with self.lock:
            self.calls += 1
        t0 = time.monotonic()
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as r:
                status, hdr, raw = r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(self.max_body + 1)
        except urllib.error.HTTPError as e:
            hdr = {k.lower(): v for k, v in (e.headers or {}).items()}
            try:
                raw = e.read(64_000) if e.fp else b""
            except Exception:
                raw = b""
            finally:
                e.close()
            try:
                j = json.loads(raw or b"{}")
            except ValueError:
                j = {}
            code = hdr.get("otari-error-code") or (j.get("code") if isinstance(j, dict) else None)
            detail = j.get("detail") if isinstance(j, dict) else None
            if not isinstance(detail, str):
                detail = json.dumps(detail)[:200] if detail is not None else ""
            raise OtariError(_classify(e.code, code, detail), e.code, code, detail, hdr.get("otari-request-id"),
                             hdr.get("retry-after"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OtariError("unavailable", detail=type(e).__name__)
        if len(raw) > self.max_body:
            raise OtariError("bad_response", status, detail="response too large")
        try:
            j = json.loads(raw)
            msg = j["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise OtariError("bad_response", status, detail="not a chat completion", request_id=hdr.get("otari-request-id"))
        usage = j.get("usage") if isinstance(j.get("usage"), dict) else {}
        try:
            cost = float(usage.get("cost_usd") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        content = msg.get("content") if isinstance(msg, dict) else None
        tool_calls = []
        for t in (j.get("otari_tool_calls") or [])[:10]:
            if isinstance(t, dict):
                tool_calls.append({"type": str(t.get("type", ""))[:40], "status": str(t.get("status", ""))[:20],
                                   "query": str(t.get("query", ""))[:200] if t.get("query") else None,
                                   "sources": [str(s.get("url", ""))[:300] for s in (t.get("sources") or [])[:5]
                                               if isinstance(s, dict)]})
        return {"content": content if isinstance(content, str) else "", "model": j.get("model"),
                "provider": hdr.get("otari-provider"), "servedModel": hdr.get("otari-model"),
                "fallback": hdr.get("otari-fallback") == "true",
                "attempts": int(hdr["otari-attempt-count"]) if str(hdr.get("otari-attempt-count", "")).isdigit() else None,
                "requestId": hdr.get("otari-request-id"), "costUsd": cost,
                "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                "toolCalls": tool_calls, "ms": int((time.monotonic() - t0) * 1000)}
