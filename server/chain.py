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
        "InsufficientCredit(uint256,uint256)", "NotProvider()", "BalanceMismatch(uint256,uint256)", "ZeroAddress()",
        "ZeroAmount()"]
}
AUTH_USED = "FiatTokenV2: authorization is used or canceled"


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


def find_paid_log(rpc, contract, rid, block):
    """The Paid(endpointId, requestId, payer, payee, amount) log of `rid`, looked up by request id (eth_getLogs on the
    one block getPayment() names), never by a client-supplied transaction hash. Returns a dict or None."""
    logs = rpc("eth_getLogs", [{"address": contract, "fromBlock": hex(int(block)), "toBlock": hex(int(block)),
                                "topics": [PAID_TOPIC, None, rid.lower()]}], tries=3)
    if not isinstance(logs, list):
        return None
    for lg in logs:
        t = lg.get("topics") or []
        if (str(lg.get("address", "")).lower() == contract.lower() and len(t) == 4 and t[0].lower() == PAID_TOPIC
                and t[2].lower() == rid.lower() and not lg.get("removed")):
            payee, amount = decode(["address", "uint256"], bytes.fromhex(lg["data"][2:]))
            return {"endpointId": int(t[1], 16), "requestId": t[2].lower(), "payer": "0x" + t[3][-40:].lower(),
                    "payee": payee.lower(), "amount": amount, "logIndex": int(lg.get("logIndex", "0x0"), 16),
                    "tx": str(lg.get("transactionHash", "")).lower(), "block": int(lg.get("blockNumber", hex(int(block))), 16)}
    return None


def code_at(rpc, addr):
    return rpc("eth_getCode", [addr, "latest"], tries=3)


def erc20_balance(rpc, token, owner):
    return eth_call(rpc, token, "balanceOf(address)", ["address"], [owner], ["uint256"])[0]


def authorization_state(rpc, token, authorizer, nonce):
    return eth_call(rpc, token, "authorizationState(address,bytes32)", ["address", "bytes32"],
                    [authorizer, bytes.fromhex(nonce[2:])], ["bool"])[0]


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


class PendingTx(RpcError):
    """The transaction was broadcast but no receipt arrived in time. `.tx` is its hash; never resend blindly."""

    def __init__(self, tx):
        super().__init__(f"no receipt for {tx} yet")
        self.tx = tx


class Sender:
    """Signs and sends the provider's transactions one at a time (one nonce stream). Each kind of transaction
    ("settle", "refund") has its own daily gas budget (UTC day), so refunds can never eat the settlement budget.

    Every send is simulated with exactly the fields of the real transaction: the same gas limit, maxFeePerGas and
    maxPriorityFeePerGas (so tx.gasprice in the simulation equals the real one), first by eth_estimateGas and then by
    eth_call with the final gas limit. `gate` is called only after the simulation passed, right before signing: a
    rate limiter passed as gate therefore counts only transactions that are actually sent."""

    def __init__(self, rpc, account, chain_id, max_fee_cap_wei=2000 * 10**9, budgets=None, spent=None, on_spend=None):
        self.rpc, self.acct, self.chain_id = rpc, account, chain_id
        self.max_fee_cap = max_fee_cap_wei
        self.budgets = dict(budgets or {"settle": 2 * 10**18, "refund": 5 * 10**17})
        self.lock = threading.Lock()
        self.day = time.strftime("%Y-%m-%d", time.gmtime())
        self.spent = dict(spent or {})          # kind -> wei spent today (restored from the seller's journal)
        self.on_spend = on_spend                # callback(kind, wei) to persist spending

    @property
    def address(self):
        return self.acct.address

    def _roll_day(self):
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self.day:
            self.day, self.spent = today, {}

    def _charge(self, kind, wei):
        self.spent[kind] = self.spent.get(kind, 0) + wei
        if self.on_spend:
            try:
                self.on_spend(kind, wei)
            except Exception:
                pass

    def fees(self):
        blk = self.rpc("eth_getBlockByNumber", ["latest", False], tries=3)
        base = int(blk["baseFeePerGas"], 16)
        tip = 10**6
        return base, tip, 2 * base + tip

    def send(self, to, data, kind="settle", gas_margin=1.3, wait_s=90, lag_retries=0, gate=None, max_cost_wei=None):
        """Simulate, sign, send once, wait for the receipt. Returns the receipt (dict, with _fee_wei) or raises RpcError
        (simulation revert: .data holds the revert data), PendingTx (sent, no receipt yet).
        lag_retries: re-simulate after 2 s when the simulation reverts (a load-balanced RPC node can be a block behind
        the one that just returned our previous receipt). Only for calls whose revert can be caused by that lag.
        max_cost_wei: refuse when the expected fee (estimate x (base fee + tip)) is above it (gas spike guard)."""
        if kind not in self.budgets:
            raise ValueError(f"unknown transaction kind {kind}")
        with self.lock:
            self._roll_day()
            base, tip, max_fee = self.fees()
            if max_fee > self.max_fee_cap:
                raise RpcError("gas price above the server's cap; try later")
            call = {"from": self.acct.address, "to": to, "data": "0x" + data.hex(),
                    "maxFeePerGas": hex(max_fee), "maxPriorityFeePerGas": hex(tip)}
            for attempt in range(lag_retries + 1):
                try:
                    est = int(self.rpc("eth_estimateGas", [call], tries=2), 16)   # raises RpcError(data=revert) on revert
                    gas = int(est * gas_margin) + 5000
                    self.rpc("eth_call", [{**call, "gas": hex(gas)}, "latest"], tries=2)   # the exact tx, as it will run
                    break
                except RpcError as e:
                    if attempt == lag_retries or "RPC unavailable" in str(e):
                        raise
                    time.sleep(2)
            if max_cost_wei is not None and est * (base + tip) > max_cost_wei:
                raise RpcError("gas cost above this sale's price right now; try later")
            if self.spent.get(kind, 0) + gas * max_fee > self.budgets[kind]:
                raise RpcError(f"daily {kind} gas budget reached")
            if gate is not None and not gate():
                raise RpcError("busy: too many settlements right now")
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
            rc = self.wait_receipt(h, wait_s)
            if rc:
                used = int(rc["gasUsed"], 16) * int(rc.get("effectiveGasPrice", hex(max_fee)), 16)
                self._charge(kind, used)
                rc["_fee_wei"] = used
                return rc
            self._charge(kind, gas * max_fee)
            raise PendingTx(h)

    def wait_receipt(self, h, wait_s):
        deadline = time.time() + wait_s
        while True:
            try:
                rc = self.rpc("eth_getTransactionReceipt", [h])
            except RpcError:
                rc = None
            if rc or time.time() >= deadline:
                return rc or None
            time.sleep(min(1.5, max(0.05, wait_s / 5)))

    def balance(self):
        return int(self.rpc("eth_getBalance", [self.acct.address, "latest"], tries=3), 16)
