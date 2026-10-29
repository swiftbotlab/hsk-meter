#!/usr/bin/env python3
"""Run a test script with every non-loopback network call refused (Swift Bot Labs, HSK Meter CI).

An audit hook (it cannot be removed once installed) refuses, in this process:
  - socket connect / sendto / sendmsg to any address that is not 127.0.0.0/8 or ::1 (unix sockets are allowed),
  - name lookups (getaddrinfo, gethostbyname, ...) for anything but localhost and literal loopback addresses.
So a test that tried to reach HashKey Chain mainnet, a gateway or any other host fails instead of calling out.

  ci_offline.py SCRIPT [ARGS...]     run SCRIPT as __main__ under the guard
  ci_offline.py --selftest           positive + negative control: loopback works, the outside world is refused
"""
import ipaddress
import os
import runpy
import socket
import sys
import threading

LOOPBACK_NAMES = {"localhost", "localhost.", "ip6-localhost"}


def _loopback_ip(host):
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _host_ok(host):
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    return str(host).lower() in LOOPBACK_NAMES or _loopback_ip(host)


def _hook(event, args):
    if event == "socket.connect" or event == "socket.sendto":
        addr = args[1]
        if isinstance(addr, tuple) and addr and not _loopback_ip(addr[0]):
            raise PermissionError(f"ci_offline: network access to {addr[0]!r} refused (CI runs offline)")
    elif event == "socket.sendmsg":
        addr = args[1] if len(args) > 1 else None
        if isinstance(addr, tuple) and addr and not _loopback_ip(addr[0]):
            raise PermissionError(f"ci_offline: network access to {addr[0]!r} refused (CI runs offline)")
    elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr",
                   "socket.getnameinfo"):
        host = args[0] if args else None
        if event == "socket.getnameinfo" and isinstance(host, tuple):
            host = host[0]
        if not _host_ok(host):
            raise PermissionError(f"ci_offline: name lookup of {str(host)[:80]!r} refused (CI runs offline)")


def install():
    sys.addaudithook(_hook)


def selftest():
    install()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept()[0].close(), daemon=True).start()
    socket.create_connection(("127.0.0.1", port), timeout=3).close()          # positive control: loopback works
    socket.getaddrinfo("localhost", port)
    refused = []
    for what, f in (("connect 1.1.1.1:443", lambda: socket.create_connection(("1.1.1.1", 443), timeout=3)),
                    ("lookup mainnet.hsk.xyz", lambda: socket.getaddrinfo("mainnet.hsk.xyz", 443)),
                    ("udp 8.8.8.8:53", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("8.8.8.8", 53))),
                    ("urllib https", lambda: __import__("urllib.request").request.urlopen("https://mainnet.hsk.xyz", timeout=3))):
        try:
            f()
        except PermissionError:
            refused.append(what)
        except Exception as e:          # e.g. urllib wraps the error: still refused if the cause is ours
            if "ci_offline" in repr(e) or isinstance(getattr(e, "reason", None), PermissionError):
                refused.append(what)
            else:
                print(f"selftest: {what} raised {type(e).__name__}, not the guard")
    print({"loopback": "ok", "refused": refused})
    return 0 if len(refused) == 4 else 1


def main(argv):
    if argv[:1] == ["--selftest"]:
        return selftest()
    if not argv:
        print(__doc__)
        return 2
    script = os.path.abspath(argv[0])
    sys.argv = [script, *argv[1:]]
    sys.path[0] = os.path.dirname(script)
    install()
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
