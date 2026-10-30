"""Dedicated QA wallets (testnet only).

create(): generates a key in-process from os.urandom(32) and writes it with O_CREAT|O_EXCL at mode 600 into the
secrets dir. It never overwrites an existing file, and never prints, logs or passes the key as an argument; only the
address leaves this module. load_payer() returns an eth_account LocalAccount; load_address() reads only the address.
"""
import os
import re
import stat

import config

KEY_RE = re.compile(r"^0x[0-9a-f]{64}$")
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class WalletError(Exception):
    pass


def _path(name):
    return os.path.join(config.SECRETS_DIR, name)


def _account_cls():
    from eth_account import Account
    return Account


def create(name, label):
    """Create secrets/<name> if absent. Returns (address, created: bool)."""
    path = _path(name)
    if os.path.lexists(path):
        return load_address(name), False
    os.makedirs(config.SECRETS_DIR, mode=0o700, exist_ok=True)
    Account = _account_cls()
    key = os.urandom(32)
    acct = Account.from_key(key)
    body = (f"# {label}: dedicated HSP QA wallet, TESTNET ONLY. Never send mainnet funds here.\n"
            f"HSP_QA_ADDRESS={acct.address}\n"
            f"HSP_QA_KEY=0x{key.hex()}\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, body.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    del key, body
    return acct.address, True


def _read(name):
    path = _path(name)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise WalletError(f"{name} does not exist; run `pay.py wallets --create`") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise WalletError(f"{name} is not a regular file")
    if st.st_mode & 0o077:
        raise WalletError(f"{name} must be mode 600")
    if st.st_uid != os.getuid():
        raise WalletError(f"{name} is not owned by us")
    if st.st_size > 4096:
        raise WalletError(f"{name} is too large")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        text = os.read(fd, 4096).decode()
    finally:
        os.close(fd)
    vals = {}
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip()
    return vals


def load_address(name):
    addr = _read(name).get("HSP_QA_ADDRESS", "")
    if not ADDR_RE.match(addr):
        raise WalletError(f"{name} has no valid address")
    return addr


def load_account(name):
    vals = _read(name)
    key = vals.get("HSP_QA_KEY", "")
    if not KEY_RE.match(key):
        raise WalletError(f"{name} has no valid key")
    acct = _account_cls().from_key(key)
    if acct.address != vals.get("HSP_QA_ADDRESS"):
        raise WalletError(f"{name}: key does not match the stored address")
    return acct


def load_payer():
    return load_account(config.PAYER_ENV)


def payer_address():
    return load_address(config.PAYER_ENV)


def merchant_address():
    return load_address(config.MERCHANT_ENV)
