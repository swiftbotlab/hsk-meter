"""Offline stand-in for HashKey Chain, for the seller's tests (Swift Bot Labs). No network, no real keys.

FakeChain answers the JSON-RPC calls the seller and chain.Sender make (eth_call, eth_estimateGas, eth_getLogs,
eth_sendRawTransaction, ...). Transactions are the REAL signed bytes the Sender broadcasts: they are decoded and
executed with the rules of HskPayPerRequest (payWithAuthorization, refund) and of USDC.e's EIP-3009
receiveWithAuthorization (signature, validity window, nonce, balance). Gas is charged to the sender at the effective
gas price, so tests can measure what an attack costs the provider.

Knobs: `contract_wallets` (payers with code whose signature check passes only when tx.gasprice == 0, the
ERC-1271/EIP-7702 trick), `hold_txs` (broadcast but no receipt until mine()), `fail_methods` (RPC errors).
"""
import time

from eth_abi import decode, encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak
from hexbytes import HexBytes

import chain as C
import protocol as P

ENDPOINT_T = ["(address,bool,uint64,address,uint64,uint128,uint128,uint128,uint64)"]
PAYMENT_T = ["(address,uint64,bool,uint128,uint64,uint64)"]
GAS = {"pay": 178_123, "refund": 120_892, "other": 50_000}
AUTH_USED = getattr(C, "AUTH_USED", "FiatTokenV2: authorization is used or canceled")   # also works with older chain.py
BASE_FEE = 500 * 10**9


def sel(sig):
    return "0x" + keccak(text=sig)[:4].hex()


S_PWA = sel("payWithAuthorization(uint256,bytes32,address,uint256,uint256,uint256,bytes)")
S_REFUND = sel("refund(bytes32)")
S_SETPRICE = sel("setPrice(uint256,uint128)")
S_GETEP = sel("getEndpoint(uint256)")
S_GETPAY = sel("getPayment(bytes32)")
S_BAL = sel("balanceOf(address)")
S_AUTH = sel("authorizationState(address,bytes32)")
S_TOKEN = sel("token()")
S_LATEST = sel("latestRoundData()")
S_DECIMALS = sel("decimals()")
S_DESCRIPTION = sel("description()")
S_ISHUMAN = sel("isHuman(address)")
S_KYCINFO = sel("getKycInfo(address)")


def revert(name_sig=None, args_types=(), args=(), message=None):
    e = C.RpcError("RPC error: execution reverted")
    if message is not None:
        e.data = "0x08c379a0" + encode(["string"], [message]).hex()
    else:
        e.data = sel(name_sig) + encode(list(args_types), list(args)).hex()
    return e


class FakeChain:
    def __init__(self, contract, provider, payee, prices=None, provider_hsk=10 * 10**18):
        self.contract, self.provider, self.payee = contract.lower(), provider.lower(), payee.lower()
        self.endpoints = {eid: {"price": p, "active": True} for eid, p in (prices or {1: 30000, 2: 25000}).items()}
        self.payments = {}                 # rid -> payment
        self.usdce = {}                    # holder -> units
        self.native = {self.provider: provider_hsk}
        self.codes = {}                    # addr -> runtime code (hex)
        self.contract_wallets = set()      # payers with code, valid signature only at gasprice 0
        self.auth_used = set()             # (authorizer, nonce)
        self.credit = 0
        self.receipts = {}                 # tx hash -> receipt
        self.txs = {}                      # tx hash -> decoded tx
        self.held = []                     # (hash, raw) broadcast, not mined
        self.hold_txs = False
        self.fail_methods = set()
        self.nonces = {}
        self.block = 1000
        self.logs = []                     # Paid logs (with blockNumber, transactionHash)
        self.calls = []                    # every method called, in order
        self.sent = []                     # selectors of transactions broadcast (provider txs)
        self.gas_burned = 0                # wei the provider paid
        self.pre_execute = None            # callable(call) run right before a broadcast tx executes (front-running)
        self.on_estimate = None            # callable(call) run before each eth_estimateGas (state changes mid-flight)
        self.broadcast_delay = 0.0         # seconds eth_sendRawTransaction takes
        self.block_refunds = False         # refunds revert with InsufficientCredit (e.g. the payee withdrew)
        self.stale_reads = False           # simulations lag one block: they miss the payment of the last tx
        self.last_rids = set()
        self.feeds = {}                    # price feed address -> {description, decimals, roundId, answer, updatedAt, answeredInRound}
        self.kyc_sbt = None                # KYC SBT address, and its records: holder -> {valid, level, status, createTime}
        self.kyc = {}
        self.fail_targets = set()          # eth_call to these addresses fails with an RPC error
        self.call_targets = []             # the `to` of every eth_call, in order

    # ---- helpers for tests
    def fund(self, who, units):
        self.usdce[who.lower()] = self.usdce.get(who.lower(), 0) + units

    def add_external_tx(self, h):
        self.receipts[h.lower()] = {"status": "0x1", "blockNumber": hex(self.block - 5), "gasUsed": "0x5208",
                                    "effectiveGasPrice": hex(BASE_FEE), "logs": [], "from": "0x" + "01" * 20,
                                    "to": "0x" + "02" * 20, "transactionHash": h.lower()}
        self.txs[h.lower()] = {"value": "0x0"}

    def submit_direct(self, submitter, call_data):
        """Execute payWithAuthorization sent by someone else (e.g. the buyer submits its own signed authorization)."""
        h = "0x" + keccak(call_data + submitter.lower().encode()).hex()
        self.block += 1
        kind, logs = self._simulate({"from": submitter, "to": self.contract, "data": "0x" + call_data.hex(),
                                     "maxFeePerGas": hex(BASE_FEE), "maxPriorityFeePerGas": "0x0"}, apply=True, tx_hash=h)
        self.receipts[h] = {"status": "0x1", "transactionHash": h, "blockNumber": hex(self.block), "gasUsed": hex(GAS[kind]),
                            "effectiveGasPrice": hex(BASE_FEE), "logs": logs, "from": submitter, "to": self.contract}
        return h

    def mine(self):
        held, self.held = self.held, []
        for h, raw in held:
            self._execute(h, raw)

    # ---- JSON-RPC entry point
    def __call__(self, method, params, tries=1):
        self.calls.append(method)
        if method in self.fail_methods:
            raise C.RpcError("RPC unavailable: test failure")
        if method == "eth_chainId":
            return hex(177)
        if method == "eth_blockNumber":
            return hex(self.block)
        if method == "eth_getBlockByNumber":
            return {"number": hex(self.block), "baseFeePerGas": hex(BASE_FEE)}
        if method == "eth_getCode":
            a = params[0].lower()
            return "0x60806040" if a == self.contract else self.codes.get(a, "0x")
        if method == "eth_getBalance":
            return hex(self.native.get(params[0].lower(), 0))
        if method == "eth_getTransactionCount":
            return hex(self.nonces.get(params[0].lower(), 0))
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0].lower())
        if method == "eth_getTransactionByHash":
            return self.txs.get(params[0].lower())
        if method == "eth_getLogs":
            f = params[0]
            rid = (f.get("topics") or [None, None, None])[2]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            return [lg for lg in self.logs if lg["topics"][2] == rid and lo <= int(lg["blockNumber"], 16) <= hi]
        if method == "eth_estimateGas":
            if self.on_estimate:
                self.on_estimate(params[0])
            kind = self._simulate(params[0])
            return hex(GAS[kind])
        if method == "eth_call":
            return self._call(params[0])
        if method == "eth_sendRawTransaction":
            return self._broadcast(params[0])
        raise C.RpcError("RPC error: method not supported in FakeChain: " + method)

    # ---- views
    def set_feed(self, address, description, price, decimals=8, age_s=60, round_id=(1 << 64) + 100, answered=None,
                 updated_at=None):
        self.feeds[address.lower()] = {"description": description, "decimals": decimals, "roundId": round_id,
                                       "answer": int(round(price * 10**decimals)) if isinstance(price, float) else int(price),
                                       "updatedAt": int(time.time()) - age_s if updated_at is None else updated_at,
                                       "answeredInRound": round_id if answered is None else answered}

    def _call(self, call):
        to, data = call["to"].lower(), call["data"]
        s, args = data[:10], bytes.fromhex(data[10:])
        self.call_targets.append(to)
        if to in self.fail_targets:
            raise C.RpcError("RPC unavailable: test failure")
        if to in self.feeds:
            f = self.feeds[to]
            if s == S_LATEST:
                return "0x" + encode(["uint80", "int256", "uint256", "uint256", "uint80"],
                                     [f["roundId"], f["answer"], f["updatedAt"], f["updatedAt"], f["answeredInRound"]]).hex()
            if s == S_DECIMALS:
                return "0x" + encode(["uint8"], [f["decimals"]]).hex()
            if s == S_DESCRIPTION:
                return "0x" + encode(["string"], [f["description"]]).hex()
            raise revert(message="unknown function")
        if self.kyc_sbt and to == self.kyc_sbt.lower() and s in (S_ISHUMAN, S_KYCINFO):
            who = decode(["address"], args)[0].lower()
            k = self.kyc.get(who, {"valid": False, "level": 0, "status": 0, "createTime": 0})
            if s == S_ISHUMAN:
                return "0x" + encode(["bool", "uint8"], [k["valid"], k["level"] if k["valid"] else 0]).hex()
            return "0x" + encode(["string", "uint8", "uint8", "uint256"],
                                 ["x.hsk" if k["status"] else "", k["level"], k["status"], k["createTime"]]).hex()
        if to == self.contract and s in (S_PWA, S_REFUND, S_SETPRICE):
            self._simulate(call)
            return "0x"
        if to == self.contract and s == S_GETEP:
            eid = decode(["uint256"], args)[0]
            e = self.endpoints.get(eid)
            if not e:
                return "0x" + encode(ENDPOINT_T, [("0x" + "00" * 20, False, 0, "0x" + "00" * 20, 0, 0, 0, 0, 0)]).hex()
            return "0x" + encode(ENDPOINT_T, [(self.provider, e["active"], 1, self.payee, 0, e["price"], 0, 0, 0)]).hex()
        if to == self.contract and s == S_GETPAY:
            rid = "0x" + decode(["bytes32"], args)[0].hex()
            p = self.payments.get(rid)
            if not p:
                return "0x" + encode(PAYMENT_T, [("0x" + "00" * 20, 0, False, 0, 0, 0)]).hex()
            return "0x" + encode(PAYMENT_T, [(p["payer"], p["endpointId"], p["refunded"], p["amount"], p["paidAt"], p["paidBlock"])]).hex()
        if to == self.contract and s == S_TOKEN:
            return "0x" + encode(["address"], [P.USDCE]).hex()
        if to == P.USDCE.lower() and s == S_BAL:
            who = decode(["address"], args)[0].lower()
            return "0x" + encode(["uint256"], [self.usdce.get(who, 0)]).hex()
        if to == P.USDCE.lower() and s == S_AUTH:
            who, n = decode(["address", "bytes32"], args)
            return "0x" + encode(["bool"], [(who.lower(), "0x" + n.hex()) in self.auth_used]).hex()
        if s == S_BAL:                                       # other tokens: zero balances
            return "0x" + encode(["uint256"], [0]).hex()
        raise C.RpcError("RPC error: call not supported in FakeChain")

    # ---- transactions
    @staticmethod
    def _gasprice(call_or_tx):
        mf = call_or_tx.get("maxFeePerGas")
        if mf is None:
            return 0                                          # a call without fee fields runs at gasprice 0
        mf = int(mf, 16) if isinstance(mf, str) else int(mf)
        tip = call_or_tx.get("maxPriorityFeePerGas", 0)
        tip = int(tip, 16) if isinstance(tip, str) else int(tip)
        return min(mf, BASE_FEE + tip)

    def _simulate(self, call, apply=False, tx_hash=None):
        to = call["to"].lower()
        data = call["data"]
        data = data if isinstance(data, str) else "0x" + bytes(data).hex()
        frm = call.get("from", "0x" + "00" * 20).lower()
        s, args = data[:10], bytes.fromhex(data[10:])
        gasprice = self._gasprice(call)
        if to != self.contract:
            return ("other", []) if apply else "other"
        if s == S_PWA:
            eid, rid_b, payer, value, va, vb, sig = decode(["uint256", "bytes32", "address", "uint256", "uint256", "uint256", "bytes"], args)
            rid, payer = "0x" + rid_b.hex(), payer.lower()
            e = self.endpoints.get(eid)
            if not e:
                raise revert("UnknownEndpoint(uint256)", ["uint256"], [eid])
            if not e["active"]:
                raise revert("EndpointInactive(uint256)", ["uint256"], [eid])
            bound = "0x" + rid[2:42]
            if bound != "0x" + "00" * 20 and bound != payer:
                raise revert("RequestNotForPayer(address)", ["address"], [bound])
            stale = self.stale_reads and not apply and rid in self.last_rids
            if rid in self.payments and not stale:
                raise revert("AlreadyPaid(bytes32)", ["bytes32"], [rid_b])
            if value != e["price"]:
                raise revert("WrongAmount(uint256,uint256)", ["uint256", "uint256"], [value, e["price"]])
            nonce = P.authorization_nonce(self.contract, eid, rid)
            now = time.time()
            if not (now > va):
                raise revert(message="FiatTokenV2: authorization is not yet valid")
            if not (now < vb):
                raise revert(message="FiatTokenV2: authorization is expired")
            if (payer, nonce) in self.auth_used and not stale:
                raise revert(message=AUTH_USED)
            au = {"from": payer, "to": self.contract, "value": value, "validAfter": va, "validBefore": vb, "nonce": nonce}
            if payer in self.contract_wallets:
                ok = gasprice == 0                          # ERC-1271 wallet that only validates at gasprice 0
            elif self.codes.get(payer, "0x") != "0x":
                ok = False
            else:
                ok = self._recover(au, sig) == payer
            if not ok:
                raise revert(message="FiatTokenV2: invalid signature")
            if self.usdce.get(payer, 0) < value and not stale:
                raise revert(message="ERC20: transfer amount exceeds balance")
            if apply:
                self.last_rids = {rid}
                self.auth_used.add((payer, nonce))
                self.usdce[payer] -= value
                self.credit += value
                self.payments[rid] = {"payer": payer, "endpointId": eid, "refunded": False, "amount": value,
                                      "paidAt": int(now), "paidBlock": self.block}
                lg = {"address": self.contract, "logIndex": "0x0", "blockNumber": hex(self.block), "transactionHash": tx_hash,
                      "topics": [C.PAID_TOPIC, "0x%064x" % eid, rid, "0x" + "00" * 12 + payer[2:]],
                      "data": "0x" + encode(["address", "uint256"], [self.payee, value]).hex()}
                self.logs.append(lg)
                return "pay", [lg]
            return "pay"
        if s == S_REFUND:
            rid_b = decode(["bytes32"], args)[0]
            rid = "0x" + rid_b.hex()
            p = self.payments.get(rid)
            if not p:
                raise revert("UnknownPayment(bytes32)", ["bytes32"], [rid_b])
            if frm != self.provider:
                raise revert("NotProvider()")
            if p["refunded"]:
                raise revert("AlreadyRefunded(bytes32)", ["bytes32"], [rid_b])
            if self.block_refunds or p["amount"] > self.credit:
                raise revert("InsufficientCredit(uint256,uint256)", ["uint256", "uint256"], [p["amount"], 0 if self.block_refunds else self.credit])
            if apply:
                p["refunded"] = True
                self.credit -= p["amount"]
                self.usdce[p["payer"]] = self.usdce.get(p["payer"], 0) + p["amount"]
                lg = {"address": self.contract, "logIndex": "0x0", "blockNumber": hex(self.block), "transactionHash": tx_hash,
                      "topics": [C.REFUNDED_TOPIC, "0x%064x" % p["endpointId"], rid, "0x" + "00" * 12 + p["payer"][2:]],
                      "data": "0x" + encode(["uint256"], [p["amount"]]).hex()}
                return "refund", [lg]
            return "refund"
        if s == S_SETPRICE:
            eid, price = decode(["uint256", "uint128"], args)
            if frm != self.provider:
                raise revert("NotProvider()")
            if apply:
                self.endpoints[eid]["price"] = price
                return "other", []
            return "other"
        raise revert(message="unknown function")

    @staticmethod
    def _recover(au, sig):
        try:
            td = P.receive_auth_typed_data(au["from"], au["to"], au["value"], au["validAfter"], au["validBefore"], au["nonce"])
            return Account.recover_message(encode_typed_data(full_message=td), signature=bytes(sig)).lower()
        except Exception:
            return None

    def _broadcast(self, raw_hex):
        if self.broadcast_delay:
            time.sleep(self.broadcast_delay)
        raw = bytes.fromhex(raw_hex[2:])
        h = "0x" + keccak(raw).hex()
        frm = Account.recover_transaction(raw).lower()
        self.nonces[frm] = self.nonces.get(frm, 0) + 1
        d = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        self.sent.append("0x" + bytes(d["data"])[:4].hex())
        if self.hold_txs:
            self.held.append((h, raw))
        else:
            self._execute(h, raw)
        return h

    def _execute(self, h, raw):
        frm = Account.recover_transaction(raw).lower()
        d = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        call = {"from": frm, "to": "0x" + bytes(d["to"]).hex(), "data": "0x" + bytes(d["data"]).hex(),
                "maxFeePerGas": d["maxFeePerGas"], "maxPriorityFeePerGas": d["maxPriorityFeePerGas"]}
        gasprice = self._gasprice(call)
        self.block += 1
        if self.pre_execute:
            self.pre_execute(call)
        try:
            kind, logs = self._simulate(call, apply=True, tx_hash=h)
            status, used = "0x1", GAS[kind]
        except C.RpcError:
            kind, logs, status, used = "reverted", [], "0x0", 60_000
        fee = used * gasprice
        self.native[frm] = self.native.get(frm, 0) - fee
        if frm == self.provider:
            self.gas_burned += fee
        self.receipts[h] = {"status": status, "transactionHash": h, "blockNumber": hex(self.block), "gasUsed": hex(used),
                            "effectiveGasPrice": hex(gasprice), "logs": logs, "from": frm, "to": call["to"]}
        self.txs[h] = {"value": "0x0"}


def sign_auth(acct, contract, value, valid_after, valid_before, nonce):
    td = P.receive_auth_typed_data(acct.address, contract, value, valid_after, valid_before, nonce)
    return "0x" + acct.sign_message(encode_typed_data(full_message=td)).signature.hex()
