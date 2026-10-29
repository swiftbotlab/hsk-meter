/* HSK Meter public endpoint on Cloudflare Workers (Swift Bot Labs). Keyless: no private key of any kind that can move
   funds. Routes:
     paid  GET /v1/hsk/address-profile?address=0x...   endpoint 1      GET /v1/hsk/tx-receipt?hash=0x...   endpoint 2
     free  GET /  /health  /.well-known/x402  /v1/receipt/<requestId|txHash>  /v1/refunds
   Request logic: core.js; served-set: ledger_core.js + ledger.js (Durable Object, one instance per requestId).
   This file has no Workers-only imports, so the offline tests run it under Node. */
import { createService, makeRpc, wellKnown, RESOURCES, VERSION, NETWORK, CONTRACT, MAX_URL, PROVIDER, PAYEE, USDCE, KEYLESS_NOTE } from './core.js';
import { doLedger } from './ledger_core.js';
import { homePage } from './page.js';

const CORS = {
  'Access-Control-Allow-Origin': '*',          // a public API without cookies or credentials: any origin may read it
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'X-Payer, X-Paywall-Token, X-Paywall-Tx, PAYMENT-SIGNATURE, X-PAYMENT',
  'Access-Control-Expose-Headers': 'PAYMENT-REQUIRED, PAYMENT-RESPONSE, Retry-After',
  'Access-Control-Max-Age': '600'
};
const BASE = { 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store' };
const PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
const LIMITS = { quote: [60, 60000], verify: [30, 60000], receipt: [30, 60000], free: [120, 60000] };

/* per-isolate fixed-window limiter (least recently used key evicted first): per client, per IPv6 /48 (4x the client
   limit, against address rotation) and per isolate for everything that costs RPC calls (20x). The Workers rate-limit
   bindings add a per-location limit on top. Nothing inside a Worker can stop requests from counting against the
   account's daily request quota: that needs a WAF rule in front (see SECURITY.md). */
const buckets = new Map();
const ENDPOINT_CACHE = new Map();   // endpoint prices, kept 10 s per isolate for quotes and pages (paid retries read fresh)
const EXISTS_CACHE = new Map();     // tx-receipt quotes: mined (or too large) answers, 10 min
const RECEIPT_CACHE = new Map();    // free receipt-verifier answers, 30 s
function hit(key, limit, win, nowMs) {
  let b = buckets.get(key);
  buckets.delete(key);
  if (!b || nowMs - b.start > win) b = { start: nowMs, n: 0 };
  b.n += 1;
  buckets.set(key, b);
  while (buckets.size > 5000) buckets.delete(buckets.keys().next().value);
  return b.n > limit ? Math.max(1, Math.ceil((b.start + win - nowMs) / 1000)) : 0;
}
function localLimit(kind, ip, nowMs) {
  const [limit, win] = LIMITS[kind];
  let w = hit(kind + '|' + ip, limit, win, nowMs);
  const m = /^(.*):[0-9a-f]{1,4}::\/64$/.exec(ip);
  if (!w && m) w = hit(kind + '|' + m[1] + '::/48', 4 * limit, win, nowMs);
  if (!w && kind !== 'free') w = hit(kind + '|*', 20 * limit, win, nowMs);
  return w;
}

/* IPv4 as is; IPv6 keyed by its /64 */
export function clientKey(ip) {
  const s = String(ip || '').trim();
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(s)) return s;
  if (!/^[0-9a-fA-F:.]{2,45}$/.test(s) || !s.includes(':')) return 'invalid';
  let [head, tail] = s.split('::');
  if (s.split('::').length > 2) return 'invalid';
  const h = head ? head.split(':') : [];
  const t = tail !== undefined ? (tail ? tail.split(':') : []) : null;
  let groups;
  if (t === null) groups = h;
  else groups = [...h, ...Array(Math.max(0, 8 - h.length - t.length)).fill('0'), ...t];
  if (groups.length < 4) return 'invalid';
  return groups.slice(0, 4).map((g) => (g.includes('.') ? '0' : g).toLowerCase().replace(/^0+(?=.)/, '')).join(':') + '::/64';
}

function respond(status, body, headers, extra) {
  const h = new Headers({ ...BASE, ...CORS, 'Content-Type': 'application/json; charset=utf-8', ...(headers || {}), ...(extra || {}) });
  return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: h });
}

function tokenKeyFrom(env) {
  const v = env && typeof env.PAYWALL_TOKEN_KEY === 'string' ? env.PAYWALL_TOKEN_KEY.trim() : '';
  if (!/^[0-9a-fA-F]{64,128}$/.test(v) || v.length % 2) return null;
  const out = new Uint8Array(v.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(v.substr(2 * i, 2), 16);
  return out;
}

/* deps (tests and the local compatibility server only): fetchImpl, ledger, now, rpcUrl, log */
export async function handle(req, env, deps = {}) {
  const now = deps.now || (() => Date.now());
  const ip = clientKey(req.headers.get('CF-Connecting-IP') || deps.ip || '');
  const limiter = async (kind, key) => {
    const w = localLimit(kind, key, now());
    if (w) return w;
    const b = env && env['RL_' + kind.toUpperCase()];
    if (b && typeof b.limit === 'function') {
      try {
        const r = await b.limit({ key });
        if (r && r.success === false) return 60;
      } catch (e) { /* the binding failing open is fine: the local limiter already ran */ }
    }
    return 0;
  };
  const fetchImpl = deps.fetchImpl || ((u, o) => fetch(u, o));
  const service = createService({
    rpcFactory: (o = {}) => makeRpc({ fetchImpl, url: deps.rpcUrl, budget: { n: 40 }, maxBytes: o.maxBytes }),
    ledger: deps.ledger || doLedger(env.LEDGER),
    tokenKey: tokenKeyFrom(env),
    now, limiter,
    log: deps.log || ((o) => console.log(JSON.stringify(o))),
    endpointCache: deps.endpointCache || ENDPOINT_CACHE,
    existsCache: deps.existsCache || EXISTS_CACHE,
    receiptCache: deps.receiptCache || RECEIPT_CACHE
  });
  const t0 = now();
  if (req.url.length > MAX_URL + 64) return respond(414, { ok: false, error: 'uri_too_long' });
  const url = new URL(req.url);
  const path = url.pathname;
  const lg = { t: new Date(t0).toISOString(), p: path };
  if (req.method === 'OPTIONS') {
    if (await limiter('free', ip)) return respond(429, { ok: false, error: 'rate_limited' }, { 'Retry-After': '30' });
    return respond(204, null, {});
  }
  if (req.method !== 'GET') return respond(405, { ok: false, error: 'method_not_allowed' }, { Allow: 'GET, OPTIONS' });
  if ((url.pathname + url.search).length > MAX_URL) return respond(414, { ok: false, error: 'uri_too_long' });
  let status, body, headers = {};
  try {
    if (RESOURCES[path]) {
      [status, body, headers] = await service.handlePaid(path, url.searchParams, req.headers, ip, lg);
    } else if (path.startsWith('/v1/receipt/')) {
      [status, body, headers] = await service.receipt(path.slice('/v1/receipt/'.length), url.searchParams, ip);
    } else if (path === '/v1/refunds') {
      [status, body, headers] = await service.refunds(ip);
    } else if (path === '/.well-known/x402') {
      if (await limiter('free', ip)) return respond(429, { ok: false, error: 'rate_limited' }, { 'Retry-After': '30' });
      [status, body] = [200, wellKnown()];
    } else if (path === '/health') {
      if (await limiter('free', ip)) return respond(429, { ok: false, error: 'rate_limited' }, { 'Retry-After': '30' });
      [status, body] = [200, { ok: true, service: 'hsk-meter', version: VERSION, network: NETWORK, contract: CONTRACT,
        endpoints: await service.info(), settler: null, hspAdapter: null, keyless: true, provider: PROVIDER, payee: PAYEE,
        servedSet: env && env.LEDGER ? 'durable-object' : 'unavailable', paidRoutes: env && tokenKeyFrom(env) ? 'on' : 'off (no quote-token key)' }];
    } else if (path === '/') {
      if (await limiter('free', ip)) return respond(429, { ok: false, error: 'rate_limited' }, { 'Retry-After': '30' });
      const eps = await service.info();
      if ((req.headers.get('Accept') || '').includes('text/html')) {
        return new Response(homePage(eps, url.origin), { status: 200, headers: { ...BASE, 'Content-Type': 'text/html; charset=utf-8',
          'Content-Security-Policy': PAGE_CSP, 'X-Frame-Options': 'DENY' } });
      }
      [status, body] = [200, { service: 'HSK Meter by Swift Bot Labs: HashKey Chain data, paid per request in USDC.e',
        paid: Object.keys(RESOURCES), prices: eps, asset: USDCE, network: NETWORK, contract: CONTRACT,
        free: ['/health', '/.well-known/x402', '/v1/receipt/{requestId|txHash}', '/v1/refunds'],
        how: 'GET a paid path with X-Payer -> 402 (x402 v2, scheme hsk-paywall-tx) -> approve + pay(endpointId, requestId, amount) on the contract -> retry with X-Payer, X-Paywall-Token, X-Paywall-Tx',
        keyless: KEYLESS_NOTE, repo: 'https://github.com/swiftbotlab/hsk-meter' }];
    } else {
      [status, body] = [404, { ok: false, error: 'not_found' }];
    }
  } catch (err) {               // never leak internals
    lg.err = (err && err.name ? err.name + ': ' : '') + String(err && err.message).slice(0, 120);
    [status, body, headers] = [500, { ok: false, error: 'internal' }, {}];
  }
  lg.s = status;
  lg.ms = now() - t0;
  if (RESOURCES[path] || path.startsWith('/v1/receipt/') || lg.err) (deps.log || ((o) => console.log(JSON.stringify(o))))(lg);
  return respond(status, body, headers);
}
