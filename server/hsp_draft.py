"""HSP-shaped mandate / receipt / verifier for the hsk-paywall settlement rail (Swift Bot Labs). DRAFT.

What this is, exactly
  The public HSP developer guide (HashkeyHSK/documentation, contents/docs/HSP/index.mdx, read 2026-10-23) defines the
  model: a payer-signed EIP-712 MANDATE ("pay this much of this token to this recipient on this chain, meeting these
  requirements"), a RECEIPT signed by a settlement ADAPTER ("I observed the settlement that satisfies this mandate"),
  optional ATTESTATIONS, and one verifier rule: ACCEPT iff requiredCapabilities is a subset of satisfiedCapabilities,
  with outcome classes ACCEPT / RETRYABLE / POLICY / PERMANENT and paymentId == mandateHash. Section 7 of the guide
  invites custom settlement adapters ("a new rail, a new proof") built with @hsp/devkit.
  The guide does NOT publish the EIP-712 type definitions, the receipt encoding or the SDK (the SDK repo
  github.com/project-hsp/hsp returned 404 and the sandbox Coordinator had no DNS on 2026-10-23). So the types below
  are OUR draft encoding of the documented fields, not the official @hsp/core wire format.
What is implemented
  - Mandate (EIP-712, signed by the payer's wallet) bound to one paywall request (reference = requestId).
  - Receipt (EIP-712, signed by our adapter key) produced only after the Paid event is observed on HashKey Chain.
  - verify(): signature checks, field-by-field mandate/receipt match, optional on-chain re-check, the subset rule.
  - Coordinator client: endpoints from the guide (section 9), DISABLED until the organizer shares the URL + API key.
TODO when the SDK / sandbox is published
  - TODO(hsp-sdk): replace MANDATE_TYPES / RECEIPT_TYPES with @hsp/core's definitions; keep this module as the adapter.
  - TODO(hsp-devkit): run the devkit conformance suite against this adapter; register (adapterId, instanceKey,
    signing address, reorgPolicy) with the organizer.
  - TODO(hsp-coordinator): POST /payments + /payments/:id/observe with HSP_COORDINATOR_URL / HSP_API_KEY.
"""
import json
import os
import re
import time
import urllib.request

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

DRAFT_ID = "hsp-draft/swiftbotlab-0"
ADAPTER_ID = "swiftbotlab/hsk-paywall-contract-rail@0"
CAP_SETTLED = "proves:settlement-verified"
CAP_REFUNDABLE = "proves:refund-path"          # the rail lets the provider refund on-chain (contract escrow credit)
CAP_RE = re.compile(r"^(proves|attests):[a-z0-9-]{1,40}(:[a-z0-9.-]{1,16})?$")

DOMAIN = {"name": "HSP Mandate (draft)", "version": "0-swiftbotlab"}
MANDATE_TYPES = {
    "Mandate": [
        {"name": "payer", "type": "address"}, {"name": "payee", "type": "address"},
        {"name": "token", "type": "address"}, {"name": "amount", "type": "uint256"},
        {"name": "reference", "type": "bytes32"}, {"name": "requiredCapabilities", "type": "string"},
        {"name": "deadline", "type": "uint256"},
    ]
}
RECEIPT_TYPES = {
    "Receipt": [
        {"name": "mandateHash", "type": "bytes32"}, {"name": "adapterId", "type": "string"},
        {"name": "txHash", "type": "bytes32"}, {"name": "logIndex", "type": "uint256"},
        {"name": "settlementContract", "type": "address"}, {"name": "payer", "type": "address"},
        {"name": "payee", "type": "address"}, {"name": "token", "type": "address"},
        {"name": "amount", "type": "uint256"}, {"name": "blockNumber", "type": "uint256"},
        {"name": "satisfiedCapabilities", "type": "string"}, {"name": "observedAt", "type": "uint256"},
    ]
}
EIP712_DOMAIN = [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                 {"name": "chainId", "type": "uint256"}]


def caps_string(caps):
    caps = sorted(set(caps))
    for c in caps:
        if not CAP_RE.match(c):
            raise ValueError(f"bad capability {c!r}")
    return ",".join(caps)


def caps_list(s):
    if not s:
        return []
    if not isinstance(s, str) or len(s) > 400:
        raise ValueError("capabilities must be a comma-separated string (<= 400 chars)")
    return [c for c in s.split(",") if c]


_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_B32 = re.compile(r"^0x[0-9a-fA-F]{64}$")
_UINT = re.compile(r"^(0|[1-9][0-9]{0,29})$")


def _field(j, k, pattern):
    v = j.get(k)
    if isinstance(v, int) and not isinstance(v, bool) and pattern is _UINT:
        v = str(v)
    if not isinstance(v, str) or not pattern.match(v):
        raise ValueError(f"mandate.{k} is malformed")
    return v


def _typed(types, primary, message, chain_id):
    return {"types": {"EIP712Domain": EIP712_DOMAIN, **types}, "primaryType": primary,
            "domain": {**DOMAIN, "chainId": int(chain_id)}, "message": message}


def mandate_message(*, payer, payee, token, amount, reference, required_caps, deadline):
    return {"payer": to_checksum_address(payer), "payee": to_checksum_address(payee),
            "token": to_checksum_address(token), "amount": int(amount),
            "reference": bytes.fromhex(reference[2:]), "requiredCapabilities": caps_string(required_caps),
            "deadline": int(deadline)}


def mandate_to_json(m):
    return {**m, "amount": str(m["amount"]), "deadline": str(m["deadline"]), "reference": "0x" + m["reference"].hex()}


def mandate_from_json(j):
    """Strict parse of a JSON mandate (every field type- and pattern-checked; raises ValueError only)."""
    if not isinstance(j, dict):
        raise ValueError("mandate must be an object")
    return mandate_message(payer=_field(j, "payer", _ADDR), payee=_field(j, "payee", _ADDR), token=_field(j, "token", _ADDR),
                           amount=int(_field(j, "amount", _UINT)), reference=_field(j, "reference", _B32),
                           required_caps=caps_list(j.get("requiredCapabilities")), deadline=int(_field(j, "deadline", _UINT)))


def mandate_hash(m, chain_id):
    sm = encode_typed_data(full_message=_typed(MANDATE_TYPES, "Mandate", m, chain_id))
    return "0x" + keccak(b"\x19" + sm.version + sm.header + sm.body).hex()


def sign_mandate(acct, m, chain_id):
    return "0x" + acct.sign_message(encode_typed_data(full_message=_typed(MANDATE_TYPES, "Mandate", m, chain_id))).signature.hex()


def recover_mandate(m, sig, chain_id):
    return Account.recover_message(encode_typed_data(full_message=_typed(MANDATE_TYPES, "Mandate", m, chain_id)),
                                   signature=bytes.fromhex(sig[2:]))


def receipt_message(*, mandate_hash_, tx_hash, log_index, contract, payer, payee, token, amount, block, caps, observed_at):
    return {"mandateHash": bytes.fromhex(mandate_hash_[2:]), "adapterId": ADAPTER_ID,
            "txHash": bytes.fromhex(tx_hash[2:]), "logIndex": int(log_index),
            "settlementContract": to_checksum_address(contract), "payer": to_checksum_address(payer),
            "payee": to_checksum_address(payee), "token": to_checksum_address(token), "amount": int(amount),
            "blockNumber": int(block), "satisfiedCapabilities": caps_string(caps), "observedAt": int(observed_at)}


def receipt_to_json(r):
    out = dict(r)
    for k in ("mandateHash", "txHash"):
        out[k] = "0x" + r[k].hex()
    for k in ("logIndex", "amount", "blockNumber", "observedAt"):
        out[k] = str(r[k])
    return out


def receipt_from_json(j):
    return receipt_message(mandate_hash_=j["mandateHash"], tx_hash=j["txHash"], log_index=int(j["logIndex"]),
                           contract=j["settlementContract"], payer=j["payer"], payee=j["payee"], token=j["token"],
                           amount=int(j["amount"]), block=int(j["blockNumber"]),
                           caps=caps_list(j["satisfiedCapabilities"]), observed_at=int(j["observedAt"]))


def sign_receipt(acct, r, chain_id):
    return "0x" + acct.sign_message(encode_typed_data(full_message=_typed(RECEIPT_TYPES, "Receipt", r, chain_id))).signature.hex()


def recover_receipt(r, sig, chain_id):
    return Account.recover_message(encode_typed_data(full_message=_typed(RECEIPT_TYPES, "Receipt", r, chain_id)),
                                   signature=bytes.fromhex(sig[2:]))


def verify(*, mandate, mandate_sig, receipt, receipt_sig, chain_id, pinned_adapter, now=None, onchain=None):
    """The relying party's decision. Returns {"outcomeClass", "ok", "reasons", "paymentId"}.
    onchain: optional callable(receipt) -> (ok: bool, retryable: bool, reason: str) that re-reads the chain."""
    now = int(now if now is not None else time.time())
    reasons = []
    try:
        mh = mandate_hash(mandate, chain_id)
        signer = recover_mandate(mandate, mandate_sig, chain_id)
        adapter = recover_receipt(receipt, receipt_sig, chain_id)
    except Exception as e:  # malformed input
        return {"outcomeClass": "PERMANENT", "ok": False, "reasons": [f"malformed: {e}"[:200]], "paymentId": None}
    if signer.lower() != mandate["payer"].lower():
        reasons.append("mandate not signed by its payer")
    if adapter.lower() != pinned_adapter.lower():
        reasons.append("receipt not signed by the pinned adapter")
    if "0x" + receipt["mandateHash"].hex() != mh:
        reasons.append("receipt is for another mandate")
    for k in ("payer", "payee", "token", "amount"):
        if str(receipt[k]).lower() != str(mandate[k]).lower():
            reasons.append(f"receipt.{k} != mandate.{k}")
    if reasons:
        return {"outcomeClass": "PERMANENT", "ok": False, "reasons": reasons, "paymentId": mh}
    if receipt["observedAt"] > mandate["deadline"]:
        return {"outcomeClass": "POLICY", "ok": False, "reasons": ["settled after the mandate deadline"], "paymentId": mh}
    required = set(caps_list(mandate["requiredCapabilities"]))
    satisfied = set(caps_list(receipt["satisfiedCapabilities"]))
    if not required <= satisfied:
        return {"outcomeClass": "POLICY", "ok": False, "paymentId": mh,
                "reasons": ["missing capabilities: " + ",".join(sorted(required - satisfied))]}
    if onchain is not None:
        ok, retryable, why = onchain(receipt)
        if not ok:
            return {"outcomeClass": "RETRYABLE" if retryable else "PERMANENT", "ok": False, "reasons": [why], "paymentId": mh}
    return {"outcomeClass": "ACCEPT", "ok": True, "reasons": [], "paymentId": mh}


class Coordinator:
    """Client for the hosted HSP Coordinator (guide section 9). DISABLED until the organizer shares
    HSP_COORDINATOR_URL and HSP_API_KEY (TODO(hsp-coordinator)); every call raises NotConfigured until then."""

    class NotConfigured(RuntimeError):
        pass

    def __init__(self, url=None, api_key=None, timeout=10):
        self.url = url or os.environ.get("HSP_COORDINATOR_URL")
        self.key = api_key or os.environ.get("HSP_API_KEY")
        self.timeout = timeout

    def _req(self, method, path, body=None, write=False):
        if not self.url or (write and not self.key):
            raise Coordinator.NotConfigured("HSP Coordinator not configured (sandbox credentials pending)")
        if not self.url.startswith("https://"):
            raise Coordinator.NotConfigured("Coordinator URL must be https")
        headers = {"content-type": "application/json", "user-agent": "swiftbotlab-hsk-paywall/1.0"}
        if write:
            headers["authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.url.rstrip("/") + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read(1_000_001)[:1_000_000])

    def register_payment(self, mandate_json, signature, attestations=()):   # POST /payments
        return self._req("POST", "/payments", {"mandate": mandate_json, "signature": signature,
                                                "attestations": list(attestations)}, write=True)

    def observe(self, payment_id, tx_hash):                                # POST /payments/:id/observe
        return self._req("POST", f"/payments/{payment_id}/observe", {"txHash": tx_hash}, write=True)

    def status(self, payment_id):                                          # GET /payments/:id
        return self._req("GET", f"/payments/{payment_id}")

    def chains(self):                                                       # GET /chains (adapter address to pin)
        return self._req("GET", "/chains")
