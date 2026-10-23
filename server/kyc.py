"""Optional compliant mode for HSK Meter sellers (Swift Bot Labs): pay only from addresses that hold a valid HashKey
Chain KYC SBT. Off unless the operator starts the seller with --require-kyc LEVEL.

The KYC SBT and its interface are documented at docs.hskchain.net (Build on HashKey Chain > Tools > KYC):
isHuman(address) -> (bool valid, uint8 level) and getKycInfo(address) -> (string ensName, uint8 level, uint8 status,
uint256 createTime); levels NONE 0, BASIC 1, ADVANCED 2, PREMIUM 3, ULTIMATE 4; statuses NONE 0, APPROVED 1, REVOKED 2.
The docs page does not print the mainnet address. KYC_SBT below is the mainnet address in the configuration of the
KYC provider's own demo app (github.com/hunyuan-kyc/next-kyc-demo, .env.example; the docs link that provider's
portal), checked on-chain before use: name() "KYC SBT", symbol() "KYC", suffix() ".hsk", the isHuman, getKycInfo,
requestKyc and getTotalFee selectors are in its code, both reads decode with the documented types, and its
resolver() is the KYC resolver address from the same file. The contract's source is not verified on the explorer,
so the operator can point --kyc-sbt at another address.

The decision is isHuman's own verdict (valid, and level >= the required level). getKycInfo is read only to explain a
refusal (no SBT, revoked, not valid any more, level too low). Reads only; results are cached for TTL_S per address.
"""
import threading
import time

from eth_abi import decode, encode
from eth_utils import keccak

KYC_SBT = "0x0f362c05fb3Fadca687648F412abE2A6d6450D70"
LEVELS = {"none": 0, "basic": 1, "advanced": 2, "premium": 3, "ultimate": 4}
LEVEL_NAMES = {v: k.upper() for k, v in LEVELS.items()}
STATUS_NAMES = {0: "NONE", 1: "APPROVED", 2: "REVOKED"}
TTL_S = 60
MAX_ENTRIES = 5000
_SEL_HUMAN = keccak(text="isHuman(address)")[:4]
_SEL_INFO = keccak(text="getKycInfo(address)")[:4]


class KycUnavailable(RuntimeError):
    """The SBT could not be read: callers refuse (fail closed) and say the chain is unavailable."""


def _call(rpc, sbt, sel, addr):
    try:
        res = rpc("eth_call", [{"to": sbt, "data": "0x" + (sel + encode(["address"], [addr])).hex()}, "latest"], tries=2)
    except Exception as e:
        raise KycUnavailable(type(e).__name__)
    if not isinstance(res, str) or not res.startswith("0x") or len(res) < 2 + 64 * 2 or len(res) > 2 + 64 * 40:
        raise KycUnavailable("unexpected answer")
    return bytes.fromhex(res[2:])


def is_human(rpc, addr, sbt=KYC_SBT):
    try:
        valid, level = decode(["bool", "uint8"], _call(rpc, sbt, _SEL_HUMAN, addr))
    except KycUnavailable:
        raise
    except Exception:
        raise KycUnavailable("malformed isHuman answer")
    return bool(valid), int(level)


def kyc_info(rpc, addr, sbt=KYC_SBT):
    try:
        ens, level, status, created = decode(["string", "uint8", "uint8", "uint256"], _call(rpc, sbt, _SEL_INFO, addr))
    except KycUnavailable:
        raise
    except Exception:
        raise KycUnavailable("malformed getKycInfo answer")
    return {"level": int(level), "status": int(status), "createTime": int(created), "hasName": bool(ens)}


class KycGate:
    def __init__(self, rpc, min_level, sbt=KYC_SBT, ttl_s=TTL_S, mono=time.monotonic):
        if not 1 <= int(min_level) <= 4:
            raise ValueError("KYC level must be 1 (basic) .. 4 (ultimate)")
        self.rpc, self.min_level, self.sbt, self.ttl, self.mono = rpc, int(min_level), sbt, ttl_s, mono
        self._cache = {}
        self._lock = threading.Lock()

    def describe(self):
        return {"required": True, "sbt": self.sbt, "minLevel": self.min_level, "minLevelName": LEVEL_NAMES[self.min_level],
                "check": "isHuman(payer) must return valid and level >= minLevel",
                "docs": "https://docs.hskchain.net/docs/Build-on-HashKey-Chain/Tools/KYC"}

    def check(self, addr):
        """(ok, detail) for `addr`; raises KycUnavailable when the SBT cannot be read (callers fail closed)."""
        a = addr.lower()
        with self._lock:
            c = self._cache.get(a)
            if c and self.mono() - c[1] < self.ttl:
                return c[0]
        valid, level = is_human(self.rpc, a, self.sbt)
        if valid and level >= self.min_level:
            res = (True, {"level": level, "levelName": LEVEL_NAMES.get(level, str(level))})
        else:
            info = kyc_info(self.rpc, a, self.sbt)
            if info["status"] == 0:
                why = "no KYC SBT for this address"
            elif info["status"] == 2:
                why = "the KYC SBT of this address is revoked"
            elif not valid:
                why = "the KYC SBT of this address is not valid any more"
            else:
                why = f"KYC level {LEVEL_NAMES.get(level, level)} is below the required {LEVEL_NAMES[self.min_level]}"
            res = (False, {"reason": why, "level": info["level"], "levelName": LEVEL_NAMES.get(info["level"], str(info["level"])),
                           "status": STATUS_NAMES.get(info["status"], str(info["status"]))})
        with self._lock:
            if len(self._cache) >= MAX_ENTRIES:
                self._cache.pop(next(iter(self._cache)))
            self._cache[a] = (res, self.mono())
        return res
