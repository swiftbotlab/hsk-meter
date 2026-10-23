"""In-process egress guard for the seller (Swift Bot Labs).

An audit hook refuses every outgoing socket connect/send except to the current IP addresses of an explicit
(host, port) allowlist: on mainnet only mainnet.hsk.xyz:443. Loopback (e.g. a browser's DevTools port), the LAN,
link-local, unix sockets and every other public host are refused, whatever the code that asks. DNS lookups go
through the C library (not Python sockets) and are not affected. Accepting connections on the listening socket is
not an egress and is not affected either.

Audit hooks cannot be removed once installed, so install() is called once, at startup, before any request.
"""
import ipaddress
import socket
import sys
import threading
import time

_installed = []


class EgressGuard:
    def __init__(self, allow, refresh_s=30):
        """allow: iterable of (host, port); host is a DNS name or a literal IP address."""
        self.allow = [(str(h).lower(), int(p)) for h, p in allow]
        if not self.allow:
            raise ValueError("empty egress allowlist")
        self.refresh_s = refresh_s
        self.ips = {}                 # host -> set of ip strings
        self.last = 0.0
        self.lock = threading.Lock()
        self.refresh(force=True)

    def _resolve(self, host):
        try:
            return {str(ipaddress.ip_address(host))}
        except ValueError:
            pass
        out = set()
        try:
            for fam, _, _, _, sa in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP):
                out.add(str(ipaddress.ip_address(sa[0].split("%", 1)[0])))
        except OSError:
            pass
        return out

    def refresh(self, force=False):
        with self.lock:
            if not force and time.monotonic() - self.last < self.refresh_s:
                return False
            self.last = time.monotonic()
        fresh = {h: self._resolve(h) for h, _ in self.allow}
        with self.lock:
            for h, ips in fresh.items():
                if ips:
                    self.ips[h] = ips
        return True

    def _match(self, ip, port):
        with self.lock:
            return any(port == p and ip in self.ips.get(h, ()) for h, p in self.allow)

    def refusal(self, address):
        """None when `address` may be connected to, else the reason."""
        if address is None:
            return None
        if isinstance(address, (str, bytes, bytearray)):
            return "unix-socket connections are not allowed"
        if not isinstance(address, tuple) or len(address) < 2:
            return "unexpected address form"
        try:
            ip = ipaddress.ip_address(str(address[0]).split("%", 1)[0])
        except ValueError:
            return "connect by host name is not allowed"
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        try:
            port = int(address[1])
        except (TypeError, ValueError):
            return "bad port"
        ip = str(ip)
        if self._match(ip, port):
            return None
        if self.refresh() and self._match(ip, port):     # the allowed host may have moved to a new address
            return None
        return f"{ip}:{port} is not on the egress allowlist"

    def hook(self, event, args):
        if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            why = self.refusal(args[1] if len(args) > 1 else None)
            if why:
                raise PermissionError("egress guard: " + why)


def install(allow):
    """Install the guard for this process (once). Returns the guard."""
    if _installed:
        return _installed[0]
    g = EgressGuard(allow)
    sys.addaudithook(g.hook)
    _installed.append(g)
    return g
