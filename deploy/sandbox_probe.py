#!/usr/bin/env python3
"""What can the seller's process reach? Run it inside the sandbox (deploy/run_sandboxed.sh probe) and, as the
positive control, outside it (`python3 deploy/sandbox_probe.py --no-guard --outside`). Prints one line per probe:
OK / BLOCKED with the reason. Never prints file contents (sizes and variable NAMES only).

  sandbox_probe.py [--no-guard] [--outside]
  env: HSK_METER_KEY_DIR, HSK_METER_STATE_DIR, and optionally HSK_METER_PROBE_FILES / HSK_METER_PROBE_DIRS (comma
  lists of other secret files / private directories on this host that the seller must NOT reach)
"""
import os
import socket
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))
import chain as C   # noqa: E402

GUARD = "--no-guard" not in sys.argv
PROD = os.path.dirname(HERE)
KEYS = os.environ["HSK_METER_KEY_DIR"]
STATE = os.environ.get("HSK_METER_STATE_DIR", os.path.join(PROD, "run"))
SECRETS = os.path.dirname(os.path.abspath(KEYS))          # the directory that holds the key directory
FILES = [f for f in os.environ.get("HSK_METER_PROBE_FILES", "").split(",") if f]
DIRS = [os.path.dirname(SECRETS), os.path.dirname(PROD)] + [d for d in os.environ.get("HSK_METER_PROBE_DIRS", "").split(",") if d]
if GUARD:
    import egress
    egress.install([("mainnet.hsk.xyz", 443)])

# a local listener: the positive control for the loopback rule (reachable without the guard, refused with it)
lst = socket.socket()
lst.bind(("127.0.0.1", 0))
lst.listen(4)
LPORT = lst.getsockname()[1]


def t(name, f):
    try:
        print(f"{name:52s} -> OK: {f()}", flush=True)
    except Exception as e:
        print(f"{name:52s} -> BLOCKED: {type(e).__name__}: {str(e)[:80]}", flush=True)


def names(path):
    with open(path) as f:
        return sorted(line.split("=", 1)[0] for line in f if "=" in line and not line.startswith("#"))


print(f"# egress guard: {'on' if GUARD else 'off'}; uid={os.getuid()}; pid={os.getpid()}", flush=True)
t("RPC https://mainnet.hsk.xyz eth_chainId", lambda: C.Rpc("https://mainnet.hsk.xyz")("eth_chainId", []))
t("other public host https://example.com", lambda: urllib.request.urlopen("https://example.com", timeout=8).status)
t("loopback listener 127.0.0.1:%d (control)" % LPORT, lambda: socket.create_connection(("127.0.0.1", LPORT), timeout=3) and "CONNECTED")
t("loopback 127.0.0.1:9222 (browser DevTools)", lambda: socket.create_connection(("127.0.0.1", 9222), timeout=3) and "CONNECTED")
t("LAN 192.168.1.1:80", lambda: socket.create_connection(("192.168.1.1", 80), timeout=3) and "CONNECTED")
t("UDP 8.8.8.8:53 sendto", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("8.8.8.8", 53)))


def listing(d):
    n = os.listdir(d)
    return sorted(n) if len(n) < 4 else f"{len(n)} entries"


def reachable(path):        # stat + access only: the probe never reads another secret's contents
    st = os.stat(path)
    if not os.access(path, os.R_OK):
        raise PermissionError("not readable")
    return "readable, %d bytes" % st.st_size


t("list the key dir's parent", lambda: listing(SECRETS))
for f in FILES:
    t("other secret " + os.path.basename(f), lambda f=f: reachable(f))
t("read key dir provider.env (needed)", lambda: names(os.path.join(KEYS, "provider.env")))
t("read key dir adapter.env (needed)", lambda: names(os.path.join(KEYS, "adapter.env")))
if "--outside" in sys.argv:
    t("key dir writable", lambda: "WRITABLE" if os.access(KEYS, os.W_OK) else (_ for _ in ()).throw(PermissionError("read-only")))
else:
    t("write key dir", lambda: open(os.path.join(KEYS, ".probe"), "w").write("x") and "WROTE")
t("write server/ (code)", lambda: open(os.path.join(os.path.dirname(HERE), "server", ".probe"), "w").write("x") and "WROTE")
t("write state dir (needed)", lambda: open(os.path.join(STATE, ".probe"), "w").write("x") and "WROTE")
for d in DIRS:
    t("list " + d, lambda d=d: listing(d))
for p in (os.path.join(os.path.dirname(HERE), "server", ".probe"), os.path.join(STATE, ".probe")):
    try:
        os.remove(p)
    except OSError:
        pass
