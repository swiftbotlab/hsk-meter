#!/usr/bin/env python3
"""Write docs/snapshot.json: the settlement contract's event logs from the public Blockscout API v2, trimmed to the
fields the dashboard reads. The dashboard falls back to this file only when the explorer API is unreachable.

  python3 tools/dashboard_snapshot.py [--out docs/snapshot.json]
"""
import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request

API = "https://hsk.blockscout.com/api/v2"
CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"
MAX_PAGES = 40
WORD = re.compile(r"^0x[0-9a-f]{64}$")
DATA = re.compile(r"^0x(?:[0-9a-f]{64}){0,16}$")


def get(url):
    req = urllib.request.Request(url, headers={"user-agent": "hsk-meter-snapshot/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return json.loads(r.read(5_000_001)[:5_000_000])


def clean(it):
    addr = (it.get("address") or {}).get("hash", "") if isinstance(it.get("address"), dict) else ""
    if addr.lower() != CONTRACT:
        return None
    tx = str(it.get("transaction_hash", "")).lower()
    topics = [str(t).lower() for t in (it.get("topics") or []) if t is not None]
    data = str(it.get("data", "")).lower()
    if not WORD.match(tx) or not (1 <= len(topics) <= 4) or not all(WORD.match(t) for t in topics) or not DATA.match(data):
        return None
    return {"address": {"hash": CONTRACT}, "transaction_hash": tx, "topics": topics, "data": data,
            "block_number": int(it["block_number"]), "index": int(it["index"]), "block_timestamp": str(it["block_timestamp"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "snapshot.json"))
    args = ap.parse_args()
    items, params = [], None
    for _ in range(MAX_PAGES):
        url = f"{API}/addresses/{CONTRACT}/logs" + ("?" + urllib.parse.urlencode(params) if params else "")
        body = get(url)
        for it in body.get("items", []):
            c = clean(it)
            if c:
                items.append(c)
        params = body.get("next_page_params")
        if not params:
            break
        time.sleep(0.5)
    out = {"generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "source": f"{API}/addresses/{CONTRACT}/logs",
           "items": items}
    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, args.out)
    print(json.dumps({"out": args.out, "items": len(items), "generatedAt": out["generatedAt"]}))


if __name__ == "__main__":
    main()
