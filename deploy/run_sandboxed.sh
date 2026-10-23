#!/bin/bash
# Run the HSK Meter seller (or the sandbox probe) inside a bubblewrap ALLOWLIST sandbox.
#
#   HSK_METER_KEY_DIR=/path/to/keys HSK_METER_VENV=/path/to/venv deploy/run_sandboxed.sh seller [seller.py args]
#   HSK_METER_KEY_DIR=... HSK_METER_VENV=... deploy/run_sandboxed.sh probe [--no-guard]
#
# Visible inside: /usr and /etc (read-only), the DNS stub resolv.conf, this repo's server/ and deploy/ directories and
# ops/state/deployment.json (read-only), the Python venv (read-only), the key directory (read-only: provider.env,
# adapter.env, token.env only), the state directory (read-write), private /tmp and /run. Not visible: the rest of
# $HOME (other secrets, browser profiles, other projects), /var, D-Bus and other sockets.
# The network namespace is shared (the seller must reach the RPC and accept local connections); outgoing connections
# are limited in-process by server/egress.py to the RPC host on port 443.
# Why bwrap and not systemd's ProtectHome=/IPAddressDeny=: per-user systemd managers on hosts that restrict
# unprivileged user namespaces silently skip those directives; bwrap enforces the allowlist itself.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD="$(dirname "$HERE")"
KEYS="${HSK_METER_KEY_DIR:?set HSK_METER_KEY_DIR}"
VENV="${HSK_METER_VENV:?set HSK_METER_VENV (a venv with eth-account, eth-abi, eth-utils)}"
STATE="${HSK_METER_STATE_DIR:-$PROD/run}"
STUB=/run/systemd/resolve/stub-resolv.conf
for f in provider.env adapter.env token.env; do
  [ -f "$KEYS/$f" ] || { echo "missing $KEYS/$f" >&2; exit 2; }
done
mkdir -p "$STATE"
chmod 700 "$STATE"
ARGS=(--unshare-user-try --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup-try
      --new-session --clearenv --cap-drop ALL
      --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/sbin /sbin
      --ro-bind /etc /etc
      --dev /dev --proc /proc --tmpfs /tmp --tmpfs /run --tmpfs /home)
[ -f "$STUB" ] && ARGS+=(--ro-bind "$STUB" "$STUB")
ARGS+=(--ro-bind "$PROD/server" "$PROD/server" --ro-bind "$PROD/deploy" "$PROD/deploy"
       --ro-bind "$PROD/ops/state/deployment.json" "$PROD/ops/state/deployment.json"
       --ro-bind "$VENV" "$VENV" --ro-bind "$KEYS" "$KEYS" --bind "$STATE" "$STATE"
       --chdir "$PROD"
       --setenv HOME /tmp --setenv TMPDIR /tmp --setenv PATH /usr/bin:/bin --setenv LANG C.UTF-8
       --setenv PYTHONUNBUFFERED 1 --setenv PYTHONDONTWRITEBYTECODE 1
       --setenv HSK_METER_KEY_DIR "$KEYS" --setenv HSK_METER_STATE_DIR "$STATE")
WHAT="${1:-}"; shift || true
case "$WHAT" in
  seller) SCRIPT="$PROD/server/seller.py" ;;
  probe)  SCRIPT="$PROD/deploy/sandbox_probe.py"
          ARGS+=(--setenv HSK_METER_PROBE_FILES "${HSK_METER_PROBE_FILES:-}" --setenv HSK_METER_PROBE_DIRS "${HSK_METER_PROBE_DIRS:-}") ;;
  *) echo "usage: run_sandboxed.sh seller|probe [args]" >&2; exit 2 ;;
esac
exec /usr/bin/bwrap "${ARGS[@]}" "$VENV/bin/python" -I "$SCRIPT" "$@"
