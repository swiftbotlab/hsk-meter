"""Signing firewall: the payer signs a gateway-supplied EIP-712 payload only if it is exactly what we expect.

The HSP gateway hands the payer an `eip712` object to sign. A compromised or buggy gateway could swap the
recipient, the token, the chain, the amount or the expiry; once signed, an EIP-3009 / Permit2 authorization is a
bearer instrument. So before signing we rebuild every expectation from OUR OWN state (the order we created, our
payer, our pay_to, the token row from get_chain_config, our caps) and refuse on any mismatch.

check() returns a Verdict with the NORMALISED typed data (ints as int, addresses checksummed). The caller signs that
normalised copy, so the bytes we verified are the bytes we sign (it hashes identically to the gateway's copy).
"""
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import config

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
B32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
UINT_RE = re.compile(r"^[0-9]{1,78}$")

DOMAIN_FIELD_TYPES = {"name": "string", "version": "string", "chainId": "uint256", "verifyingContract": "address"}
EIP3009_TYPE = [("from", "address"), ("to", "address"), ("value", "uint256"), ("validAfter", "uint256"),
                ("validBefore", "uint256"), ("nonce", "bytes32")]
PERMIT2_TYPE = [("permitted", "TokenPermissions"), ("spender", "address"), ("nonce", "uint256"),
                ("deadline", "uint256")]
TOKEN_PERMISSIONS_TYPE = [("token", "address"), ("amount", "uint256")]


class Refused(Exception):
    """The payload failed a firewall rule. .rule names it."""

    def __init__(self, rule, detail):
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


@dataclass(frozen=True)
class TokenRow:
    network: str
    chain_id: int
    coin: str
    address: str
    decimals: int
    protocol: str                 # eip3009 | permit2
    eip712_name: str = None       # if the config declares the token's EIP-712 domain
    eip712_version: str = None
    permit2_address: str = None   # if the config declares a Permit2 contract
    spenders: tuple = ()          # Permit2 spender/proxy/facilitator addresses the config declares
    raw: dict = field(default=None, compare=False, hash=False, repr=False)


@dataclass(frozen=True)
class Expectation:
    payer: str
    pay_to: str
    token: TokenRow
    order_usd: Decimal
    gas_fee_advanced: object = None      # True / False / None (unknown)
    payment_request_id: str = None
    per_payment_cap_usd: Decimal = config.PER_PAYMENT_CAP_USD
    price_slack: Decimal = config.PRICE_SLACK
    fee_allowance_usd: Decimal = config.FEE_ALLOWANCE_USD
    extra_permit2_spenders: tuple = ()   # operator-pinned, after review


@dataclass
class Verdict:
    typed_data: dict
    token_total_raw: int
    valid_before: int
    summary: dict


def _addr(v, what):
    if not isinstance(v, str) or not ADDR_RE.match(v):
        raise Refused("malformed", f"{what} is not an address")
    return v.lower()


def _uint(v, what):
    if isinstance(v, bool):
        raise Refused("malformed", f"{what} is a bool")
    if isinstance(v, int):
        if v < 0 or v >= 2 ** 256:
            raise Refused("malformed", f"{what} out of uint256 range")
        return v
    if isinstance(v, str) and UINT_RE.match(v):
        n = int(v)
        if n >= 2 ** 256:
            raise Refused("malformed", f"{what} out of uint256 range")
        return n
    raise Refused("malformed", f"{what} is not a decimal uint")


def _checksum(a):
    from eth_utils import to_checksum_address
    return to_checksum_address(a)


def _rfc3339(s):
    if not isinstance(s, str) or len(s) > 40:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def raw_units(usd, decimals, rounding=ROUND_FLOOR):
    return int((Decimal(usd) * (Decimal(10) ** decimals)).to_integral_value(rounding=rounding))


def _type_list(types, name):
    t = types.get(name)
    if not isinstance(t, list):
        raise Refused("types", f"missing struct {name}")
    out = []
    for f in t:
        if not isinstance(f, dict) or set(f) != {"name", "type"}:
            raise Refused("types", f"bad field in {name}")
        out.append((f["name"], f["type"]))
    return out


def check(prepared, exp, *, now=None, quote_expires_at=None):
    """Validate prepare_payment's output against our expectation. Returns Verdict or raises Refused."""
    now = int(time.time() if now is None else now)
    tok = exp.token
    if not isinstance(prepared, dict):
        raise Refused("malformed", "prepare_payment output is not an object")
    td = prepared.get("eip712")
    if not isinstance(td, dict) or set(td) - {"domain", "types", "primaryType", "message"}:
        raise Refused("malformed", "eip712 must have exactly domain/types/primaryType/message")
    domain, types, primary, msg = td.get("domain"), td.get("types"), td.get("primaryType"), td.get("message")
    if not isinstance(domain, dict) or not isinstance(types, dict) or not isinstance(msg, dict):
        raise Refused("malformed", "eip712 parts have wrong types")

    # --- our own invariants, independent of the payload ---
    payer, pay_to = _addr(exp.payer, "our payer"), _addr(exp.pay_to, "our pay_to")
    if payer == pay_to:
        raise Refused("payer_is_payee", "payer must not equal pay_to")
    if tok.chain_id not in config.TESTNET_CHAIN_IDS:
        raise Refused("mainnet", f"chain {tok.chain_id} is not an allowed testnet (QA wallets are testnet-only)")
    if tok.coin.upper() not in config.USD_STABLECOINS:
        raise Refused("token", f"{tok.coin} is not a USD stablecoin; the USD cap cannot be enforced")
    if not (0 <= tok.decimals <= 36):
        raise Refused("token", "implausible decimals")
    order_usd = Decimal(exp.order_usd)
    if order_usd <= 0 or order_usd > exp.per_payment_cap_usd:
        raise Refused("cap", f"order amount {order_usd} outside (0, {exp.per_payment_cap_usd}]")

    # --- top-level echo fields from prepare_payment must match what we asked for ---
    for k, ours in (("payer", payer), ("pay_to", pay_to)):
        if prepared.get(k) is not None and _addr(prepared.get(k), k) != ours:
            raise Refused(k, f"prepare_payment.{k} differs from ours")
    if exp.payment_request_id and prepared.get("payment_request_id") not in (None, exp.payment_request_id):
        raise Refused("payment_request_id", "prepare_payment refers to a different payment request")
    if "token_total_raw" not in prepared:
        raise Refused("amount", "token_total_raw missing")
    total_raw = _uint(prepared.get("token_total_raw"), "token_total_raw")

    # --- domain ---
    dtypes = types.get("EIP712Domain")
    if dtypes is not None:
        dl = _type_list(types, "EIP712Domain")
        if any(DOMAIN_FIELD_TYPES.get(n) != t for n, t in dl) or {n for n, _ in dl} != set(domain):
            raise Refused("domain", "EIP712Domain fields do not match the domain object")
    if set(domain) - set(DOMAIN_FIELD_TYPES) or not {"chainId", "verifyingContract"} <= set(domain):
        raise Refused("domain", f"unexpected domain fields {sorted(domain)}")
    chain_id = _uint(domain.get("chainId"), "domain.chainId")
    if chain_id != tok.chain_id:
        raise Refused("chain", f"domain.chainId {chain_id} != config chain {tok.chain_id}")
    vc = _addr(domain.get("verifyingContract"), "domain.verifyingContract")
    for k in ("name", "version"):
        if k in domain and not isinstance(domain[k], str):
            raise Refused("domain", f"domain.{k} must be a string")

    # --- validity window ---
    qexp = quote_expires_at if quote_expires_at is not None else _rfc3339(prepared.get("quote_expires_at"))
    upper = (qexp + config.VALID_BEFORE_SLACK_S) if qexp else (now + config.FALLBACK_MAX_VALID_S)

    def window(v, what):
        if v < now + config.MIN_TIME_TO_SUBMIT_S:
            raise Refused("expiry", f"{what} {v} leaves less than {config.MIN_TIME_TO_SUBMIT_S}s to submit")
        if v > upper:
            raise Refused("expiry", f"{what} {v} outlives the quote window (max {upper})")
        return v

    # --- amount bound from OUR order, never from the payload ---
    slack = Decimal(1) + exp.price_slack
    fee = Decimal(0) if exp.gas_fee_advanced is True else exp.fee_allowance_usd
    bound_raw = raw_units(order_usd * slack + fee, tok.decimals, ROUND_CEILING)
    cap_raw = raw_units(exp.per_payment_cap_usd, tok.decimals, ROUND_FLOOR)
    if total_raw <= 0:
        raise Refused("amount", "token_total_raw must be positive")
    if total_raw > cap_raw:
        raise Refused("cap", f"token_total_raw {total_raw} exceeds the per-payment cap {cap_raw}")
    if total_raw > bound_raw:
        raise Refused("amount", f"token_total_raw {total_raw} exceeds what our order allows ({bound_raw})")

    norm_domain = {}
    for k in domain:
        if k == "chainId":
            norm_domain[k] = chain_id
        elif k == "verifyingContract":
            norm_domain[k] = _checksum(vc)
        else:
            norm_domain[k] = domain[k]
    norm_types = {"EIP712Domain": [{"name": n, "type": DOMAIN_FIELD_TYPES[n]} for n in DOMAIN_FIELD_TYPES
                                   if n in domain]}

    if tok.protocol == "eip3009":
        if primary != "TransferWithAuthorization":
            raise Refused("primaryType", f"{primary!r} is not TransferWithAuthorization (eip3009)")
        if set(types) - {"EIP712Domain", "TransferWithAuthorization"}:
            raise Refused("types", f"unexpected struct types {sorted(set(types) - {'EIP712Domain'})}")
        if _type_list(types, "TransferWithAuthorization") != EIP3009_TYPE:
            raise Refused("types", "TransferWithAuthorization fields are not the EIP-3009 definition")
        if vc != tok.address.lower():
            raise Refused("token", "domain.verifyingContract is not the configured token contract")
        if tok.eip712_name is not None and domain.get("name") != tok.eip712_name:
            raise Refused("domain", "domain.name differs from the configured token EIP-712 name")
        if tok.eip712_version is not None and domain.get("version") != tok.eip712_version:
            raise Refused("domain", "domain.version differs from the configured token EIP-712 version")
        if set(msg) != {n for n, _ in EIP3009_TYPE}:
            raise Refused("message", f"message fields {sorted(msg)} are not the EIP-3009 set")
        if _addr(msg.get("from"), "message.from") != payer:
            raise Refused("payer", "message.from is not our payer")
        if _addr(msg.get("to"), "message.to") != pay_to:
            raise Refused("recipient", "message.to is not our pay_to")
        value = _uint(msg.get("value"), "message.value")
        if value != total_raw:
            raise Refused("amount", f"message.value {value} != token_total_raw {total_raw}")
        va = _uint(msg.get("validAfter"), "message.validAfter")
        vb = window(_uint(msg.get("validBefore"), "message.validBefore"), "validBefore")
        if va > now + 60:
            raise Refused("expiry", "validAfter is in the future")
        if va >= vb:
            raise Refused("expiry", "validAfter >= validBefore")
        if not isinstance(msg.get("nonce"), str) or not B32_RE.match(msg["nonce"]):
            raise Refused("message", "nonce is not bytes32 hex")
        norm_types["TransferWithAuthorization"] = [{"name": n, "type": t} for n, t in EIP3009_TYPE]
        norm_msg = {"from": _checksum(payer), "to": _checksum(pay_to), "value": value, "validAfter": va,
                    "validBefore": vb, "nonce": msg["nonce"]}
        recipient, deadline = pay_to, vb
    elif tok.protocol == "permit2":
        if primary != "PermitTransferFrom":
            raise Refused("primaryType", f"{primary!r} is not PermitTransferFrom (permit2)")
        if set(types) - {"EIP712Domain", "PermitTransferFrom", "TokenPermissions"}:
            raise Refused("types", f"unexpected struct types {sorted(set(types) - {'EIP712Domain'})}")
        if _type_list(types, "PermitTransferFrom") != PERMIT2_TYPE or \
                _type_list(types, "TokenPermissions") != TOKEN_PERMISSIONS_TYPE:
            raise Refused("types", "Permit2 struct fields are not the canonical definition")
        p2 = (tok.permit2_address or config.CANONICAL_PERMIT2).lower()
        if vc != p2:
            raise Refused("token", "domain.verifyingContract is not the Permit2 contract the config declares")
        if "name" in domain and domain["name"] != "Permit2":
            raise Refused("domain", "Permit2 domain name must be 'Permit2'")
        if "version" in domain:
            raise Refused("domain", "Permit2 domain has no version")
        if set(msg) != {"permitted", "spender", "nonce", "deadline"} or not isinstance(msg.get("permitted"), dict) \
                or set(msg["permitted"]) != {"token", "amount"}:
            raise Refused("message", "Permit2 message fields are not the canonical set")
        if _addr(msg["permitted"].get("token"), "permitted.token") != tok.address.lower():
            raise Refused("token", "permitted.token is not the configured token")
        amount = _uint(msg["permitted"].get("amount"), "permitted.amount")
        if amount != total_raw:
            raise Refused("amount", f"permitted.amount {amount} != token_total_raw {total_raw}")
        spender = _addr(msg.get("spender"), "message.spender")
        allowed = {pay_to} | {a.lower() for a in tok.spenders} | {a.lower() for a in exp.extra_permit2_spenders}
        if spender not in allowed:
            raise Refused("recipient", "Permit2 spender is neither our pay_to nor a spender the config declares")
        nonce = _uint(msg.get("nonce"), "message.nonce")
        dl = window(_uint(msg.get("deadline"), "message.deadline"), "deadline")
        norm_types["PermitTransferFrom"] = [{"name": n, "type": t} for n, t in PERMIT2_TYPE]
        norm_types["TokenPermissions"] = [{"name": n, "type": t} for n, t in TOKEN_PERMISSIONS_TYPE]
        norm_msg = {"permitted": {"token": _checksum(tok.address.lower()), "amount": amount},
                    "spender": _checksum(spender), "nonce": nonce, "deadline": dl}
        recipient, deadline = spender, dl
    else:
        raise Refused("protocol", f"unsupported protocol {tok.protocol!r}")

    # --- the x402 payment_payload the gateway will broadcast must agree with what we sign (when present) ---
    pp = prepared.get("payment_payload")
    acc = pp.get("accepted") if isinstance(pp, dict) else None
    if isinstance(acc, dict):
        if acc.get("payTo") is not None and _addr(acc["payTo"], "accepted.payTo") != pay_to:
            raise Refused("recipient", "payment_payload.accepted.payTo is not our pay_to")
        if acc.get("asset") is not None and _addr(acc["asset"], "accepted.asset") != tok.address.lower():
            raise Refused("token", "payment_payload.accepted.asset is not the configured token")
        if acc.get("amount") is not None and _uint(acc["amount"], "accepted.amount") != total_raw:
            raise Refused("amount", "payment_payload.accepted.amount != token_total_raw")
        net = acc.get("network")
        if isinstance(net, str) and net.startswith("eip155:") and net != f"eip155:{tok.chain_id}":
            raise Refused("chain", "payment_payload.accepted.network is another chain")
    auth = pp.get("payload", {}).get("authorization") if isinstance(pp, dict) and isinstance(
        pp.get("payload"), dict) else None
    if isinstance(auth, dict) and tok.protocol == "eip3009":
        for k in ("from", "to"):
            if auth.get(k) is not None and _addr(auth[k], f"authorization.{k}") != norm_msg[k].lower():
                raise Refused("recipient" if k == "to" else "payer", f"payment_payload authorization.{k} differs")
        for k in ("value", "validBefore", "validAfter"):
            if auth.get(k) is not None and _uint(auth[k], f"authorization.{k}") != norm_msg[k]:
                raise Refused("amount" if k == "value" else "expiry", f"payment_payload authorization.{k} differs")
        if auth.get("nonce") is not None and str(auth["nonce"]).lower() != norm_msg["nonce"].lower():
            raise Refused("message", "payment_payload authorization.nonce differs")

    typed = {"types": norm_types, "domain": norm_domain, "primaryType": primary, "message": norm_msg}
    summary = {"protocol": tok.protocol, "network": tok.network, "chain_id": chain_id, "token": tok.address,
               "coin": tok.coin, "payer": payer, "recipient": recipient, "token_total_raw": str(total_raw),
               "amount_human": str(Decimal(total_raw) / (Decimal(10) ** tok.decimals)),
               "valid_until": deadline, "bound_raw": str(bound_raw), "cap_raw": str(cap_raw)}
    return Verdict(typed_data=typed, token_total_raw=total_raw, valid_before=deadline, summary=summary)
