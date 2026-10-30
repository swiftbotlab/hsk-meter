"""Minimal MCP client over streamable HTTP (JSON-RPC 2.0), stdlib only.

Supports initialize -> notifications/initialized -> tools/list -> tools/call, JSON and SSE responses and the
Mcp-Session-Id header. Hardened for an untrusted server:
  * host pinned to the official HSP QA host over HTTPS (or loopback over HTTP for the local mock, opt-in);
  * no redirects, no proxies (http.client does not read proxy env vars);
  * per-read socket timeout plus an overall deadline per request (slow-trickle safe);
  * response body cap (JSON and SSE alike), header sanity checks, JSON depth errors caught.
Every server response is untrusted data: it is parsed, never executed or used as instructions.
"""
import http.client
import json
import re
import ssl
import time
from urllib.parse import urlsplit

import config

PROTOCOL_VERSION = "2025-06-18"
_SESSION_RE = re.compile(r"^[\x21-\x7e]{1,256}$")
_PROTO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class McpError(Exception):
    pass


class McpHttpError(McpError):
    def __init__(self, status, snippet):
        super().__init__(f"HTTP {status}: {snippet}")
        self.status = status
        self.snippet = snippet


class McpBlocked(McpHttpError):
    """403: the IP is not allowlisted. Callers must stop, not retry."""


class McpTimeout(McpError):
    pass


class McpProtocolError(McpError):
    pass


class McpRpcError(McpError):
    def __init__(self, code, message, data=None):
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class McpToolError(McpError):
    """tools/call returned isError: true. .payload is the parsed content (dict) or {"text": ...}."""

    def __init__(self, payload):
        super().__init__(f"tool error: {_snip(json.dumps(payload, default=str))}")
        self.payload = payload


def _snip(s, n=300):
    s = s if isinstance(s, str) else repr(s)
    s = "".join(ch if ch.isprintable() else "?" for ch in s)
    return s[:n] + ("..." if len(s) > n else "")


def check_url(url, allow_loopback=False):
    """Return (scheme, host, port, path) or raise McpError. Only the pinned HTTPS host, or loopback HTTP if allowed."""
    if not isinstance(url, str) or len(url) > 512:
        raise McpError("bad URL")
    u = urlsplit(url)
    if u.username or u.password or u.query or u.fragment:
        raise McpError("URL must not carry credentials, query or fragment")
    host = (u.hostname or "").lower()
    path = u.path or "/"
    if u.scheme == "https" and host == config.MCP_HOST and u.port in (None, 443):
        return "https", host, 443, path
    if allow_loopback and u.scheme == "http" and host in config.LOOPBACK_HOSTS and u.port:
        return "http", host, u.port, path
    raise McpError(f"refusing host {host!r} (scheme {u.scheme!r}); only {config.MCP_HOST} over https"
                   + (" or loopback http" if allow_loopback else ""))


def _loads(raw):
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as e:
        raise McpProtocolError(f"invalid JSON from server: {type(e).__name__}") from None


class _Reader:
    """Reads a response body under a byte cap and a wall-clock deadline."""

    def __init__(self, conn, resp, deadline, max_bytes):
        self.conn, self.resp, self.deadline, self.max_bytes = conn, resp, deadline, max_bytes
        self.total = 0

    def chunk(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise McpTimeout("response deadline exceeded")
        if self.conn.sock is not None:
            self.conn.sock.settimeout(max(0.05, min(remaining, config.HTTP_TIMEOUT_S)))
        try:
            data = self.resp.read1(16384)
        except (TimeoutError, OSError) as e:  # socket.timeout is TimeoutError
            if isinstance(e, TimeoutError) or "timed out" in str(e):
                raise McpTimeout("read timed out") from None
            raise McpError(f"read failed: {type(e).__name__}") from None
        self.total += len(data)
        if self.total > self.max_bytes:
            raise McpProtocolError(f"response larger than {self.max_bytes} bytes")
        return data

    def read_all(self):
        parts = []
        while True:
            d = self.chunk()
            if not d:
                return b"".join(parts)
            parts.append(d)


class McpClient:
    def __init__(self, url=config.MCP_URL, *, allow_loopback=False, timeout=config.HTTP_TIMEOUT_S,
                 total_timeout=config.HTTP_TOTAL_TIMEOUT_S, max_bytes=config.MAX_RESPONSE_BYTES,
                 ssl_context=None):
        self.scheme, self.host, self.port, self.path = check_url(url, allow_loopback)
        self.timeout = float(timeout)
        self.total_timeout = float(total_timeout)
        self.max_bytes = int(max_bytes)
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.session_id = None
        self.protocol_version = None
        self.server_info = None
        self._next_id = 1
        self.initialized = False

    # ---- transport ----
    def _conn(self, timeout):
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=self.ssl_context)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "User-Agent": config.USER_AGENT}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    def _post(self, message, *, want_id=None, total_timeout=None):
        """POST one JSON-RPC message. Returns (headers dict lowercased, response message or None)."""
        body = json.dumps(message, separators=(",", ":")).encode()
        deadline = time.monotonic() + float(total_timeout or self.total_timeout)
        conn = self._conn(min(self.timeout, float(total_timeout or self.total_timeout)))
        try:
            try:
                conn.request("POST", self.path, body=body, headers=self._headers())
                resp = conn.getresponse()
            except (TimeoutError,) as e:
                raise McpTimeout(f"no response: {type(e).__name__}") from None
            except (OSError, http.client.HTTPException) as e:
                if "timed out" in str(e):
                    raise McpTimeout("no response: timed out") from None
                raise McpError(f"request failed: {type(e).__name__}: {_snip(str(e), 120)}") from None
            headers = {k.lower(): v for k, v in resp.getheaders()}
            reader = _Reader(conn, resp, deadline, self.max_bytes)
            status = resp.status
            if status == 403:
                raise McpBlocked(status, _snip(reader.read_all().decode("utf-8", "replace"), 200))
            if 300 <= status < 400:
                raise McpHttpError(status, "redirect refused")
            if want_id is None:
                if status in (200, 202, 204):
                    return headers, None
                raise McpHttpError(status, _snip(reader.read_all().decode("utf-8", "replace"), 200))
            if status != 200:
                raise McpHttpError(status, _snip(reader.read_all().decode("utf-8", "replace"), 300))
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype == "application/json":
                msg = _loads(reader.read_all())
                return headers, self._pick(msg, want_id)
            if ctype == "text/event-stream":
                return headers, self._read_sse(reader, want_id)
            raise McpProtocolError(f"unexpected content-type {_snip(ctype, 60)!r}")
        finally:
            conn.close()

    @staticmethod
    def _pick(msg, want_id):
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            if isinstance(m, dict) and m.get("id") == want_id and ("result" in m or "error" in m):
                return m
        raise McpProtocolError("no JSON-RPC response with our id")

    def _read_sse(self, reader, want_id):
        buf = b""
        data_lines = []
        while True:
            chunk = reader.chunk()
            if not chunk:
                raise McpProtocolError("SSE stream ended without our response")
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    if len(buf) > 1_000_000:
                        raise McpProtocolError("SSE line too long")
                    break
                line, buf = buf[:nl].rstrip(b"\r"), buf[nl + 1:]
                if not line:
                    if data_lines:
                        m = _loads(b"\n".join(data_lines))
                        data_lines = []
                        for one in (m if isinstance(m, list) else [m]):
                            if isinstance(one, dict) and one.get("id") == want_id and (
                                    "result" in one or "error" in one):
                                return one
                        # server notifications/requests (progress, logging) are ignored
                    continue
                if line.startswith(b":"):
                    continue
                field, _, value = line.partition(b":")
                if value.startswith(b" "):
                    value = value[1:]
                if field == b"data":
                    data_lines.append(value)

    def _rpc(self, method, params=None, total_timeout=None):
        rid = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        headers, resp = self._post(msg, want_id=rid, total_timeout=total_timeout)
        if "error" in resp:
            err = resp["error"] if isinstance(resp["error"], dict) else {}
            raise McpRpcError(err.get("code"), _snip(str(err.get("message")), 300), err.get("data"))
        return headers, resp.get("result")

    # ---- MCP methods ----
    def initialize(self):
        headers, result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "hsk-meter-hsp-qa", "version": "1.0"}})
        sid = headers.get("mcp-session-id")
        if sid is not None:
            if not _SESSION_RE.match(sid):
                raise McpProtocolError("malformed Mcp-Session-Id")
            self.session_id = sid
        if not isinstance(result, dict):
            raise McpProtocolError("initialize result is not an object")
        pv = result.get("protocolVersion")
        self.protocol_version = pv if isinstance(pv, str) and _PROTO_RE.match(pv) else PROTOCOL_VERSION
        si = result.get("serverInfo")
        self.server_info = {k: _snip(str(si.get(k)), 80) for k in ("name", "version") if k in si} \
            if isinstance(si, dict) else None
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.initialized = True
        return result

    def list_tools(self, max_pages=10):
        if not self.initialized:
            self.initialize()
        tools, cursor = [], None
        for _ in range(max_pages):
            _, result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise McpProtocolError("tools/list result has no tools array")
            for t in result["tools"]:
                if isinstance(t, dict) and isinstance(t.get("name"), str):
                    tools.append(t)
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or len(cursor) > 1024:
                raise McpProtocolError("bad nextCursor")
        if len(tools) > 200:
            raise McpProtocolError("too many tools")
        return tools

    def call_tool(self, name, arguments, total_timeout=None):
        """Return the tool's payload: structuredContent (dict) if present, else the JSON-parsed text content,
        else {"text": text}. Raises McpToolError when isError is true."""
        if not self.initialized:
            self.initialize()
        _, result = self._rpc("tools/call", {"name": name, "arguments": arguments}, total_timeout=total_timeout)
        if not isinstance(result, dict):
            raise McpProtocolError("tools/call result is not an object")
        payload = tool_payload(result)
        if result.get("isError") is True:
            raise McpToolError(payload)
        return payload

    def close(self):
        """Best effort: end the session (DELETE). Errors are ignored."""
        if not self.session_id:
            return
        try:
            conn = self._conn(5)
            conn.request("DELETE", self.path, headers=self._headers())
            conn.getresponse().read(1024)
            conn.close()
        except Exception:
            pass
        self.session_id = None


def tool_payload(result):
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        return sc
    texts = [c.get("text") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"
             and isinstance(c.get("text"), str)]
    text = "".join(texts)
    if text:
        try:
            v = json.loads(text)
            return v if isinstance(v, dict) else {"value": v}
        except (ValueError, RecursionError):
            return {"text": _snip(text, 4000)}
    return {}


def health(url=config.HEALTH_URL, timeout=10.0, allow_loopback=False):
    """GET /health once. Returns (status, body_snippet). Never retries."""
    scheme, host, port, path = check_url(url, allow_loopback)
    conn = (http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
            if scheme == "https" else http.client.HTTPConnection(host, port, timeout=timeout))
    try:
        conn.request("GET", path, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read(4096)
        return resp.status, _snip(body.decode("utf-8", "replace"), 200)
    finally:
        conn.close()
