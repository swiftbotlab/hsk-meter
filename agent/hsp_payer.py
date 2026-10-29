#!/usr/bin/env python3
"""HSP payment agent for HSK Meter (Swift Bot Labs): pays a receiving agent through the HashKey Merchant (HSP) gateway.

The A2A flow, in the organizer's Track 2 clarification (2026-10-27): "the Payment Agent constructs a Payment
Mandate based on the payment conditions transmitted by the receiving Agent, completes the wallet signature, and then
verifies and executes the on-chain payment through the existing Gateway of HSP." Here:

  1. GET the paid URL with X-Payer. The receiving agent (server/hsp_merchant_agent.py) answers 402 with
     extensions["hsp-gateway"]: its signed Cart Mandate (contents + ES256K merchant_authorization), the gateway's
     payment_url / flow_id and payment_request_id.
  2. Verify the conditions BEFORE signing anything (evaluate()): the gateway origin is pinned; the merchant JWT
     verifies against the merchant key pinned out of band (a key in the offer is only compared, never trusted);
     cart_hash == SHA-256(canonical contents); the documented cart rules hold; the cart is for the resource we asked
     for; the chosen method_data row is in the documented token table, on an allowed network, paying an allowed
     payee; the amount is within the AP2-style constraints (payment.amount_range, payment.budget,
     payment.agent_recurrence) and reserved in a 0600 ledger first; the gateway's own copy of the order (public
     checkout read) must match the cart we were shown, when it can be read.
  3. Sign (sign_payment_mandate()): the wallet signs the EIP-712 EIP-3009 TransferWithAuthorization for the token's
     on-chain-verified domain (payer -> pay_to, the exact amount, a random nonce, 300 s validity), and the Payment
     Mandate is built in the wire format the hosted checkout uses: payment_response.details.payment_payload =
     base64 of an x402 v2 PaymentPayload {x402Version, payload {signature, authorization}, accepted, resource};
     user_authorization = HS256 JWT keyed by the payer address, aud "HashkeyMerchant", transaction_data =
     [sha256(canonical cart), sha256(canonical payment_mandate_contents)].
  4. Submit {flow_id, payment_mandate} to POST /api/v1/payment/pay-mandate (pay-mandate/multi-pay for reusable
     orders), poll GET /api/v1/payment/flow/{flow_id}?payment_details_id=..., then GET the URL again with X-HSP-Flow:
     the receiving agent serves only after the gateway's merchant API (or its signed webhook) shows the payment.

NOT IN THE DOCS (see HSP_STATUS.md, "Payer wire format"): the docs publish the merchant API only, plus the two
route names in their architecture diagram. The bodies above follow the hosted checkout's own client code as found in
public prior art, read as text. Tested against our mock gateway only; UNVERIFIED against a live gateway.
The cart hash in transaction_data covers the cart's contents (what that checkout code hashes; it equals the merchant
JWT's cart_hash); --cart-hash-scope cart_mandate hashes {contents, merchant_authorization} instead (what one
prior-art test script does). --sign-only signs and stores the mandate without submitting it.

  hsp_payer.py URL --key-file F --pin-merchant-key PEM --expect-payee 0x... [--max-usd 0.05] [--budget-usd 0.20]
               [--max-payments 20] [--network hashkey-testnet] [--gateway https://merchant-qa.hashkeymerchant.com]
               [--submit-path /api/v1/payment/pay-mandate] [--sign-only] [--cart-hash-scope contents|cart_mandate]
               [--allow-loopback] [--ledger F]
"""
import argparse
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
import hsp_gateway as G  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402

OFFER_VERSION = "hsp-gateway/1"
SUBMIT_PATH = G.PAYER_ROUTES["pay_mandate"]              # docs diagram names it; body from the checkout's code
SUBMIT_PATH_MULTI = G.PAYER_ROUTES["pay_mandate_multi"]
FLOW_PATH = G.PAYER_ROUTES["flow"]
LEDGER = os.path.join(ROOT, "ops", "state", "hsp_payer_ledger.json")
AUTH_VALID_S = 300                                       # the checkout's EIP-3009 window (validAfter = now)
X402_MAX_TIMEOUT_S = 3600                                # accepted.maxTimeoutSeconds, as the checkout sends it
JWT_AUDS = {"HashkeyMerchant", "HashKeyMerchant"}         # docs/Go SDK and Python/Node SDK spellings
GATEWAY_ERRORS = {10001: "invalid request", 30001: "invalid authorization", 30043: "quote updated, retry",
                  10005: "gateway server error"}         # codes the checkout code maps to messages
SPENDING = ("reserved", "signed", "submitted", "paid")    # ledger states that count against the budget
TRANSFER_TYPES = {
    "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                     {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
    "TransferWithAuthorization": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                                  {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                                  {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]}


class Refused(Exception):
    """The payer will not sign: the conditions break the policy or cannot be verified."""


class Policy:
    """Operator-set limits, expressed as AP2 Payment Mandate constraints (ap2-protocol.org, Payment Mandate)."""

    def __init__(self, *, merchants, payees, networks, max_payment_usd, budget_usd, max_payments=20,
                 gateways=("https://merchant-qa.hashkeymerchant.com",), checkout_hosts=(), max_fee_usd="0",
                 allow_mainnet=False, allow_loopback=False):
        self.merchants = {n: bytes(k) for n, k in merchants.items()}     # merchant_name -> pinned 64-byte key
        self.payees = {p.lower() for p in payees}
        self.networks = set(networks)
        self.max_payment = Decimal(str(max_payment_usd))
        self.budget = Decimal(str(budget_usd))
        self.max_payments = int(max_payments)
        self.max_fee = Decimal(str(max_fee_usd))
        self.allow_mainnet, self.allow_loopback = bool(allow_mainnet), bool(allow_loopback)
        self.gateways = {G.check_base_url(g, allow_loopback, allow_mainnet)[0] for g in gateways}
        self.checkout_hosts = {h.lower() for h in checkout_hosts}

    def constraints(self):
        return [{"type": "payment.allowed_payees", "allowed": sorted(self.payees)},
                {"type": "payment.amount_range", "currency": "USD", "max": str(self.max_payment)},
                {"type": "payment.budget", "currency": "USD", "max": str(self.budget)},
                {"type": "payment.agent_recurrence", "frequency": "ON_DEMAND", "max_occurrences": self.max_payments}]


class Ledger:
    """Spend ledger (0600 JSON): spent + reserved stay <= budget; one signature per flow (a retry re-uses it)."""

    def __init__(self, path=LEDGER):
        self.path, self.lock = path, threading.Lock()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return {"flows": {}}
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, d):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, self.path)

    def totals(self):
        d = self._load()
        used = sum(Decimal(v["usd"]) for v in d["flows"].values() if v["state"] in SPENDING)
        return used, sum(1 for v in d["flows"].values() if v["state"] in SPENDING), d

    def reserve(self, flow_id, usd, policy):
        with self.lock:
            used, count, d = self.totals()
            if flow_id in d["flows"]:
                return d["flows"][flow_id]            # already reserved/signed: never a second signature
            if count + 1 > policy.max_payments:
                raise Refused("payment.agent_recurrence: max_occurrences reached")
            if used + usd > policy.budget:
                raise Refused(f"payment.budget: {used} + {usd} USD would exceed {policy.budget} USD")
            d["flows"][flow_id] = {"usd": str(usd), "state": "reserved", "t": int(time.time())}
            self._save(d)
            return None

    def update(self, flow_id, **kv):
        with self.lock:
            d = self._load()
            d["flows"].setdefault(flow_id, {"usd": "0", "state": "reserved"}).update(kv)
            self._save(d)

    def get(self, flow_id):
        return self._load()["flows"].get(flow_id)


def pinned_gateway(offer, policy):
    """The offer's gateway origin, if it is one we pinned; else None (and nothing is fetched from it)."""
    try:
        gw, _ = G.check_base_url(str(offer.get("gateway", "")), policy.allow_loopback, policy.allow_mainnet)
    except (G.HspError, AttributeError, ValueError):
        return None
    return gw if gw in policy.gateways else None


def evaluate(offer, policy, *, expected_resource, now=None, checkout=None):
    """Every check before a signature. Returns the decision; raises Refused with the reason."""
    now = int(time.time()) if now is None else int(now)
    try:
        if not isinstance(offer, dict) or offer.get("version") != OFFER_VERSION:
            raise Refused("not an hsp-gateway/1 offer")
        gw, _ = G.check_base_url(str(offer.get("gateway", "")), policy.allow_loopback, policy.allow_mainnet)
        if gw not in policy.gateways:
            raise Refused("gateway is not one we pinned")
        url, flow_id = offer.get("payment_url"), offer.get("flow_id")
        u = urllib.parse.urlsplit(str(url))
        same = (u.scheme + "://" + (u.netloc or "")).lower() == gw.lower()
        if not (same or (u.scheme == "https" and (u.hostname or "").lower() in policy.checkout_hosts)):
            raise Refused("payment_url is not on the pinned gateway")
        if not isinstance(flow_id, str) or G.flow_id_from_url(url) != flow_id:
            raise Refused("flow_id is not the payment_url's flow")
        cm = offer.get("cart_mandate")
        if not isinstance(cm, dict) or set(cm) != {"contents", "merchant_authorization"}:
            raise Refused("cart_mandate must be {contents, merchant_authorization}")
        contents = cm["contents"]
        eff = G.validate_cart_contents(contents)
        name = contents["merchant_name"]
        if name not in policy.merchants:
            raise Refused("merchant is not one we pinned")
        pinned = policy.merchants[name]
        if offer.get("merchant_public_key") and G.parse_public_pem(offer["merchant_public_key"]) != pinned:
            raise Refused("merchant key in the offer differs from the pinned key")
        claims = G.jwt_verify(cm["merchant_authorization"], pinned)
        G.check_merchant_claims(claims, contents, aud=JWT_AUDS, now=now)
        if G.rfc3339_ts(contents["cart_expiry"]) < now + 60:
            raise Refused("cart expires in under 60 s")
        prid = contents["payment_request"]["details"]["id"]
        if offer.get("payment_request_id") != prid:
            raise Refused("payment_request_id differs from the cart")
        labels = [it["label"].lower() for it in contents["payment_request"]["details"].get("display_items") or []]
        if expected_resource.lower() not in labels:      # HSK Meter parameters are hex: compared case-insensitively
            raise Refused("the cart is not for the resource we asked for")
        choice, why = None, []
        for i, m in enumerate(contents["payment_request"]["method_data"]):
            d = m["data"]
            row = G.token_by_contract(d["chain_id"], d["contract_address"])
            if row is None or row["network"] != d["network"] or row["coin"] != d["coin"]:
                why.append(f"[{i}] not in the documented token table")
            elif row["protocol"] != "eip3009":
                why.append(f"[{i}] {row['protocol']} is not supported (spender undocumented)")
            elif row["eip712"] is None:
                why.append(f"[{i}] token EIP-712 domain not verified")
            elif row["network"] not in policy.networks:
                why.append(f"[{i}] network {row['network']} not allowed")
            elif row["mainnet"] and not policy.allow_mainnet:
                why.append(f"[{i}] mainnet refused (testnet-only payer)")
            elif d["pay_to"].lower() not in policy.payees:
                why.append(f"[{i}] payee not allowed")
            else:
                choice = (i, row, d["pay_to"])
                break
        if choice is None:
            raise Refused("no acceptable payment method: " + "; ".join(why))
        i, row, pay_to = choice
        usd = eff[i]
        if usd <= 0 or usd > policy.max_payment:
            raise Refused(f"payment.amount_range: {usd} USD is outside (0, {policy.max_payment}]")
        units = G.units_from_usd(format(usd, "f"), row["decimals"])
        multi_pay = offer.get("multi_pay") is True
        if checkout is not None:
            # the gateway's own copy of the order: the checkout route returns {cartmandate, multi_pay}, the Go SDK's
            # route {payment_request_id, cartmandate, status, amount}; whatever is present must agree with the offer
            if not isinstance(checkout, dict):
                raise Refused("gateway checkout order is not an object")
            gcm = checkout.get("cartmandate")
            gprid = checkout.get("payment_request_id")
            if gprid is None and isinstance(gcm, dict):
                gprid = (((gcm.get("contents") or {}).get("payment_request") or {}).get("details") or {}).get("id")
            if gprid != prid:
                raise Refused("gateway checkout order is for another payment request")
            if isinstance(gcm, dict) and isinstance(gcm.get("contents"), dict):
                if G.cart_hash(gcm["contents"]) != G.cart_hash(contents):
                    raise Refused("gateway's cart differs from the cart the receiving agent sent")
                if gcm.get("merchant_authorization") not in (None, cm["merchant_authorization"]):
                    raise Refused("gateway's merchant_authorization differs from the one the receiving agent sent")
            if checkout.get("status") not in (None, "payment-required"):
                raise Refused(f"gateway flow is not awaiting payment ({str(checkout.get('status'))[:40]})")
            if checkout.get("multi_pay") is not None:
                if not isinstance(checkout["multi_pay"], bool) or ("multi_pay" in offer and checkout["multi_pay"] != multi_pay):
                    raise Refused("gateway's multi_pay differs from the offer")
                multi_pay = checkout["multi_pay"]
            if checkout.get("amount") is not None:
                gamt = G.dec(str(checkout["amount"]), "checkout amount")
                if gamt < usd or gamt > usd + policy.max_fee:
                    raise Refused(f"gateway amount {gamt} is outside [{usd}, {usd + policy.max_fee}]")
                units = G.units_from_usd(format(gamt, "f"), row["decimals"])
                usd = gamt
        total = contents["payment_request"]["details"]["total"]
        method_name = contents["payment_request"]["method_data"][i]["supported_methods"]
    except (G.HspError, KeyError, TypeError) as e:
        raise Refused(f"conditions do not verify: {e}") from None
    return {"index": i, "token": row, "pay_to": pay_to, "units": units, "usd": usd, "flow_id": flow_id,
            "payment_request_id": prid, "merchant": name, "cart_hash": G.cart_hash(contents),
            "cart_mandate": {"contents": contents, "merchant_authorization": cm["merchant_authorization"]},
            "total": {"label": total["label"], "value": total["amount"]["value"], "pending": total.get("pending") is True},
            "method_name": method_name, "asset": contents["payment_request"]["method_data"][i]["data"]["contract_address"],
            "multi_pay": multi_pay, "resource": expected_resource,
            "gateway": gw, "expiry": int(G.rfc3339_ts(contents["cart_expiry"])), "constraints": policy.constraints()}


def sign_payment_mandate(decision, account, *, resource_url, now=None, valid_s=AUTH_VALID_S, cart_hash_scope="contents"):
    """The wallet signature (EIP-3009 TransferWithAuthorization) and the Payment Mandate around it, in the hosted
    checkout's wire format (see the module docstring). Field order follows the checkout's code; the hashes do not
    depend on it (canonical JSON)."""
    now = time.time() if now is None else float(now)
    inow = int(now)
    t = decision["token"]
    name, version = t["eip712"]
    auth = {"from": account.address, "to": decision["pay_to"], "value": str(decision["units"]),
            "validAfter": str(inow), "validBefore": str(min(inow + int(valid_s), decision["expiry"])),
            "nonce": "0x" + secrets.token_bytes(32).hex()}
    typed = {"types": TRANSFER_TYPES, "primaryType": "TransferWithAuthorization",
             "domain": {"name": name, "version": version, "chainId": t["chain_id"], "verifyingContract": t["contract"]},
             "message": {**auth, "value": int(auth["value"]), "validAfter": int(auth["validAfter"]),
                         "validBefore": int(auth["validBefore"]), "nonce": bytes.fromhex(auth["nonce"][2:])}}
    sig = "0x" + Account.sign_message(encode_typed_data(full_message=typed), account.key).signature.hex().removeprefix("0x")
    x402 = {"x402Version": 2,                                                  # an x402 v2 PaymentPayload
            "payload": {"signature": sig, "authorization": auth},
            "accepted": {"scheme": "exact", "network": f"eip155:{t['chain_id']}", "asset": decision["asset"],
                         "amount": str(decision["units"]), "payTo": decision["pay_to"],
                         "maxTimeoutSeconds": X402_MAX_TIMEOUT_S,
                         "extra": {"protocol": t["protocol"], "name": name, "version": version}},
            "resource": {"url": str(resource_url)[:400], "description": decision["resource"][:200],
                         "mimeType": "application/json"}}
    tot = decision["total"]
    contents = {
        "payment_mandate_id": decision["payment_request_id"],                  # ID4 = ID2
        "payment_details_id": G.payment_details_id(now),                       # ID5, generated by the payer
        "payment_details_total": {"label": tot["label"], "amount": {"value": tot["value"], "currency": "USD"},
                                  "pending": tot["pending"]},
        "payment_response": {"request_id": decision["payment_request_id"], "method_name": decision["method_name"],
                             "details": {"payment_payload": G.encode_payment_payload(x402)},
                             "shipping_address": None, "shipping_option": None, "payer_name": None,
                             "payer_email": None, "payer_phone": None},
        "merchant_agent": G.CHECKOUT_MERCHANT_AGENT,
        "timestamp": G.iso_ms(now)}
    claims = {"aud": G.CHECKOUT_AUD, "nonce": "0x" + secrets.token_bytes(32).hex(), "iat": inow,
              "exp": decision["expiry"], "iss": account.address,
              "transaction_data": G.transaction_data(decision["cart_mandate"], contents, cart_hash_scope)}
    return {"payment_mandate_contents": contents,
            "user_authorization": G.hs256_jwt(claims, account.address.encode("utf-8"))}


def payment_payload(mandate):
    """The decoded x402 PaymentPayload inside a Payment Mandate."""
    return G.decode_payment_payload(mandate["payment_mandate_contents"]["payment_response"]["details"]["payment_payload"])


def recover_transfer_signer(x402, token_row):
    """Who signed the EIP-3009 authorization in a decoded x402 PaymentPayload (used by the mock gateway and tests)."""
    a = x402["payload"]["authorization"]
    name, version = token_row["eip712"]
    typed = {"types": TRANSFER_TYPES, "primaryType": "TransferWithAuthorization",
             "domain": {"name": name, "version": version, "chainId": token_row["chain_id"],
                        "verifyingContract": token_row["contract"]},
             "message": {"from": a["from"], "to": a["to"], "value": int(a["value"]), "validAfter": int(a["validAfter"]),
                         "validBefore": int(a["validBefore"]), "nonce": bytes.fromhex(a["nonce"][2:])}}
    return Account.recover_message(encode_typed_data(full_message=typed), signature=x402["payload"]["signature"])


def canonical_resource(url):
    """The receiving agent's resource string: 'GET /path?param=value', path and query lower-cased (hex values)."""
    u = urllib.parse.urlsplit(url)
    return "GET " + (u.path + ("?" + u.query if u.query else "")).lower()


def delivery_message(flow_id, resource):
    return f"HSK Meter HSP delivery\nflow: {flow_id}\nresource: {resource}"


def delivery_proof(account, flow_id, resource):
    """EIP-191 signature by the paying wallet: the receiving agent serves only to the wallet that paid."""
    from eth_account.messages import encode_defunct
    sig = Account.sign_message(encode_defunct(text=delivery_message(flow_id, resource)), account.key).signature
    return "0x" + sig.hex().removeprefix("0x")


def choose_submit_path(gateway, explicit=None, multi_pay=False, sign_only=False):
    """The submission route the hosted checkout uses (pay-mandate, or pay-mandate/multi-pay for reusable orders);
    an explicit --submit-path overrides it (an /api/v1/payment/ path only); --sign-only submits nothing."""
    if sign_only:
        return None
    if explicit:
        if not explicit.startswith("/api/v1/payment/") or "?" in explicit or "#" in explicit or ".." in explicit:
            raise Refused("submit path must be an /api/v1/payment/ path")
        return explicit
    return SUBMIT_PATH_MULTI if multi_pay else SUBMIT_PATH


def submit(gateway, flow_id, mandate, path):
    """POST {flow_id, payment_mandate}; no auth (as the checkout sends it). Returns the envelope's data."""
    if not path.startswith("/api/v1/payment/") or "?" in path:
        raise Refused("submit path must be an /api/v1/payment/ path")
    body = G.compact_json({"flow_id": flow_id, "payment_mandate": mandate}).encode()   # JSON.stringify order
    data = G.http_json("POST", gateway + path, body=body)
    if data is None:
        data = {}
    if not isinstance(data, dict) or data.get("flow_id") not in (None, flow_id):
        raise Refused("gateway answered for another flow")
    return data


def flow_status(gateway, flow_id, payment_details_id=None, path=FLOW_PATH):
    """GET /api/v1/payment/flow/{id}[?payment_details_id=] (the checkout's status read); else the order read."""
    q = "?" + urllib.parse.urlencode({"payment_details_id": payment_details_id}) if payment_details_id else ""
    try:
        d = G.http_json("GET", gateway + path.format(flow_id=urllib.parse.quote(flow_id)) + q)
        if isinstance(d, dict) and d.get("status") in G.STATUS_RANK:
            return d
    except G.HspApiError:
        pass
    try:
        d = G.checkout_order(gateway, flow_id)
    except G.HspApiError:
        return {}
    return d if isinstance(d, dict) else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise urllib.error.HTTPError(a[0].full_url, a[2], "redirect refused", a[4], None)


OPENER = urllib.request.build_opener(_NoRedirect)


def http_get(url, headers, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "hsk-meter-hsp-payer/1", **headers})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, G.loads_strict(r.read(1 << 20))
    except urllib.error.HTTPError as e:
        try:
            return e.code, G.loads_strict(e.read(1 << 20))
        except G.HspError:
            return e.code, {}


def load_wallet(path):
    env = G.parse_env(G.read_secret_file(path, 4096))
    if set(env) != {"HKPW_BUYER_PK"}:
        raise Refused("key file must hold exactly HKPW_BUYER_PK")
    return Account.from_key(env["HKPW_BUYER_PK"])


def pay(url, *, account, policy, ledger, submit_path=None, sign_only=False, cart_hash_scope="contents",
        poll_s=60, poll_every=2.0):
    """One paid request through the HSP gateway. Returns a result dict (never prints secrets)."""
    resource = canonical_resource(url)
    st, body = http_get(url, {"X-Payer": account.address, "X-HSP": "gateway"})
    if st != 402:
        return {"ok": st == 200, "status": st, "body": body, "paid": False}
    offer = (body.get("extensions") or {}).get("hsp-gateway") if isinstance(body, dict) else None
    checkout = None
    if pinned_gateway(offer, policy):        # never contact a host the receiving agent named unless we pinned it
        try:
            checkout = G.checkout_order(offer["gateway"], offer["flow_id"])
        except (G.HspError, KeyError, TypeError):
            checkout = None                  # not readable: the cart checks still stand on their own
    decision = evaluate(offer, policy, expected_resource=resource, checkout=checkout)
    prior = ledger.reserve(decision["flow_id"], decision["usd"], policy)
    state = (prior or {}).get("state")
    if state == "paid":
        mandate = None
    elif prior and prior.get("mandate"):
        mandate = prior["mandate"]           # a retry re-uses the signature: same EIP-3009 nonce, never a 2nd payment
    else:
        mandate = sign_payment_mandate(decision, account, resource_url=url, cart_hash_scope=cart_hash_scope)
        ledger.update(decision["flow_id"], state="signed", mandate=mandate)
    out = {"flow_id": decision["flow_id"], "usd": str(decision["usd"]), "network": decision["token"]["network"],
           "constraints": decision["constraints"]}
    pdid = mandate["payment_mandate_contents"]["payment_details_id"] if mandate else (prior or {}).get("payment_details_id")
    if mandate is not None and state != "submitted":
        path = choose_submit_path(decision["gateway"], submit_path, decision["multi_pay"], sign_only)
        if not path:
            out.update(ok=False, paid=False, signed=True, payment_details_id=pdid,
                       note="Payment Mandate signed and stored, not submitted (--sign-only)")
            return out
        try:
            out["submit"] = submit(decision["gateway"], decision["flow_id"], mandate, path)
            ledger.update(decision["flow_id"], state="submitted", payment_details_id=pdid)
        except G.HspApiError as e:
            err = {"http": e.http_status, "code": e.code, "msg": e.msg, "meaning": GATEWAY_ERRORS.get(e.code)}
            if not (state == "signed" and e.http_status is not None):
                out.update(ok=False, paid=False, signed=True, payment_details_id=pdid, gateway_error=err)
                return out
            out["resubmit_error"] = err      # a retry of a mandate that may have arrived: the flow status decides
    deadline = time.time() + poll_s
    while True:
        s = flow_status(decision["gateway"], decision["flow_id"], pdid)
        if s.get("status") in ("payment-included", "payment-safe", "payment-finalized", "payment-failed"):
            break
        if time.time() > deadline:
            out.update(ok=False, paid=False, note="gateway did not report the payment in time")
            return out
        time.sleep(poll_every)
    out["gateway_status"] = s.get("status")
    if s.get("status") == "payment-failed":
        ledger.update(decision["flow_id"], state="failed")
        out.update(ok=False, paid=False)
        return out
    ledger.update(decision["flow_id"], state="paid", tx=s.get("tx_signature"))
    for _ in range(int(max(1, poll_s // 2))):
        st, body = http_get(url, {"X-Payer": account.address, "X-HSP-Flow": decision["flow_id"],
                                  "X-HSP-Proof": delivery_proof(account, decision["flow_id"], resource)})
        if st != 402 or not (isinstance(body, dict) and body.get("error") == "payment_pending"):
            break
        time.sleep(poll_every)
    out.update(ok=st == 200, paid=True, status=st, body=body, tx=s.get("tx_signature"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("url")
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--pin-merchant-key", required=True, help="the receiving agent's ES256K public key (PEM)")
    ap.add_argument("--merchant-name", default="Swift Bot Labs")
    ap.add_argument("--expect-payee", action="append", required=True)
    ap.add_argument("--network", action="append", default=None)
    ap.add_argument("--gateway", action="append", default=None)
    ap.add_argument("--checkout-host", action="append", default=[])
    ap.add_argument("--max-usd", default="0.05")
    ap.add_argument("--budget-usd", default="0.20")
    ap.add_argument("--max-fee-usd", default="0")
    ap.add_argument("--max-payments", type=int, default=20)
    ap.add_argument("--submit-path", default=None, help="override the checkout's pay-mandate route (/api/v1/payment/...)")
    ap.add_argument("--sign-only", action="store_true", help="sign and store the Payment Mandate, submit nothing")
    ap.add_argument("--cart-hash-scope", choices=G.CART_HASH_SCOPES, default="contents",
                    help="what transaction_data[0] hashes (see HSP_STATUS.md)")
    ap.add_argument("--allow-loopback", action="store_true", help="accept a gateway on 127.0.0.1 (the mock) only")
    ap.add_argument("--allow-remote-seller", action="store_true", help="pay a receiving agent that is not on loopback (https)")
    ap.add_argument("--ledger", default=LEDGER)
    a = ap.parse_args()
    su = urllib.parse.urlsplit(a.url)
    local = su.scheme == "http" and su.hostname in ("127.0.0.1", "localhost")
    if not (local or (a.allow_remote_seller and su.scheme == "https")):
        print(json.dumps({"ok": False, "error": "the receiving agent must be on loopback (or https with --allow-remote-seller)"}))
        return 2
    policy = Policy(merchants={a.merchant_name: G.parse_public_pem(open(a.pin_merchant_key).read())},
                    payees=a.expect_payee, networks=a.network or ["hashkey-testnet", "base-sepolia"],
                    max_payment_usd=a.max_usd, budget_usd=a.budget_usd, max_payments=a.max_payments,
                    gateways=a.gateway or ["https://merchant-qa.hashkeymerchant.com"], checkout_hosts=a.checkout_host,
                    max_fee_usd=a.max_fee_usd, allow_loopback=a.allow_loopback)
    try:
        out = pay(a.url, account=load_wallet(a.key_file), policy=policy, ledger=Ledger(a.ledger), submit_path=a.submit_path,
                  sign_only=a.sign_only, cart_hash_scope=a.cart_hash_scope)
    except Refused as e:
        out = {"ok": False, "refused": str(e)[:300], "signed": False}
    except G.HspApiError as e:
        out = {"ok": False, "gateway_error": {"http": e.http_status, "code": e.code, "msg": e.msg}}
    print(json.dumps(out, indent=1, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
