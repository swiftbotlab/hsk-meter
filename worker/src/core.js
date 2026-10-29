/* HSK Meter keyless public endpoint (Swift Bot Labs): the request logic, free of Workers-specific APIs so it can be
   tested offline. The Worker holds NO private key: it cannot submit payments or refunds. It sells the same two
   HashKey Chain data APIs as server/seller.py, under the same wire protocol, for the buyer-pays-itself scheme
   ("hsk-paywall-tx"): the buyer calls pay(endpointId, requestId, amount) on HskPayPerRequest and retries with
   X-Payer + X-Paywall-Token; the Worker verifies the payment on-chain (getPayment + the Paid log found by request id)
   and serves each paid request id once (strongly consistent served-set in a Durable Object).
   Outbound network: ONLY the official HashKey Chain RPC (fixed URL, no redirects, timeouts, byte caps). */
import {
  ADDR_RE, B32_RE, ZERO, AbiError, bytesToHex, hexToBytes, concat, keccak, keccakText, keccakHex,
  wordUint, wordAddress, wordBytes32, calldata, decodeWords, hexToBigInt, fmtFloatDiv, fmtIntDiv
} from './abi.js';
import { LEASE_MS as SERVING_LEASE_MS } from './ledger_core.js';

export const VERSION = '1.0.0';
export const DOMAIN_TAG = 'hsk-paywall/v1';
export const X402_VERSION = 2;
export const CHAIN_ID = 177;
export const NETWORK = 'eip155:177';
export const RPC_URL = 'https://mainnet.hsk.xyz';
export const CONTRACT = '0x78c8476772bc209c3df09da5e129d0879cd57f8c';
export const DEPLOY_BLOCK = 29212085;
export const PROVIDER = '0x30ccc6d816854d1a66fbfece15b1515a3e941160';
export const PAYEE = '0xa8a49a6ca872d54a8fd5fb121139de9bff62a48d';
export const USDCE = '0x054ed45810DbBAb8B27668922D110669c9D88D0a';
export const USDT = '0xf1b50ed67a9e2cc94ad3c477779e2d4cbfff9029';
export const WHSK = '0xB210D2120d57b758EE163cFfb43e73728c471Cf1';
export const EXPLORER = 'https://hsk.blockscout.com';
export const REPO = 'https://github.com/swiftbotlab/hsk-meter';
export const BUIDL = 'https://dorahacks.io/buidl/50342';
export const DASHBOARD = 'https://swiftbotlab.github.io/hsk-meter/';
export const MAX_TIMEOUT = 300;
export const SERVE_WINDOW = 3600;          // a payment is served within 1 h of paidAt (as the Python seller)
export const MAX_URL = 400;
export const LOG_RANGE = 5000;             // eth_getLogs: mainnet.hsk.xyz refuses toBlock - fromBlock > 5000 (measured)


const DOMAIN = keccakText(DOMAIN_TAG);
export const PAID_TOPIC = keccakHex('Paid(uint256,bytes32,address,address,uint256)');
export const REFUNDED_TOPIC = keccakHex('Refunded(uint256,bytes32,address,uint256)');
export const TRANSFER_TOPIC = keccakHex('Transfer(address,address,uint256)');
const KNOWN_TOKENS = { [USDCE.toLowerCase()]: ['USDC.e', 6], [USDT.toLowerCase()]: ['USDT', 6], [WHSK.toLowerCase()]: ['WHSK', 18] };
const ENDPOINT_T = ['address', 'bool', 'uint64', 'address', 'uint64', 'uint128', 'uint128', 'uint128', 'uint64'];
const PAYMENT_T = ['address', 'uint64', 'bool', 'uint128', 'uint64', 'uint64'];

export const RESOURCES = {
  '/v1/hsk/address-profile': { endpointId: 1, param: 'address', re: ADDR_RE,
    desc: 'HashKey Chain address profile: HSK, USDC.e, USDT, WHSK balances, nonce, contract or EOA.' },
  '/v1/hsk/tx-receipt': { endpointId: 2, param: 'hash', re: B32_RE,
    desc: 'HashKey Chain transaction check: status, fee, ERC-20 transfers of a mined transaction.' }
};
export const SERVED_ENDPOINTS = new Set(Object.values(RESOURCES).map((r) => r.endpointId));

export class RpcError extends Error {
  constructor(msg, data) { super(msg); this.data = data; }
}
export class NotFound extends Error {}

/* ------------------------------------------------------------------ JSON-RPC client (pinned host) */
export function makeRpc({ fetchImpl, url = RPC_URL, timeoutMs = 8000, maxBytes = 1_000_000, budget = { n: 40 } }) {
  /* budget is per client; one request uses at most 2 clients of 40 calls (Workers free plan: 50 subrequests) */
  let id = 0;
  async function once(method, params) {
    if (budget.n-- <= 0) throw new RpcError('RPC unavailable: subrequest budget used up');
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const res = await fetchImpl(url, {
        method: 'POST', redirect: 'manual', signal: ctl.signal,
        headers: { 'content-type': 'application/json', 'user-agent': 'swiftbotlab-hsk-meter-worker/' + VERSION },
        body: JSON.stringify({ jsonrpc: '2.0', id: ++id, method, params })
      });
      if (res.status >= 300 && res.status < 400) throw new RpcError('RPC unavailable: redirect refused');
      const raw = await readCapped(res, maxBytes);
      let j;
      try { j = JSON.parse(raw); } catch (e) { throw new RpcError('RPC unavailable: ' + (res.status === 200 ? 'invalid JSON' : 'HTTP ' + res.status)); }
      if (res.status !== 200 && !(j && typeof j === 'object' && j.error !== undefined)) throw new RpcError('RPC unavailable: HTTP ' + res.status);
      if (!j || typeof j !== 'object' || Array.isArray(j)) throw new RpcError('RPC unavailable: invalid JSON');
      if (j.error !== undefined) {
        const err = j.error && typeof j.error === 'object' ? j.error : { message: String(j.error) };
        throw new RpcError('RPC error: ' + String(err.message).slice(0, 200), err.data);
      }
      return j.result;
    } catch (e) {
      if (e instanceof RpcError) throw e;
      throw new RpcError('RPC unavailable: ' + (e && e.name === 'AbortError' ? 'timeout' : String(e && e.message).slice(0, 80)));
    } finally {
      clearTimeout(timer);
    }
  }
  return async function rpc(method, params, tries = 1) {
    let last;
    for (let i = 0; i < tries; i++) {
      try { return await once(method, params); } catch (e) {
        if (!(e instanceof RpcError) || !e.message.startsWith('RPC unavailable') || e.message.includes('budget') || e.message.includes('too large')) throw e;
        last = e;
        if (i + 1 < tries) await new Promise((r) => setTimeout(r, 300 * (i + 1)));
      }
    }
    throw last;
  };
}

async function readCapped(res, maxBytes) {
  const len = Number(res.headers.get('content-length') || '0');
  if (len > maxBytes) throw new RpcError('RPC unavailable: response too large');
  if (!res.body || !res.body.getReader) {
    const t = await res.text();
    if (t.length > maxBytes) throw new RpcError('RPC unavailable: response too large');
    return t;
  }
  const reader = res.body.getReader();
  const chunks = [];
  let n = 0;
  for (;;) {
    const x = await reader.read();
    if (x.done) break;
    n += x.value.byteLength;
    if (n > maxBytes) {
      try { await reader.cancel(); } catch (e) { /* closed */ }
      throw new RpcError('RPC unavailable: response too large');
    }
    chunks.push(x.value);
  }
  return new TextDecoder().decode(concat(...chunks));
}

/* ------------------------------------------------------------------ chain reads */
async function ethCall(rpc, to, sig, args, outTypes, tries = 2) {
  const res = await rpc('eth_call', [{ to, data: calldata(sig, args) }, 'latest'], tries);
  try { return decodeWords(outTypes, res); } catch (e) { throw new RpcError('RPC unavailable: malformed eth_call result'); }
}

export async function readEndpoint(rpc, eid) {
  const e = await ethCall(rpc, CONTRACT, 'getEndpoint(uint256)', [['uint256', eid]], ENDPOINT_T);
  return { provider: e[0], active: e[1], createdAt: Number(e[2]), payee: e[3], paidCount: Number(e[4]),
    price: e[5], totalPaid: e[6], totalRefunded: e[7], refundCount: Number(e[8]) };
}

export async function readPayment(rpc, rid) {
  const p = await ethCall(rpc, CONTRACT, 'getPayment(bytes32)', [['bytes32', rid]], PAYMENT_T);
  return { payer: p[0], endpointId: Number(p[1]), refunded: p[2], amount: p[3], paidAt: Number(p[4]), paidBlock: Number(p[5]) };
}

function decodePaidLog(lg, rid) {
  const t = Array.isArray(lg && lg.topics) ? lg.topics : [];
  if (String(lg.address || '').toLowerCase() !== CONTRACT || t.length !== 4 || String(t[0]).toLowerCase() !== PAID_TOPIC) return null;
  if (rid && String(t[2]).toLowerCase() !== rid.toLowerCase()) return null;
  if (lg.removed) return null;
  try {
    const [payee, amount] = decodeWords(['address', 'uint256'], lg.data);
    const payerWord = decodeWords(['address'], t[3])[0];
    return { endpointId: Number(hexToBigInt(t[1])), requestId: String(t[2]).toLowerCase(), payer: payerWord, payee, amount,
      logIndex: Number(hexToBigInt(lg.logIndex || '0x0')), tx: String(lg.transactionHash || '').toLowerCase(),
      block: lg.blockNumber ? Number(hexToBigInt(lg.blockNumber)) : null };
  } catch (e) { return null; }
}

function decodeRefundedLog(lg, rid) {
  const t = Array.isArray(lg && lg.topics) ? lg.topics : [];
  if (String(lg.address || '').toLowerCase() !== CONTRACT || t.length !== 4 || String(t[0]).toLowerCase() !== REFUNDED_TOPIC) return null;
  if (rid && String(t[2]).toLowerCase() !== rid.toLowerCase()) return null;
  if (lg.removed) return null;
  try {
    const [amount] = decodeWords(['uint256'], lg.data);
    return { endpointId: Number(hexToBigInt(t[1])), requestId: String(t[2]).toLowerCase(), payer: decodeWords(['address'], t[3])[0],
      amount, tx: String(lg.transactionHash || '').toLowerCase(), block: lg.blockNumber ? Number(hexToBigInt(lg.blockNumber)) : null };
  } catch (e) { return null; }
}

/* The Paid log of `rid`, looked up by request id in the one block getPayment() names (never by a client tx hash). */
export async function findPaidLog(rpc, rid, block) {
  const logs = await rpc('eth_getLogs', [{ address: CONTRACT, fromBlock: '0x' + block.toString(16), toBlock: '0x' + block.toString(16),
    topics: [PAID_TOPIC, null, rid.toLowerCase()] }], 2);
  if (!Array.isArray(logs)) return null;
  for (const lg of logs) {
    const d = decodePaidLog(lg, rid);
    if (d) return d;
  }
  return null;
}

/* The Refunded log of `rid` at or after `fromBlock`, in RPC-sized windows (at most `windows` of 5,000 blocks, ~2.8 h
   each; the full seller refunds within a minute of the payment). getPayment().refunded stays the authority. */
export async function findRefundLog(rpc, rid, fromBlock, latest, windows = 1) {
  let lo = fromBlock;
  for (let i = 0; i < windows && lo <= latest; i++) {
    const hi = Math.min(latest, lo + LOG_RANGE);
    const logs = await rpc('eth_getLogs', [{ address: CONTRACT, fromBlock: '0x' + lo.toString(16), toBlock: '0x' + hi.toString(16),
      topics: [REFUNDED_TOPIC, null, rid.toLowerCase()] }], 2);
    for (const lg of Array.isArray(logs) ? logs : []) {
      const d = decodeRefundedLog(lg, rid);
      if (d) return d;
    }
    lo = hi + 1;
  }
  return null;
}

/* ------------------------------------------------------------------ protocol (identical to server/protocol.py) */
export function computeRequestId(eid, payer, resource, token, contract = CONTRACT, chainId = CHAIN_ID) {
  if (!ADDR_RE.test(contract) || !ADDR_RE.test(payer || ZERO) || !B32_RE.test(token)) throw new AbiError('bad requestId input');
  if (typeof resource !== 'string' || resource.length === 0 || resource.length > 2048) throw new AbiError('bad resource');
  const p = (payer || ZERO).toLowerCase();
  const digest = keccak(concat(DOMAIN, wordUint(chainId), wordAddress(contract), wordUint(eid), wordAddress(p),
    keccakText(resource), wordBytes32(token)));
  const rid = concat(hexToBytes(p), digest.subarray(20));
  return { digest: bytesToHex(digest), requestId: bytesToHex(rid) };
}

/* The HMAC context names this service ("worker"), so a token of the Python seller can never verify here even if a
   key were ever shared, and the quoted price is inside the MAC'd bytes. */
function tokenCtx(eid, payer, resource) {
  return new TextEncoder().encode(`${DOMAIN_TAG}|worker|${CHAIN_ID}|${CONTRACT}|${eid}|${payer}|${resource}`);
}

async function hmac16(keyBytes, msg) {
  const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await crypto.subtle.sign('HMAC', key, msg)).slice(0, 16);
}

/* quote token (bytes32) = quoted price (uint64, big-endian) || 8 random bytes || HMAC-SHA256(key, ctx || first 16)[:16].
   Clients cannot choose request ids; a token fits only its payer, endpoint and resource; and the price that was
   quoted travels with it, so a later price change never strands a payment made at the quoted price. */
export async function issueToken(keyBytes, eid, payer, resource, price, rand) {
  const p = BigInt(price);
  if (p <= 0n || p >= (1n << 64n)) throw new AbiError('bad price');
  const n = new Uint8Array(16);
  n.set(wordUint(p).subarray(24), 0);
  n.set(rand || crypto.getRandomValues(new Uint8Array(8)), 8);
  const mac = await hmac16(keyBytes, concat(tokenCtx(eid, payer, resource), n));
  return bytesToHex(concat(n, mac));
}

/* the quoted price (BigInt) when the token is ours for this payer, endpoint and resource; null otherwise */
export async function tokenPrice(keyBytes, eid, payer, resource, token) {
  const raw = hexToBytes(token);
  const want = await hmac16(keyBytes, concat(tokenCtx(eid, payer, resource), raw.subarray(0, 16)));
  let diff = 0;
  for (let i = 0; i < 16; i++) diff |= want[i] ^ raw[16 + i];
  if (diff !== 0) return null;
  const price = BigInt(bytesToHex(raw.subarray(0, 8)));
  return price > 0n ? price : null;
}

/* base64 of compact JSON with sorted keys and non-ASCII escaped: byte-identical to protocol.b64json */
export function b64json(obj) {
  const s = JSON.stringify(sortKeys(obj)).replace(/[\u0080-\uffff]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
  const bytes = new TextEncoder().encode(s);
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
function sortKeys(v) {
  if (Array.isArray(v)) return v.map(sortKeys);
  if (v && typeof v === 'object') {
    const o = {};
    for (const k of Object.keys(v).sort()) o[k] = sortKeys(v[k]);
    return o;
  }
  return v;
}

/* ------------------------------------------------------------------ the paid work (real chain data) */
export async function addressProfile(rpc, addr) {
  const a = addr.toLowerCase();
  const bal = (tok) => ethCall(rpc, tok, 'balanceOf(address)', [['address', a]], ['uint256'], 3).then((r) => r[0]);
  const [code, hsk, nonce, block, usdce, usdt, whsk] = await Promise.all([
    rpc('eth_getCode', [a, 'latest'], 3), rpc('eth_getBalance', [a, 'latest'], 3),
    rpc('eth_getTransactionCount', [a, 'latest'], 3), rpc('eth_blockNumber', [], 3), bal(USDCE), bal(USDT), bal(WHSK)]);
  if (typeof code !== 'string' || !/^0x([0-9a-fA-F]{2})*$/.test(code)) throw new RpcError('RPC unavailable: malformed code');
  let hskW, nonceN, blockN;
  try { hskW = hexToBigInt(hsk); nonceN = Number(hexToBigInt(nonce)); blockN = Number(hexToBigInt(block)); } catch (e) {
    throw new RpcError('RPC unavailable: malformed quantity');
  }
  return { address: a, chainId: CHAIN_ID, block: blockN, type: code.length > 2 ? 'contract' : 'eoa',
    codeSize: (code.length - 2) / 2, nonce: nonceN,
    balances: { HSK: fmtFloatDiv(hskW, 1e18), 'USDC.e': fmtFloatDiv(usdce, 1e6), USDT: fmtFloatDiv(usdt, 1e6), WHSK: fmtFloatDiv(whsk, 1e18) },
    explorer: EXPLORER + '/address/' + a };
}

export async function txReceipt(rpc, h) {
  const hash = h.toLowerCase();
  const rc = await rpc('eth_getTransactionReceipt', [hash], 3);
  if (!rc) throw new NotFound('no such transaction on HashKey Chain');
  if (typeof rc !== 'object') throw new RpcError('RPC unavailable: malformed receipt');
  const tx = (await rpc('eth_getTransactionByHash', [hash], 3)) || {};
  try {
    const gasUsed = hexToBigInt(rc.gasUsed);
    const fee = gasUsed * hexToBigInt(rc.effectiveGasPrice || '0x0');
    const logs = Array.isArray(rc.logs) ? rc.logs : [];
    const transfers = [];
    for (const lg of logs) {
      const t = Array.isArray(lg && lg.topics) ? lg.topics : [];
      const tok = String(lg.address || '').toLowerCase();
      if (KNOWN_TOKENS[tok] && t.length === 3 && String(t[0]).toLowerCase() === TRANSFER_TOPIC) {
        const [sym, dec] = KNOWN_TOKENS[tok];
        const v = hexToBigInt(lg.data === '0x' ? '0x0' : lg.data);
        transfers.push({ token: sym, from: '0x' + String(t[1]).slice(-40), to: '0x' + String(t[2]).slice(-40), amount: fmtIntDiv(v, dec) });
      }
    }
    return { hash, status: rc.status === '0x1' ? 'success' : 'reverted', block: Number(hexToBigInt(rc.blockNumber)),
      from: rc.from === undefined ? null : rc.from, to: rc.to === undefined ? null : rc.to,
      contractCreated: rc.contractAddress === undefined ? null : rc.contractAddress,
      valueHSK: fmtFloatDiv(hexToBigInt(tx.value || '0x0'), 1e18),
      gasUsed: Number(gasUsed), feeHSK: fmtFloatDiv(fee, 1e18), logs: logs.length,
      knownTokenTransfers: transfers.slice(0, 20), explorer: EXPLORER + '/tx/' + hash };
  } catch (e) {
    if (e instanceof RpcError) throw e;
    throw new RpcError('RPC unavailable: malformed receipt');
  }
}

export const WORK = { '/v1/hsk/address-profile': addressProfile, '/v1/hsk/tx-receipt': txReceipt };

/* ------------------------------------------------------------------ the 402 */
export function paymentRequired({ path, resource, price, payer, token, requestId }) {
  const common = { endpointId: String(RESOURCES[path].endpointId), requestId, token, payer, resource, protocol: DOMAIN_TAG };
  const body = {
    x402Version: X402_VERSION,
    error: 'payment required',
    resource: { url: path + '?' + resource.split('?', 2)[1], description: RESOURCES[path].desc, mimeType: 'application/json' },
    accepts: [{
      scheme: 'hsk-paywall-tx', network: NETWORK, amount: String(price), asset: USDCE, payTo: CONTRACT, maxTimeoutSeconds: MAX_TIMEOUT,
      extra: { call: 'approve(contract, amount) then pay(uint256 endpointId, bytes32 requestId, uint256 amount)',
        retryWith: { 'X-Payer': payer, 'X-Paywall-Token': token, 'X-Paywall-Tx': '<pay tx hash>' },
        serveWindowSeconds: SERVE_WINDOW, explorer: EXPLORER, ...common }
    }],
    extensions: { keyless: KEYLESS_NOTE }
  };
  return body;
}

export const KEYLESS_NOTE = {
  holdsKeys: false,
  schemes: ['hsk-paywall-tx'],
  why: 'this public endpoint holds no private key, so it cannot submit signed EIP-3009 authorizations (scheme exact) or send refunds; the buyer pays the contract itself and the endpoint verifies that payment on-chain',
  servedOnce: 'each paid requestId is served once, within 1 h of the payment',
  refunds: 'a paid request that cannot be served is listed at /v1/refunds and refunded on-chain by the provider (refund(requestId))',
  verify: '/v1/receipt/{requestId or tx hash}'
};

/* ------------------------------------------------------------------ the service */
const ERR_PAYER_REQUIRED = 'Send X-Payer: <the address that will pay>; the requestId is bound to it.';
export const MAX_RECEIPT_BYTES = 262144;      // a transaction whose receipt is larger is not sold here (CPU and cost bound)
const EXISTS_TTL = 600000;                    // a mined transaction stays mined: positive (and too-large) answers cached 10 min
const RECEIPT_TTL = 30000;                    // free receipt-verifier answers cached 30 s per isolate
const UNDELIVERABLE_AFTER = { attempts: 3, ms: 120000 };   // a paid tx check whose tx is missing: retried before giving up

function capPut(map, key, value, max = 2000) {
  map.delete(key);
  map.set(key, value);
  while (map.size > max) map.delete(map.keys().next().value);
}

export function createService({ rpcFactory, ledger, tokenKey, now = () => Date.now(), limiter, log = () => {},
  endpointCache = new Map(), existsCache = new Map(), receiptCache = new Map() }) {
  /* rpcFactory(): a fresh RPC client (own subrequest budget). ledger: the served-set (Durable Object or test double).
     tokenKey: Uint8Array >= 32 bytes or null (paid routes then answer 503). limiter(kind, ip) -> seconds to wait.
     The caches are per isolate (app.js keeps them at module level). */
  async function endpoint(rpc, eid, maxAgeMs) {
    const c = endpointCache.get(eid);
    if (c && now() - c.at < maxAgeMs) return c.e;
    const e = await readEndpoint(rpc, eid);
    if (e.provider === ZERO) throw new RpcError('endpoint not registered');
    capPut(endpointCache, eid, { e, at: now() }, 16);
    return e;
  }

  function oursOk(e) { return e.provider === PROVIDER && e.payee === PAYEE; }

  async function handlePaid(path, searchParams, headers, ip, lg) {
    const spec = RESOURCES[path];
    const keys = [...searchParams.keys()];
    const vals = searchParams.getAll(spec.param);
    if (keys.length !== 1 || keys[0] !== spec.param || vals.length !== 1 || !spec.re.test(vals[0])) {
      return [400, { ok: false, error: 'bad_request', message: `GET ${path}?${spec.param}=<0x...>` }, {}];
    }
    const arg = vals[0].toLowerCase();
    const resource = `GET ${path}?${spec.param}=${arg}`;
    const eid = spec.endpointId;
    const payerH = headers.get('X-Payer');
    if (payerH !== null && (!ADDR_RE.test(payerH) || payerH.toLowerCase() === ZERO)) {
      return [400, { ok: false, error: 'bad_payer', message: 'X-Payer must be a non-zero address (0x + 40 hex)' }, {}];
    }
    const payer = payerH ? payerH.toLowerCase() : null;
    if (headers.get('PAYMENT-SIGNATURE') !== null || headers.get('X-PAYMENT') !== null) {
      return [402, { ok: false, error: 'scheme_not_supported',
        message: 'this public endpoint holds no key and cannot settle EIP-3009 authorizations (scheme exact): pay with scheme hsk-paywall-tx (call pay() yourself), or run the full seller (server/seller.py) for exact', keyless: KEYLESS_NOTE }, {}];
    }
    if (!tokenKey) return [503, { ok: false, error: 'misconfigured', retryable: true }, { 'Retry-After': '60' }];
    if (headers.get('X-Paywall-Tx') !== null || headers.get('X-Paywall-Token') !== null) {
      return verifyTx(path, eid, resource, arg, payer, headers, ip, lg);
    }
    return quote(path, eid, resource, arg, payer, ip, lg);
  }

  /* tx-receipt quotes: is the transaction mined and small enough to sell? (cached per isolate) */
  async function txSellable(rpc, h) {
    const c = existsCache.get(h);
    if (c && now() - c.at < EXISTS_TTL) return c.v;
    let v;
    try {
      v = (await rpc('eth_getTransactionReceipt', [h], 2)) ? 'ok' : 'missing';
    } catch (err) {
      if (!String(err.message).includes('too large')) throw err;
      v = 'too_large';
    }
    if (v !== 'missing') capPut(existsCache, h, { v, at: now() });
    return v;
  }

  async function quote(path, eid, resource, arg, payer, ip, lg) {
    const wait = await limiter('quote', ip);
    if (wait) return [429, { ok: false, error: 'rate_limited' }, { 'Retry-After': String(wait) }];
    const rpc = rpcFactory({ maxBytes: MAX_RECEIPT_BYTES });
    let e;
    try {
      e = await endpoint(rpc, eid, 10000);
      if (path === '/v1/hsk/tx-receipt') {
        const v = await txSellable(rpc, arg);
        if (v === 'missing') return [404, { ok: false, error: 'not_found', message: 'no such transaction on HashKey Chain (nothing to pay for)' }, {}];
        if (v === 'too_large') return [422, { ok: false, error: 'receipt_too_large', message: `this transaction's receipt is larger than ${MAX_RECEIPT_BYTES} bytes and is not sold here (nothing to pay for)` }, {}];
      }
    } catch (err) {
      lg.err = String(err.message).slice(0, 120);
      return [502, { ok: false, error: 'chain_unavailable' }, { 'Retry-After': '2' }];
    }
    if (!oursOk(e)) return [503, { ok: false, error: 'endpoint_misconfigured' }, { 'Retry-After': '60' }];
    if (!e.active) return [503, { ok: false, error: 'endpoint_inactive' }, { 'Retry-After': '60' }];
    if (payer === null) {
      const body = paymentRequired({ path, resource, price: e.price, payer: null, token: null, requestId: null });
      body.error = 'payer_required';
      body.message = ERR_PAYER_REQUIRED;
      return [402, body, { 'PAYMENT-REQUIRED': b64json(body) }];
    }
    const token = await issueToken(tokenKey, eid, payer, resource, e.price);
    const { requestId } = computeRequestId(eid, payer, resource, token);
    lg.rid = requestId.slice(0, 18);
    const body = paymentRequired({ path, resource, price: e.price, payer, token, requestId });
    return [402, body, { 'PAYMENT-REQUIRED': b64json(body) }];
  }

  const ledgerDown = (lg, err) => {
    lg.err = 'ledger: ' + String(err && err.message).slice(0, 80);
    return [503, { ok: false, error: 'ledger_unavailable', retryable: true }, { 'Retry-After': '2' }];
  };

  async function verifyTx(path, eid, resource, arg, payer, headers, ip, lg) {
    const wait = await limiter('verify', ip);
    if (wait) return [429, { ok: false, error: 'rate_limited' }, { 'Retry-After': String(wait) }];
    const token = (headers.get('X-Paywall-Token') || '').toLowerCase();
    const txh = headers.get('X-Paywall-Tx');
    if (payer === null || !B32_RE.test(token) || (txh !== null && !B32_RE.test(txh))) {
      return [400, { ok: false, error: 'bad_payment_headers', message: 'need X-Payer and X-Paywall-Token (X-Paywall-Tx optional)' }, {}];
    }
    const quoted = await tokenPrice(tokenKey, eid, payer, resource, token);
    if (quoted === null) return [402, { ok: false, error: 'unknown_token' }, {}];
    const { requestId: rid } = computeRequestId(eid, payer, resource, token);
    lg.rid = rid.slice(0, 18);
    lg.payer = payer;
    const rpc = rpcFactory();
    let pay;
    try {
      pay = await readPayment(rpc, rid);                       // the contract first, never a client tx
    } catch (err) {
      lg.err = String(err.message).slice(0, 120);
      return [502, { ok: false, error: 'chain_unavailable' }, { 'Retry-After': '2' }];
    }
    if (pay.payer === ZERO) return [402, { ok: false, error: 'payment_not_found', retryable: true }, { 'Retry-After': '2' }];
    if (pay.payer !== payer || pay.endpointId !== eid) return [402, { ok: false, error: 'inconsistent_payment' }, {}];
    /* a real payment of this payer for this request: now the served-set (never touched for unpaid request ids) */
    let st;
    try { st = await ledger.get(rid); } catch (err) { return ledgerDown(lg, err); }
    if (st && st.state === 'served') return alreadyServed(st);
    if (st && st.state === 'undeliverable') {
      try { await ledger.ensureQueued(rid, st); } catch (err) { lg.err = 'ledger: queue failed'; }   // re-listed on every retry
      return undeliverable(rid, st.payment, st.reason, st.at);
    }
    if (st && st.state === 'serving' && now() - st.at < SERVING_LEASE_MS) return inProgress();
    if (pay.refunded) return [410, { ok: false, error: 'payment_refunded' }, {}];
    const basePayment = { requestId: rid, tx: null, block: pay.paidBlock, payer, amount: String(pay.amount), asset: USDCE,
      contract: CONTRACT, endpointId: eid, network: NETWORK, explorer: null };
    if (pay.paidAt + SERVE_WINDOW < now() / 1000) {
      /* not served within the hour (an outage, or the buyer came back late): list it for a refund */
      let lgp = null;
      try { lgp = await findPaidLog(rpc, rid, pay.paidBlock); } catch (err) { /* the listing does not need the tx */ }
      const payment = lgp ? { ...basePayment, tx: lgp.tx, explorer: EXPLORER + '/tx/' + lgp.tx } : basePayment;
      let listed = false;
      try { listed = await ledger.expire(rid, { reason: 'not served within 1 h of the payment', payment }, now()); } catch (err) { lg.err = 'ledger: expire failed'; }
      if (listed) log({ refund_needed: rid, why: 'expired unserved' });
      return [410, { ok: false, error: 'payment_expired', message: 'paid more than 1 h ago and not served: listed for a refund', payment,
        refund: refundNote(rid) }, {}];
    }
    let lgp, e;
    try {
      e = await endpoint(rpc, eid, 0);
      lgp = await findPaidLog(rpc, rid, pay.paidBlock);
    } catch (err) {
      lg.err = String(err.message).slice(0, 120);
      return [502, { ok: false, error: 'chain_unavailable' }, { 'Retry-After': '2' }];
    }
    if (!oursOk(e)) return [402, { ok: false, error: 'inconsistent_payment', message: 'endpoint is not run by this service' }, {}];
    if (pay.amount !== quoted) {
      return [402, { ok: false, error: 'wrong_amount', message: `paid ${pay.amount}, the quote was for ${quoted}` }, {}];
    }
    if (!lgp) return [402, { ok: false, error: 'payment_not_visible_yet', retryable: true }, { 'Retry-After': '2' }];
    if (lgp.payer !== payer || lgp.amount !== pay.amount || lgp.endpointId !== eid || lgp.payee !== PAYEE) {
      return [402, { ok: false, error: 'inconsistent_payment' }, {}];
    }
    lg.tx = lgp.tx;
    const payment = { ...basePayment, tx: lgp.tx, block: lgp.block === null ? pay.paidBlock : lgp.block, explorer: EXPLORER + '/tx/' + lgp.tx };
    let claim;
    try { claim = await ledger.claim(rid, { tx: lgp.tx, payer, endpointId: eid, amount: String(pay.amount) }, now()); } catch (err) {
      return ledgerDown(lg, err);
    }
    if (!claim.ok) {
      if (claim.state === 'served') return alreadyServed(claim);
      if (claim.state === 'undeliverable') return undeliverable(rid, claim.payment || payment, claim.reason, claim.at);
      return inProgress();
    }
    /* claimed ("serving", with a lease): no other request can get this payment's data while we work. The data is
       returned only if the claim is then committed as "served"; a replay after that gets 429 already_served. */
    let data;
    try {
      data = await WORK[path](rpcFactory({ maxBytes: MAX_RECEIPT_BYTES }), arg);
    } catch (err) {
      const giveUp = err instanceof NotFound && claim.attempts >= UNDELIVERABLE_AFTER.attempts && now() - claim.firstAt >= UNDELIVERABLE_AFTER.ms;
      if (giveUp) {
        let marked = false;
        try { marked = await ledger.markUndeliverable(rid, claim.claimId, { reason: err.message, payment }, now()); } catch (e2) { lg.err = 'ledger: mark failed'; }
        lg.undeliverable = marked;
        log({ refund_needed: rid, why: err.message, recorded: marked });
        return undeliverable(rid, payment, err.message, now());
      }
      lg.err = String(err.message).slice(0, 120);
      let released = false;
      try { released = await ledger.release(rid, claim.claimId); } catch (e2) { lg.err += ' | release failed'; }
      const ra = err instanceof NotFound ? 45 : (released ? 5 : 60);
      return [502, { ok: false, error: 'upstream_unavailable', retryable: true, payment,
        message: 'the chain data could not be read right now and nothing was served: retry the same request (same headers) within 1 h of the payment' }, { 'Retry-After': String(ra) }];
    }
    let committed = false;
    try { committed = await ledger.commit(rid, claim.claimId, now()); } catch (err) {
      lg.err = 'ledger: commit errored';
      try { committed = await ledger.committedBy(rid, claim.claimId); } catch (e2) { committed = false; }   // it may have landed
    }
    if (!committed) {
      return [503, { ok: false, error: 'ledger_unavailable', retryable: true, payment,
        message: 'the served-set could not be updated, so the data is not sent; retry the same request in about a minute' }, { 'Retry-After': '60' }];
    }
    lg.served = true;
    const settle = { success: true, transaction: lgp.tx, network: NETWORK, payer };
    return [200, { ok: true, data, payment }, { 'PAYMENT-RESPONSE': b64json(settle) }];
  }

  function inProgress() {
    return [409, { ok: false, error: 'in_progress', retryable: true, message: 'this payment is being processed; retry in a moment' }, { 'Retry-After': '2' }];
  }

  function alreadyServed(st) {
    return [429, { ok: false, error: 'already_served',
      message: 'this payment was already served once; each payment buys one response (buy again with a new quote, e.g. buyer.py --new-purchase)',
      servedAt: st.at ? new Date(st.at).toISOString() : undefined, tx: st.tx }, {}];
  }

  function refundNote(rid) {
    return { refunded: false, tx: null,
      note: 'refund queued for the operator: this public endpoint holds no key, so the provider sends refund(requestId) on-chain itself; the request is listed at /v1/refunds until then',
      how: `cast send ${CONTRACT} "refund(bytes32)" ${rid} --rpc-url ${RPC_URL} --account <provider>`,
      check: '/v1/receipt/' + rid };
  }

  function undeliverable(rid, payment, reason, at) {
    return [502, { ok: false, error: 'not_delivered', message: String(reason || 'the paid data could not be produced').slice(0, 160), payment,
      refund: refundNote(rid), queuedAt: at ? new Date(at).toISOString() : undefined }, {}];
  }

  /* ---------------------------------------------------------------- free: the receipt verifier */
  async function receipt(id, searchParams, ip) {
    const wait = await limiter('receipt', ip);
    if (wait) return [429, { ok: false, error: 'rate_limited' }, { 'Retry-After': String(wait) }];
    if (!B32_RE.test(id)) return [400, { ok: false, error: 'bad_request', message: 'GET /v1/receipt/<requestId or tx hash: 0x + 64 hex>' }, {}];
    const expect = {};
    for (const [k, v] of searchParams) {
      if (k === 'payer' && ADDR_RE.test(v) && expect.payer === undefined) expect.payer = v.toLowerCase();
      else if (k === 'endpoint' && /^[1-9][0-9]{0,5}$/.test(v) && expect.endpoint === undefined) expect.endpoint = Number(v);
      else if (k === 'amount' && /^[1-9][0-9]{0,20}$/.test(v) && expect.amount === undefined) expect.amount = BigInt(v);
      else return [400, { ok: false, error: 'bad_request', message: 'optional query: payer=0x..., endpoint=<id>, amount=<base units>, each once' }, {}];
    }
    const h = id.toLowerCase();
    const ckey = h + '|' + (expect.payer || '') + '|' + (expect.endpoint || '') + '|' + (expect.amount === undefined ? '' : String(expect.amount));
    const c = receiptCache.get(ckey);
    if (c && now() - c.at < RECEIPT_TTL) return [200, c.body, {}];
    const rpc = rpcFactory({ maxBytes: MAX_RECEIPT_BYTES });
    let body;
    try {
      const pay = await readPayment(rpc, h);
      if (pay.payer !== ZERO) body = await verifyRequest(rpc, h, pay, expect, null);
      else body = await verifyTxHash(rpc, h, expect);
    } catch (err) {
      if (err instanceof RpcError || err instanceof AbiError) {
        if (String(err.message).includes('too large')) {
          body = { ok: true, kind: 'transaction', tx: h, verdict: 'REJECT', reasons: [`the receipt is larger than ${MAX_RECEIPT_BYTES} bytes: check its payments one by one by requestId`] };
        } else return [502, { ok: false, error: 'chain_unavailable', retryable: true }, { 'Retry-After': '2' }];
      } else throw err;
    }
    capPut(receiptCache, ckey, { body, at: now() }, 500);
    return [200, body, {}];
  }

  async function verifyTxHash(rpc, h, expect) {
    const rc = await rpc('eth_getTransactionReceipt', [h], 2);
    const paid = [], refunded = [];
    for (const lg of rc && Array.isArray(rc.logs) ? rc.logs : []) {
      const p = decodePaidLog(lg, null);
      if (p) paid.push(p);
      const r = decodeRefundedLog(lg, null);
      if (r) refunded.push(r);
    }
    if (!rc || (!paid.length && !refunded.length)) {
      /* say nothing more about other transactions: checking those is what endpoint 2 sells */
      return { ok: true, kind: 'unknown', id: h, verdict: 'REJECT', contract: CONTRACT,
        reasons: ['not a paid requestId of the settlement contract, and not a transaction with its Paid or Refunded events'] };
    }
    const out = { ok: true, kind: 'transaction', tx: h, status: rc.status === '0x1' ? 'success' : 'reverted',
      block: Number(hexToBigInt(rc.blockNumber)), contract: CONTRACT, explorer: EXPLORER + '/tx/' + h,
      events: { paid: paid.slice(0, 10).map(evPaid), refunded: refunded.slice(0, 10).map(evRefunded) }, payments: [] };
    const reasons = [];
    if (rc.status !== '0x1') reasons.push('the transaction reverted');
    const all = [...new Set([...paid, ...refunded].map((x) => x.requestId))];
    if (all.length > 2) reasons.push('more than 2 payments in one transaction: check them one by one by requestId');
    for (const r of all.slice(0, 2)) {                                       // bounded RPC cost per call
      const v = await verifyRequest(rpc, r, await readPayment(rpc, r), expect, h, refunded.find((x) => x.requestId === r));
      out.payments.push(v);
      if (v.verdict !== 'ACCEPT') reasons.push(`requestId ${r}: ` + v.reasons.join('; '));
    }
    out.verdict = reasons.length ? 'REJECT' : 'ACCEPT';
    out.reasons = reasons.length ? reasons : ['every payment in this transaction is paid, matches its Paid event and is not refunded'];
    return out;
  }

  function evPaid(p) {
    return { endpointId: p.endpointId, requestId: p.requestId, payer: p.payer, payee: p.payee, amount: String(p.amount), logIndex: p.logIndex };
  }
  function evRefunded(r) {
    return { endpointId: r.endpointId, requestId: r.requestId, payer: r.payer, amount: String(r.amount) };
  }

  async function verifyRequest(rpc, rid, pay, expect, viaTx, refundInTx) {
    const checks = [];
    const add = (check, ok, detail) => checks.push({ check, ok, detail });
    if (pay.payer === ZERO) {
      add('paid on-chain', false, 'getPayment(requestId).payer is zero');
      return { kind: 'requestId', requestId: rid, verdict: 'REJECT', reasons: ['not paid on the settlement contract'], checks };
    }
    const [latestHex, e, lgp] = await Promise.all([rpc('eth_blockNumber', [], 2), readEndpoint(rpc, pay.endpointId), findPaidLog(rpc, rid, pay.paidBlock)]);
    const latest = Number(hexToBigInt(latestHex));
    add('paid on-chain', true, `getPayment: payer ${pay.payer}, endpoint ${pay.endpointId}, amount ${pay.amount}, block ${pay.paidBlock}`);
    add('payer bound to the requestId', rid.slice(0, 42) === pay.payer, 'the top 20 bytes of the requestId name the payer');
    add('Paid event found by requestId', !!lgp, lgp ? `tx ${lgp.tx}, log ${lgp.logIndex}` : `no Paid log for this requestId in block ${pay.paidBlock}`);
    if (lgp) {
      add('Paid event matches getPayment', lgp.payer === pay.payer && lgp.amount === pay.amount && lgp.endpointId === pay.endpointId,
        `event: payer ${lgp.payer}, endpoint ${lgp.endpointId}, amount ${lgp.amount}`);
      if (viaTx && !refundInTx) add('Paid event is in the given transaction', lgp.tx === viaTx, lgp.tx === viaTx ? 'yes' : `it is in ${lgp.tx}`);
    }
    const ours = SERVED_ENDPOINTS.has(pay.endpointId) && e.provider === PROVIDER && e.payee === PAYEE;
    add('endpoint sold by this service', ours, ours ? `endpoint ${pay.endpointId} of provider ${PROVIDER}, payee ${PAYEE}`
      : `endpoint ${pay.endpointId} belongs to provider ${e.provider} (the contract is shared: anyone can register endpoints)`);
    if (lgp && ours) add('paid to the service payee', lgp.payee === PAYEE, `payee ${lgp.payee}`);
    let refund = null;
    if (pay.refunded) {
      /* getPayment().refunded is the authority. The refund tx is shown when it is the given transaction, or within the
         first RPC window after the payment (the full seller refunds within a minute): one bounded lookup. */
      refund = refundInTx ? { ...refundInTx, tx: viaTx } : await findRefundLog(rpc, rid, pay.paidBlock, latest, 1);
      add('refund status', false, refund ? `refunded in tx ${refund.tx}` + (refund.block ? ` (block ${refund.block})` : '')
        : 'refunded: getPayment().refunded is true (see the explorer for the refund transaction)');
    } else add('refund status', true, 'not refunded: getPayment().refunded is false');
    if (expect.payer !== undefined) add('expected payer', pay.payer === expect.payer, `expected ${expect.payer}, paid by ${pay.payer}`);
    if (expect.endpoint !== undefined) add('expected endpoint', pay.endpointId === expect.endpoint, `expected ${expect.endpoint}, paid for ${pay.endpointId}`);
    if (expect.amount !== undefined) add('expected amount', pay.amount === expect.amount, `expected ${expect.amount}, paid ${pay.amount}`);
    let served;
    try {
      const st = await ledger.get(rid);
      if (st) served = { state: st.state, at: st.at ? new Date(st.at).toISOString() : null };
      else if (ours && !pay.refunded && pay.paidAt + SERVE_WINDOW < now() / 1000) served = { state: 'not served by this endpoint; the 1 h window is over (the provider can refund it)' };
      else served = { state: 'not served by this endpoint' };
    } catch (err) { served = { state: 'unknown (ledger unavailable)' }; }
    const reasons = checks.filter((c) => !c.ok).map((c) => c.check + ': ' + c.detail);
    return { kind: 'requestId', requestId: rid, verdict: reasons.length ? 'REJECT' : 'ACCEPT',
      reasons: reasons.length ? reasons : ['paid on-chain to this service, the Paid event matches getPayment, not refunded'],
      checks, payment: { payer: pay.payer, endpointId: pay.endpointId, amount: String(pay.amount), paidAt: pay.paidAt,
        paidAtIso: new Date(pay.paidAt * 1000).toISOString(), paidBlock: pay.paidBlock, refunded: pay.refunded,
        tx: lgp ? lgp.tx : null, explorer: lgp ? EXPLORER + '/tx/' + lgp.tx : null },
      refund: refund ? { tx: refund.tx, block: refund.block || null, amount: String(refund.amount), explorer: EXPLORER + '/tx/' + refund.tx } : null,
      confirmations: Math.max(0, latest - pay.paidBlock), servedByThisEndpoint: served };
  }

  /* ---------------------------------------------------------------- free: info */
  async function info() {
    const rpc = rpcFactory();
    const eps = {};
    for (const [p, s] of Object.entries(RESOURCES)) {
      try {
        const e = await endpoint(rpc, s.endpointId, 10000);
        eps[p] = { endpointId: s.endpointId, price: String(e.price), active: e.active, provider: e.provider, payee: e.payee };
      } catch (err) { eps[p] = { endpointId: s.endpointId, price: null, active: null }; }
    }
    return eps;
  }

  async function refunds(ip) {
    const wait = await limiter('receipt', ip);
    if (wait) return [429, { ok: false, error: 'rate_limited' }, { 'Retry-After': String(wait) }];
    try {
      const list = await ledger.listRefunds();
      return [200, { ok: true, note: KEYLESS_NOTE.refunds, refunds: list }, {}];
    } catch (err) {
      return [503, { ok: false, error: 'ledger_unavailable', retryable: true }, { 'Retry-After': '2' }];
    }
  }

  return { handlePaid, receipt, info, refunds };
}

export function wellKnown() {
  return { x402Version: X402_VERSION, network: NETWORK, asset: USDCE, payTo: CONTRACT, schemes: ['hsk-paywall-tx'],
    resources: Object.entries(RESOURCES).map(([p, s]) => ({ path: p + '?' + s.param + '=0x...', endpointId: s.endpointId, description: s.desc })),
    extensions: ['keyless'], keyless: KEYLESS_NOTE, provider: PROVIDER, payee: PAYEE, contract: CONTRACT,
    free: ['/', '/health', '/.well-known/x402', '/v1/receipt/{requestId|txHash}', '/v1/refunds'] };
}
