#!/usr/bin/env python3
"""Write docs/snapshot.json, the dashboard's base: every event it counts, from the contract's deploy block up to a
fixed recent block. The logs come from the explorer's getLogs API with exact topic filters that only payments to
endpoints 1-2 or our own provider and payee keys can match (the same filters as EXACT_QUERIES in docs/app.js), so
nobody else can push our events out. The page adds newer events live and shows this file alone if the explorer is down.

Nothing is written unless every check passes (exit status 1 otherwise, the old file stays):
  - every query ended below its page cap (pages left over mean an incomplete snapshot);
  - endpoints 1 and 2 are the pinned registrations (provider 0x30CC..., payee 0xA8A4...);
  - counts and sums of the logs equal the contract's own counters, read with eth_call at the same block from the
    HashKey Chain RPC: getEndpoint(1), getEndpoint(2) and, while the payee has no other endpoint, creditOf(payee).

The explorer's getLogs API allows about 10 calls per hour per IP without a key; a run needs 3 when nothing overflows.

It also stores the APRO USDC/USD price feed's latestRoundData() at the same block ("usdFeed", the page's fallback for
the USD value) after checking that the feed proxy still points at the aggregator docs/app.js reads (FEED.aggregator)
and reports USDC/USD with 8 decimals. If that check or the read fails, the snapshot is written without usdFeed and
a warning says why (update FEED.aggregator in docs/app.js if the feed moved to a new aggregator).

  python3 tools/dashboard_snapshot.py [--out docs/snapshot.json] [--to-block N]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

LOGS_API = "https://hsk.blockscout.com/api"
RPC = "https://mainnet.hsk.xyz"
CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"
DEPLOY_BLOCK = 29212085
PROVIDER = "0x30ccc6d816854d1a66fbfece15b1515a3e941160"
PAYEE = "0xa8a49a6ca872d54a8fd5fb121139de9bff62a48d"
PINNED = {1: "0xec7dd370d753d746a264e3bd13acdca1733b323132e5cbe5184a573ec56f27b4",   # registration tx per endpoint
          2: "0x97e3dd2d6b5cc86a0a81231f84deaf6ae706bbbcf6a2bf916c3750f170464a82"}
CONFIRMATIONS = 12
PAGE = 1000                 # getLogs answers at most 1000 logs
MAX_PAGES = 20
MAX_BODY = 16_000_000
UA = "hsk-meter-snapshot/2.0"

T_PAID = "0x0f3c07e40340ee795b942048b66e7145339580e319ff1669f015e659b810063b"
T_REFUNDED = "0xadb1dca52dfad065e50a1e25c2ee47ae54013a1f2d6f8ea5abace52eb4b7a4c8"
T_WITHDRAWN = "0xd1c19fbcd4551a5edfb66d43d2e337c04837afda3482b42bdf569a8fccdae5fb"
T_REGISTERED = "0x77dd24cff796128cd07781fd29ca7e3193c52b494cfe6761cffb516a7c989c88"
T_PRICE = "0x2bce37c591c5b0d254c3056688b080a088f160fff82b6e79f456c8a20d5570f6"
SEL_GET_ENDPOINT = "0x937bbc4a"       # getEndpoint(uint256)
SEL_CREDIT_OF = "0x75807250"          # creditOf(address)
FEED_PROXY = "0x244ce344df8837c9d938867e2ffbf0e4b0169b56"        # APRO USDC/USD (docs.hskchain.net, Tools > Oracle)
FEED_AGGREGATOR = "0x86b79c0f9b58ca8fd72da05af3ed4c99c8140c79"   # must equal FEED.aggregator in docs/app.js
FEED_MAX_AGE_S = 26 * 3600
SEL_AGGREGATOR = "0x245a7bfc"         # aggregator()
SEL_DECIMALS = "0x313ce567"           # decimals()
SEL_DESCRIPTION = "0x7284e416"        # description()
SEL_LATEST_ROUND = "0xfeaf968c"       # latestRoundData()

WORD = re.compile(r"^0x[0-9a-f]{64}$")
DATA = re.compile(r"^0x(?:[0-9a-f]{64}){0,16}$")
QTY = re.compile(r"^0x[0-9a-f]{1,12}$")


def pad(h):
    return "0x" + h.removeprefix("0x").rjust(64, "0")


QUERIES = [  # see EXACT_QUERIES in docs/app.js for why only our own accounts can add matching logs
    {"topic1": pad("1"), "topic3": pad(PROVIDER), "topic1_3_opr": "or"},
    {"topic1": pad("2")},
    {"topic1": pad(PAYEE), "topic3": pad(PAYEE), "topic1_3_opr": "or"},
]


class Fail(Exception):
    pass


def http_json(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"user-agent": UA, "accept": "application/json"}
    if data is not None:
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 200:
            raise Fail(f"HTTP {r.status} from {url.split('?')[0]}")
        raw = r.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise Fail("response too large")
    return json.loads(raw)


def rpc(method, params):
    body = http_json(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if not isinstance(body, dict) or "result" not in body:
        raise Fail(f"RPC {method}: {str(body.get('error') if isinstance(body, dict) else body)[:200]}")
    return body["result"]


def eth_call(data, block, to=CONTRACT):
    out = rpc("eth_call", [{"to": to, "data": data}, hex(block)])
    if not isinstance(out, str) or not re.fullmatch(r"0x(?:[0-9a-f]{64})+", out):
        raise Fail("eth_call: unexpected answer")
    return [out[2 + 64 * i:2 + 64 * (i + 1)] for i in range((len(out) - 2) // 64)]


def clean(r):
    """getLogs row -> the item shape docs/app.js reads. Raises on anything malformed: a snapshot is all or nothing."""
    try:
        addr = str(r["address"]).lower()
        tx = str(r["transactionHash"]).lower()
        topics = [str(t).lower() for t in r["topics"] if t is not None]
        data = str(r["data"]).lower()
        bn, li, ts = (str(r[k]).lower() for k in ("blockNumber", "logIndex", "timeStamp"))
    except (KeyError, TypeError) as e:
        raise Fail(f"malformed log: {e}") from None
    if addr != CONTRACT or not WORD.match(tx) or not 1 <= len(topics) <= 4 or not all(WORD.match(t) for t in topics) \
            or not DATA.match(data) or not all(QTY.match(x) for x in (bn, li, ts)):
        raise Fail(f"malformed log in tx {tx[:18]}")
    return {"address": {"hash": CONTRACT}, "transaction_hash": tx, "topics": topics, "data": data,
            "block_number": int(bn, 16), "index": int(li, 16),
            "block_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts, 16)))}


def logs_result(body):
    if not isinstance(body, dict) or not isinstance(body.get("result"), list):
        raise Fail("unexpected getLogs answer")
    if body.get("status") == "1":
        return body["result"]
    if body.get("status") == "0" and not body["result"] and str(body.get("message", "")).startswith("No logs found"):
        return []
    raise Fail(f"getLogs error: {str(body.get('message'))[:200]}")


def get_logs(query, frm, to):
    out, seen = [], set()
    for page in range(MAX_PAGES):
        q = {"module": "logs", "action": "getLogs", "address": CONTRACT, "fromBlock": str(frm), "toBlock": str(to), **query}
        rows = logs_result(http_json(LOGS_API + "?" + urllib.parse.urlencode(q)))
        for r in rows:
            c = clean(r)
            k = (c["transaction_hash"], c["index"])
            if k not in seen:
                seen.add(k)
                out.append(c)
        if len(rows) < PAGE:
            return out
        last = int(str(rows[-1]["blockNumber"]), 16)
        if last <= frm:
            raise Fail(f"more than {PAGE} logs in block {last}: cannot page further")
        frm = last                       # the next page repeats block `last`; duplicates are dropped above
        time.sleep(1)
    raise Fail(f"pages remain after {MAX_PAGES} pages of {PAGE} logs for {query}: snapshot would be incomplete")


def word(data, i):
    return int(data[2 + 64 * i:2 + 64 * (i + 1)], 16)


def addr_of(t):
    return "0x" + t[26:] if t.startswith("0x" + "0" * 24) else None


def check(items, block):
    """Recompute what the page counts and compare it with the contract's counters at `block`."""
    ep = {i: {"paid": 0, "paidSum": 0, "ref": 0, "refSum": 0, "regs": [], "price": None} for i in (1, 2)}
    withdrawn, payee_endpoints = 0, set()
    for it in sorted(items, key=lambda x: (x["block_number"], x["index"])):
        t, d = it["topics"], it["data"]
        eid = {pad("1"): 1, pad("2"): 2}.get(t[1]) if len(t) > 1 else None
        if t[0] == T_PAID and len(t) == 4 and eid:
            if "0x" + d[26:66] != PAYEE:
                raise Fail(f"endpoint {eid} paid someone other than the pinned payee")
            ep[eid]["paid"] += 1
            ep[eid]["paidSum"] += word(d, 1)
        elif t[0] == T_REFUNDED and len(t) == 4 and eid:
            ep[eid]["ref"] += 1
            ep[eid]["refSum"] += word(d, 0)
        elif t[0] == T_WITHDRAWN and len(t) == 3 and addr_of(t[1]) == PAYEE:
            withdrawn += word(d, 0)
        elif t[0] == T_REGISTERED and len(t) == 4:
            if addr_of(t[3]) == PAYEE:
                payee_endpoints.add(int(t[1], 16))
            if eid:
                ep[eid]["regs"].append((it["transaction_hash"], addr_of(t[2]), addr_of(t[3])))
                ep[eid]["price"] = word(d, 0)
        elif t[0] == T_PRICE and len(t) == 2 and eid:
            ep[eid]["price"] = word(d, 1)
    counters = {}
    for i in (1, 2):
        e = ep[i]
        if e["regs"] != [(PINNED[i], PROVIDER, PAYEE)]:
            raise Fail(f"endpoint {i}: registration logs {e['regs']} do not match the pinned registration")
        w = eth_call(SEL_GET_ENDPOINT + pad(hex(i)).removeprefix("0x"), block)
        if len(w) != 9:
            raise Fail("getEndpoint: unexpected answer")
        onchain = {"provider": "0x" + w[0][24:], "payee": "0x" + w[3][24:], "paidCount": int(w[4], 16), "price": int(w[5], 16),
                   "totalPaid": int(w[6], 16), "totalRefunded": int(w[7], 16), "refundCount": int(w[8], 16)}
        mine = {"provider": PROVIDER, "payee": PAYEE, "paidCount": e["paid"], "price": e["price"], "totalPaid": e["paidSum"],
                "totalRefunded": e["refSum"], "refundCount": e["ref"]}
        if onchain != mine:
            diff = {k: (mine[k], onchain[k]) for k in mine if mine[k] != onchain[k]}
            raise Fail(f"endpoint {i}: logs != getEndpoint({i}) at block {block} (logs, chain): {diff}. "
                       "If the explorer is behind the chain, retry in a minute.")
        counters[str(i)] = {k: str(v) for k, v in onchain.items() if k not in ("provider", "payee")}
    out = {"block": block, "endpoints": counters, "withdrawnByPayee": str(withdrawn)}
    if payee_endpoints <= {1, 2}:
        credit = int(eth_call(SEL_CREDIT_OF + pad(PAYEE).removeprefix("0x"), block)[0], 16)
        mine = sum(ep[i]["paidSum"] - ep[i]["refSum"] for i in (1, 2)) - withdrawn
        if credit != mine:
            raise Fail(f"payee credit from the logs {mine} != creditOf(payee) {credit} at block {block}")
        out["creditOfPayee"] = str(credit)
    return out, sorted(payee_endpoints)


def usd_feed(block):
    """The APRO USDC/USD reading at `block` for the page's fallback, after checking the proxy (raises Fail)."""
    agg = "0x" + eth_call(SEL_AGGREGATOR, block, FEED_PROXY)[0][24:]
    if agg != FEED_AGGREGATOR:
        raise Fail(f"the USDC/USD proxy now points at aggregator {agg}; update FEED.aggregator in docs/app.js and "
                   "FEED_AGGREGATOR here")
    if int(eth_call(SEL_DECIMALS, block, FEED_PROXY)[0], 16) != 8:
        raise Fail("USDC/USD feed decimals are not 8")
    w = eth_call(SEL_DESCRIPTION, block, FEED_PROXY)
    if len(w) < 3 or int(w[1], 16) != 8 or bytes.fromhex(w[2][:16]) != b"USDC/USD":
        raise Fail("USDC/USD feed description mismatch")
    r = eth_call(SEL_LATEST_ROUND, block, FEED_PROXY)
    if len(r) != 5:
        raise Fail("latestRoundData: unexpected answer")
    rid, answer, updated, answered = int(r[0], 16), int(r[1], 16), int(r[3], 16), int(r[4], 16)
    if not (rid > 0 and answered >= rid and 50_000_000 <= answer <= 150_000_000 and 0 < updated <= time.time() + 300
            and time.time() - updated <= FEED_MAX_AGE_S):
        raise Fail("USDC/USD round failed the checks (unfinished, out of range or older than 26 h)")
    return {"pair": "USDC/USD", "proxy": "0x244Ce344df8837c9d938867E2Ffbf0E4B0169B56", "aggregator": FEED_AGGREGATOR,
            "decimals": 8, "roundId": str(rid), "answer": str(answer), "updatedAt": updated, "block": block}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "snapshot.json"))
    ap.add_argument("--to-block", type=int, default=None, help="last block to include (default: chain head - %d)" % CONFIRMATIONS)
    args = ap.parse_args()
    try:
        head = int(rpc("eth_blockNumber", []), 16)
        to = args.to_block if args.to_block is not None else head - CONFIRMATIONS
        if not DEPLOY_BLOCK <= to <= head:
            raise Fail(f"--to-block must be between {DEPLOY_BLOCK} and the chain head {head}")
        items, seen = [], set()
        for i, q in enumerate(QUERIES):
            if i:
                time.sleep(1)
            for c in get_logs(q, DEPLOY_BLOCK, to):
                k = (c["transaction_hash"], c["index"])
                if k not in seen:
                    seen.add(k)
                    items.append(c)
        items.sort(key=lambda x: (x["block_number"], x["index"]))
        counters, payee_endpoints = check(items, to)
    except Fail as e:
        print(f"snapshot NOT written: {e}", file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError) as e:
        print(f"snapshot NOT written: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        sys.exit(1)
    out = {"generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "source": LOGS_API + "?module=logs&action=getLogs",
           "contract": CONTRACT, "fromBlock": DEPLOY_BLOCK, "toBlock": to, "complete": True, "filters": QUERIES,
           "payeeEndpoints": payee_endpoints, "counters": counters, "items": items}
    try:
        out["usdFeed"] = usd_feed(to)
    except (Fail, OSError, ValueError) as e:
        print(f"warning: snapshot written without usdFeed: {str(e)[:300]}", file=sys.stderr)
    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, args.out)
    print(json.dumps({"out": args.out, "items": len(items), "toBlock": to, "generatedAt": out["generatedAt"],
                      "counters": counters, "usdFeed": out.get("usdFeed")}))


if __name__ == "__main__":
    main()
