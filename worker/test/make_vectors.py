"""Regenerates worker/test/vectors.json from Python: server/protocol.py (requestId, envelopes), the seller's number
formatting, and an independent HMAC computation of the Worker's quote tokens.
  python3 worker/test/make_vectors.py worker/test/vectors.json
The Worker tests check its requestId derivation, quote tokens, number formatting and envelopes against these."""
import hashlib, hmac, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "server"))
import protocol as P
C = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"
out = {"rid": [], "fmtFloat": [], "fmtInt": [], "token": []}
cases = [
    (1, "0x5270fe01fd3d6148b687da0b5693b0425cfca361", "GET /v1/hsk/address-profile?address=0x78c8476772bc209c3df09da5e129d0879cd57f8c", "0x" + "11" * 32),
    (2, "0x5270fe01fd3d6148b687da0b5693b0425cfca361", "GET /v1/hsk/tx-receipt?hash=0x" + "ab" * 32, "0x" + "0f" * 16 + "e1" * 16),
    (1, "0x0000000000000000000000000000000000000001", "GET /v1/hsk/address-profile?address=0x0000000000000000000000000000000000000000", "0x" + "00" * 32),
]
for eid, payer, res, tok in cases:
    d, r = P.compute_request_id(177, C, eid, payer, res, tok)
    out["rid"].append({"eid": eid, "payer": payer, "resource": res, "token": tok, "digest": d, "requestId": r})
for x in [0, 1, 7812500000000000, 10**18, 1277589056485870300, 123456789012345678901, 2**64 + 12345, 999999500000000000, 1000000500000000001, 5 * 10**11, 15 * 10**11]:
    out["fmtFloat"].append({"x": str(x), "div": "1e18", "py": f"{x / 1e18:.6f}"})
for x in [0, 146206, 30000, 25000, 7812, 1234565, 2**53 + 1, 10**30 + 7]:
    out["fmtFloat"].append({"x": str(x), "div": "1e6", "py": f"{x / 1e6:.6f}"})
for x, dec in [(1234567500000000000, 18), (1000000500000000001, 18), (999999999999999999999, 18), (2**63 + 777, 18), (7812500000000000, 18),
               (1234565, 6), (30000, 6), (2**60, 6), (3, 18), (10**24 + 5 * 10**11, 18), (1000000500000000000, 18)]:
    out["fmtInt"].append({"x": str(x), "dec": dec, "py": f"{x / 10**dec:.6f}"})
key = bytes(range(32))
for (eid, payer, res, _), price in zip(cases, (30000, 25000, 2**63 + 5)):
    # the Worker's own token: quoted price (uint64 big-endian) || 8 random bytes || HMAC-SHA256(key, ctx || first 16)[:16],
    # ctx naming the service ("worker"), so tokens of the Python seller never verify there
    n = price.to_bytes(8, "big") + hashlib.sha256(res.encode()).digest()[:8]
    ctx = f"{P.DOMAIN_TAG}|worker|177|{C}|{eid}|{payer}|{res}".encode()
    mac = hmac.new(key, ctx + n, hashlib.sha256).digest()[:16]
    out["token"].append({"key": key.hex(), "eid": eid, "payer": payer, "resource": res, "price": str(price),
                         "rand": n[8:].hex(), "token": "0x" + (n + mac).hex()})
out["paidTopic"] = "0x" + __import__("eth_utils").keccak(text="Paid(uint256,bytes32,address,address,uint256)").hex()
out["b64"] = P.b64json({"b": [1, {"z": "x", "a": None}], "a": "é", "c": True})
json.dump(out, open(sys.argv[1], "w"), indent=1)
print("ok", len(out["rid"]), len(out["fmtFloat"]), len(out["fmtInt"]))
