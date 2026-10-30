"""Read-only chain checks (ERC-20 balanceOf / allowance) over PINNED testnet RPCs.

The RPC URL is chosen from config.TESTNET_RPC by chain id; a URL from the gateway is never used (SSRF). Only
eth_call is sent; nothing is ever signed or broadcast from here.
"""
import json
import re
import urllib.request
from urllib.parse import urlsplit

import config

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HEX_RE = re.compile(r"^0x[0-9a-fA-F]{0,64}$")


class ChainError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _eth_call(chain_id, to, data, timeout=15):
    url = config.TESTNET_RPC.get(int(chain_id))
    if not url:
        raise ChainError(f"no pinned RPC for chain {chain_id} (testnets only)")
    if urlsplit(url).scheme != "https":
        raise ChainError("pinned RPC must be https")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": config.USER_AGENT})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read(65536)
    except Exception as e:
        raise ChainError(f"RPC call failed: {type(e).__name__}") from None
    try:
        res = json.loads(raw).get("result")
    except (ValueError, AttributeError):
        raise ChainError("RPC returned invalid JSON") from None
    if not isinstance(res, str) or not HEX_RE.match(res) or res == "0x":
        raise ChainError("RPC returned no result (is the token deployed on this chain?)")
    return int(res, 16)


def _word(addr):
    if not ADDR_RE.match(addr or ""):
        raise ChainError("bad address")
    return addr[2:].lower().rjust(64, "0")


def balance_of(chain_id, token, owner):
    return _eth_call(chain_id, token, "0x70a08231" + _word(owner))


def allowance(chain_id, token, owner, spender):
    return _eth_call(chain_id, token, "0xdd62ed3e" + _word(owner) + _word(spender))
