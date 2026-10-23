"""HashKey Chain access for the seller server: a locked-down JSON-RPC client, HskPayPerRequest reads, Paid-log decoding,
and a serialized transaction sender for the provider's own calls (payWithAuthorization submission, refund).
Never logs keys; never follows redirects; only the official RPC host (or 127.0.0.1 for local tests)."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

RPC_HOSTS = {"mainnet.hsk.xyz"}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
UA = "swiftbotlab-hsk-paywall/1.0"
PAID_TOPIC = "0x" + keccak(text="Paid(uint256,bytes32,address,address,uint256)").hex()
REFUNDED_TOPIC = "0x" + keccak(text="Refunded(uint256,bytes32,address,uint256)").hex()
ENDPOINT_T = ["(address,bool,uint64,address,uint64,uint128,uint128,uint128,uint64)"]
PAYMENT_T = ["(address,uint64,bool,uint128,uint64,uint64)"]
ERRORS = {  # custom errors of HskPayPerRequest (selector -> name), for readable refusals
    "0x" + keccak(text=s).hex()[:8]: s.split("(")[0] for s in [
        "UnknownEndpoint(uint256)", "EndpointInactive(uint256)", "ZeroRequestId()", "RequestNotForPayer(address)",
        "AlreadyPaid(bytes32)", "WrongAmount(uint256,uint256)", "UnknownPayment(bytes32)", "AlreadyRefunded(bytes32)",
        "InsufficientCredit(uint256,uint256)", "NotProvider()", "BalanceMismatch(uint256,uint256)", "ZeroAddress()"]
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise urllib.error.HTTPError(a[1] if len(a) > 1 else "", 399, "redirects refused", None, None)


_OPENER = urllib.request.build_opener(_NoRedirect)


class RpcError(RuntimeError):
    pass


class Rpc:
    def __init__(self, url, allow_local=False, timeout=10, max_bytes=2_000_000):
        u = urllib.parse.urlparse(url)
        remote_ok = u.scheme == "https" and u.hostname in RPC_HOSTS and not u.username
        local_ok = allow_local and u.scheme == "http" and u.hostname in LOCAL_HOSTS
        if not (remote_ok or local_ok):
            raise ValueError(f"RPC URL not allowed: {u.scheme}://{u.hostname}")
        self.url, self.timeout, self.max_bytes = url, timeout, max_bytes
        self._id = 0

    def __call__(self, method, params, tries=1):
        last = None
        for i in range(tries):
            self._id += 1
            body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
            req = urllib.request.Request(self.url, data=body, headers={"content-type": "application/json", "user-agent": UA})
            try:
                with _OPENER.open(req, timeout=self.timeout) as r:
                    raw = r.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise RpcError("RPC response too large")
                j = json.loads(raw)
                if not isinstance(j, dict):
                    raise RpcError("RPC returned invalid JSON")
                if "error" in j:
                    err = j["error"] if isinstance(j["error"], dict) else {"message": str(j["error"])}
                    e = RpcError("RPC error: " + str(err.get("message"))[:200])
                    e.data = err.get("data")
                    raise e
                return j.get("result")
            except RpcError:
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as e:
                last = e
                time.sleep(min(2 * (i + 1), 6))
        raise RpcError(f"RPC unavailable: {str(last)[:120]}")


def selector(sig):
    return keccak(text=sig)[:4]


def eth_call(rpc, to, sig, types, args, out, frm=None, block="latest", tries=3):
    tx = {"to": to, "data": "0x" + (selector(sig) + encode(types, args)).hex()}
    if frm:
        tx["from"] = frm
    res = rpc("eth_call", [tx, block], tries=tries)
    return decode(out, bytes.fromhex(res[2:]))


def read_endpoint(rpc, contract, eid):
    e = eth_call(rpc, contract, "getEndpoint(uint256)", ["uint256"], [int(eid)], ENDPOINT_T)[0]
    return {"provider": e[0].lower(), "active": e[1], "createdAt": e[2], "payee": e[3].lower(), "paidCount": e[4],
            "price": e[5], "totalPaid": e[6], "totalRefunded": e[7], "refundCount": e[8]}


def read_payment(rpc, contract, rid, block="latest"):
    p = eth_call(rpc, contract, "getPayment(bytes32)", ["bytes32"], [bytes.fromhex(rid[2:])], PAYMENT_T, block=block)[0]
    return {"payer": p[0].lower(), "endpointId": p[1], "refunded": p[2], "amount": p[3], "paidAt": p[4], "paidBlock": p[5]}


def paid_logs(receipt, contract, rid):
    out = []
    for lg in receipt.get("logs") or []:
        t = lg.get("topics") or []
        if (str(lg.get("address", "")).lower() == contract.lower() and len(t) == 4 and t[0].lower() == PAID_TOPIC
                and t[2].lower() == rid.lower()):
            payee, amount = decode(["address", "uint256"], bytes.fromhex(lg["data"][2:]))
            out.append({"endpointId": int(t[1], 16), "requestId": t[2].lower(), "payer": "0x" + t[3][-40:].lower(),
                        "payee": payee.lower(), "amount": amount, "logIndex": int(lg.get("logIndex", "0x0"), 16)})
    return out


def revert_reason(err):
    """Readable reason from an eth_call/estimateGas revert (custom error name or Error(string))."""
    data = getattr(err, "data", None)
    if isinstance(data, dict):
        data = data.get("data")
    if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
        sel = data[:10].lower()
        if sel in ERRORS:
            return ERRORS[sel]
        if sel == "0x08c379a0":
            try:
                return decode(["string"], bytes.fromhex(data[10:]))[0][:120]
            except Exception:
                pass
    return str(err)[:160]


class Sender:
    """Signs and sends the provider's transactions one at a time (one nonce stream), with a daily gas budget."""

    def __init__(self, rpc, account, chain_id, max_fee_cap_wei=2000 * 10**9, daily_budget_wei=3 * 10**18):
        self.rpc, self.acct, self.chain_id = rpc, account, chain_id
        self.max_fee_cap, self.daily_budget = max_fee_cap_wei, daily_budget_wei
        self.lock = threading.Lock()
        self.day, self.spent = time.strftime("%Y-%m-%d", time.gmtime()), 0

    @property
    def address(self):
        return self.acct.address

    def send(self, to, data, gas_margin=1.3, wait_s=90, lag_retries=0):
        """Simulate, sign, send once, wait for the receipt. Returns the receipt (dict) or raises RpcError.
        lag_retries: re-simulate after 2 s when the simulation reverts (a load-balanced RPC node can be a block behind
        the one that just returned our previous receipt). Only for calls whose revert can be caused by that lag."""
        with self.lock:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            if today != self.day:
                self.day, self.spent = today, 0
            call = {"from": self.acct.address, "to": to, "data": "0x" + data.hex()}
            for attempt in range(lag_retries + 1):
                try:
                    est = int(self.rpc("eth_estimateGas", [call], tries=2), 16)   # raises RpcError(data=revert) on revert
                    break
                except RpcError as e:
                    if attempt == lag_retries or "RPC unavailable" in str(e):
                        raise
                    time.sleep(2)
            blk = self.rpc("eth_getBlockByNumber", ["latest", False], tries=3)
            base = int(blk["baseFeePerGas"], 16)
            tip = 10**6
            max_fee = 2 * base + tip
            if max_fee > self.max_fee_cap:
                raise RpcError("gas price above the server's cap; try later")
            gas = int(est * gas_margin) + 5000
            if self.spent + gas * max_fee > self.daily_budget:
                raise RpcError("daily settlement gas budget reached")
            nonce = int(self.rpc("eth_getTransactionCount", [self.acct.address, "pending"], tries=3), 16)
            tx = {"chainId": self.chain_id, "nonce": nonce, "to": to_checksum_address(to), "value": 0, "data": data,
                  "gas": gas, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip, "type": 2}
            raw = self.acct.sign_transaction(tx).raw_transaction
            h = "0x" + keccak(raw).hex()
            try:
                self.rpc("eth_sendRawTransaction", ["0x" + raw.hex()])      # never retried blindly
            except RpcError as e:
                if "RPC unavailable" not in str(e):
                    raise
                # network error: the tx may or may not have been accepted; poll by hash below instead of resending
            deadline = time.time() + wait_s
            while time.time() < deadline:
                try:
                    rc = self.rpc("eth_getTransactionReceipt", [h])
                except RpcError:
                    rc = None
                if rc:
                    used = int(rc["gasUsed"], 16) * int(rc.get("effectiveGasPrice", hex(max_fee)), 16)
                    self.spent += used
                    rc["_fee_wei"] = used
                    return rc
                time.sleep(1.5)
            self.spent += gas * max_fee
            raise RpcError(f"no receipt for {h} yet")
