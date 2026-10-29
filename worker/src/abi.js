/* Minimal, strict EVM helpers for the HSK Meter Worker (Swift Bot Labs): hex, keccak256, static ABI words.
   Only the static types this service needs (address, bool, uintN, bytes32). Anything malformed throws. */
import { keccak_256 } from '@noble/hashes/sha3.js';

export const ADDR_RE = /^0x[0-9a-fA-F]{40}$/;
export const B32_RE = /^0x[0-9a-fA-F]{64}$/;
export const HEX_RE = /^0x([0-9a-fA-F]{2})*$/;
export const ZERO = '0x' + '0'.repeat(40);

export class AbiError extends Error {}

export function hexToBytes(h) {
  if (typeof h !== 'string' || !HEX_RE.test(h)) throw new AbiError('bad hex');
  const out = new Uint8Array((h.length - 2) / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(2 + 2 * i, 2), 16);
  return out;
}

export function bytesToHex(b) {
  let s = '0x';
  for (let i = 0; i < b.length; i++) s += b[i].toString(16).padStart(2, '0');
  return s;
}

export function concat(...parts) {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Uint8Array(n);
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

export function keccak(bytes) { return keccak_256(bytes); }
export function keccakText(s) { return keccak_256(new TextEncoder().encode(s)); }
export function keccakHex(s) { return bytesToHex(keccakText(s)); }
export function selector(sig) { return keccakText(sig).slice(0, 4); }

/* one 32-byte ABI word */
export function wordUint(v) {
  let x = BigInt(v);
  if (x < 0n || x >= (1n << 256n)) throw new AbiError('uint out of range');
  const out = new Uint8Array(32);
  for (let i = 31; i >= 0; i--) { out[i] = Number(x & 0xffn); x >>= 8n; }
  return out;
}
export function wordAddress(a) {
  if (!ADDR_RE.test(a)) throw new AbiError('bad address');
  const out = new Uint8Array(32);
  out.set(hexToBytes(a.toLowerCase()), 12);
  return out;
}
export function wordBytes32(h) {
  if (!B32_RE.test(h)) throw new AbiError('bad bytes32');
  return hexToBytes(h.toLowerCase());
}

/* calldata = selector || static words. args: [['uint256', v], ['address', a], ['bytes32', h]] */
export function calldata(sig, args) {
  const words = (args || []).map(([t, v]) => {
    if (t === 'address') return wordAddress(v);
    if (t === 'bytes32') return wordBytes32(v);
    if (/^uint\d+$/.test(t)) return wordUint(v);
    throw new AbiError('unsupported type ' + t);
  });
  return bytesToHex(concat(selector(sig), ...words));
}

/* decode static words of `types` from return data (hex). uintN -> BigInt, address -> lowercase hex, bool -> boolean,
   bytes32 -> lowercase hex. Values must be canonical (an address word with dirty high bytes is refused). */
export function decodeWords(types, hex) {
  const b = hexToBytes(hex);
  if (b.length < 32 * types.length) throw new AbiError('return data too short');
  return types.map((t, i) => {
    const w = b.subarray(32 * i, 32 * i + 32);
    const big = BigInt(bytesToHex(w));
    if (t === 'address') {
      if (big >> 160n) throw new AbiError('dirty address word');
      return bytesToHex(w.subarray(12));
    }
    if (t === 'bool') {
      if (big > 1n) throw new AbiError('dirty bool word');
      return big === 1n;
    }
    if (t === 'bytes32') return bytesToHex(w);
    const m = /^uint(\d+)$/.exec(t);
    if (m) {
      if (big >> BigInt(Number(m[1]))) throw new AbiError('uint word out of range');
      return big;
    }
    throw new AbiError('unsupported type ' + t);
  });
}

export function hexToBigInt(h) {
  if (typeof h !== 'string' || !/^0x[0-9a-fA-F]{1,64}$/.test(h)) throw new AbiError('bad quantity');
  return BigInt(h);
}

/* ---- number formatting identical to the Python seller's f"{...:.6f}" (round-half-even on the double's exact value) */
function bitLen(x) { return x === 0n ? 0 : x.toString(2).length; }

/* Python's format(d, ".6f") for a finite double d >= 0: the exact binary value, rounded half to even at 6 decimals
   (JS toFixed rounds ties up, e.g. 0.0078125). */
export function fmt6(d) {
  if (!Number.isFinite(d) || d < 0) throw new AbiError('bad number');
  if (d === 0) return '0.000000';
  const dv = new DataView(new ArrayBuffer(8));
  dv.setFloat64(0, d);
  const bits = dv.getBigUint64(0);
  const expBits = Number((bits >> 52n) & 0x7ffn);
  let mant = bits & ((1n << 52n) - 1n);
  let e;
  if (expBits === 0) e = -1074; else { mant |= 1n << 52n; e = expBits - 1075; }
  let num = mant * 1000000n, den = 1n;
  if (e >= 0) num <<= BigInt(e); else den <<= BigInt(-e);
  let q = num / den;
  const r2 = 2n * (num % den);
  if (r2 > den || (r2 === den && (q & 1n))) q += 1n;
  const s = q.toString().padStart(7, '0');
  return s.slice(0, -6) + '.' + s.slice(-6);
}

/* Python's int / int true division: the double nearest to n/d (ties to even), for BigInts n >= 0, d > 0 */
export function ratioToDouble(n, d) {
  n = BigInt(n); d = BigInt(d);
  if (n < 0n || d <= 0n) throw new AbiError('bad ratio');
  if (n === 0n) return 0;
  const s = bitLen(d) - bitLen(n) + 55;
  const num = s >= 0 ? n << BigInt(s) : n;
  const den = s >= 0 ? d : d << BigInt(-s);
  let q = num / den;
  const sticky = num % den !== 0n;
  const extra = bitLen(q) - 53;
  const low = q & ((1n << BigInt(extra)) - 1n);
  const half = 1n << BigInt(extra - 1);
  q >>= BigInt(extra);
  if (low > half || (low === half && (sticky || (q & 1n)))) q += 1n;
  return Number(q) * 2 ** (extra - s);
}

/* f"{x / 1e18:.6f}" (int / float in Python: the int is converted to a double first) */
export function fmtFloatDiv(x, divisor) { return fmt6(Number(BigInt(x)) / divisor); }
/* f"{v / 10**dec:.6f}" (int / int in Python: correctly rounded) */
export function fmtIntDiv(v, dec) { return fmt6(ratioToDouble(BigInt(v), 10n ** BigInt(dec))); }
