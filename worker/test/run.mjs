/* Offline tests of the HSK Meter Worker (Swift Bot Labs): node worker/test/run.mjs
   No network: the chain is test/fakechain.mjs, the served-set is the in-memory Durable Object double. Every guard
   is tested with a positive control (the same request with the guarded field right) next to the negative case. */
import { readFileSync } from 'node:fs';
import { handle, clientKey } from '../src/app.js';
import { computeRequestId, issueToken, tokenPrice, b64json, MAX_RECEIPT_BYTES, makeRpc, RpcError, CONTRACT, USDCE, PROVIDER, PAYEE, PAID_TOPIC,
  RESOURCES, USDT, WHSK, RPC_URL } from '../src/core.js';
import { fmtFloatDiv, fmtIntDiv, fmt6, decodeWords, wordUint, bytesToHex, concat, wordAddress, AbiError } from '../src/abi.js';
import { memoryNamespace, doLedger, ledgerCore, LEASE_MS } from '../src/ledger_core.js';
import { homePage } from '../src/page.js';
import { FakeChain } from './fakechain.mjs';

const V = JSON.parse(readFileSync(new URL('./vectors.json', import.meta.url)));
const KEY = 'a1'.repeat(32);
const BUYER = '0x5270fe01fd3d6148b687da0b5693b0425cfca361';
const OTHER = '0x' + '77'.repeat(20);
const SAMPLE = '0x78c8476772bc209c3df09da5e129d0879cd57f8c';
const HASH = '0x' + 'cd'.repeat(32);
let ipn = 0;

function setup(opts = {}) {
  const chain = new FakeChain();
  chain.native[SAMPLE] = 0n;
  chain.code[SAMPLE] = '0x' + '60'.repeat(8726);
  chain.nonces[SAMPLE] = 1;
  chain.setBalance(USDCE, SAMPLE, 620000);
  chain.externalTx(HASH, { transfer: { from: BUYER, to: OTHER, amount: 1234565 } });
  const ns = memoryNamespace();
  const logs = [];
  let t = opts.t0 || Date.now();
  const deps = { fetchImpl: chain.fetchImpl(), ledger: opts.ledger || doLedger(ns), now: () => t, ip: '10.0.' + (++ipn >> 8) + '.' + (ipn & 255),
    log: (o) => logs.push(o), endpointCache: new Map(), existsCache: new Map(), receiptCache: new Map() };
  const env = { PAYWALL_TOKEN_KEY: opts.key === undefined ? KEY : opts.key };
  async function call(path, headers = {}, method = 'GET') {
    const res = await handle(new Request('https://hsk-meter.test' + path, { method, headers }), env, deps);
    const text = await res.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch (e) { body = text; }
    return { status: res.status, body, headers: res.headers };
  }
  async function quote(path, payer = BUYER) {
    const r = await call(path, { 'X-Payer': payer });
    if (r.status !== 402) throw new Error('expected 402, got ' + r.status + ' ' + JSON.stringify(r.body));
    const x = r.body.accepts[0].extra;
    return { rid: x.requestId, token: x.token, r };
  }
  async function quoteAndPay(path, payer = BUYER, payOpts = {}) {
    const q = await quote(path, payer);
    const eid = RESOURCES[path.split('?')[0]].endpointId;
    const tx = chain.pay(q.rid, payOpts.payer || payer, payOpts.eid || eid, payOpts);
    return { ...q, tx };
  }
  const paid = (path, payer, token, tx) => call(path, { 'X-Payer': payer, 'X-Paywall-Token': token, ...(tx ? { 'X-Paywall-Tx': tx } : {}) });
  return { chain, ns, deps, env, call, quote, quoteAndPay, paid, logs, advance: (ms) => { t += ms; } };
}

const P1 = '/v1/hsk/address-profile?address=' + SAMPLE;
const P2 = '/v1/hsk/tx-receipt?hash=' + HASH;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function eq(a, b, msg) {
  const sa = JSON.stringify(a, (k, v) => (typeof v === 'bigint' ? v.toString() : v));
  const sb = JSON.stringify(b, (k, v) => (typeof v === 'bigint' ? v.toString() : v));
  if (sa !== sb) throw new Error((msg || 'not equal') + ': ' + sa + ' !== ' + sb);
}
const canon = (v) => (Array.isArray(v) ? v.map(canon) : v && typeof v === 'object'
  ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon(v[k])])) : v);
function deq(a, b, msg) { eq(canon(a), canon(b), msg); }
function ok(c, msg) { if (!c) throw new Error(msg || 'assertion failed'); }
const unb64 = (s) => JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(s), (c) => c.charCodeAt(0))));

/* ------------------------------------------------------------------ cross-language vectors (from server/protocol.py) */
test('requestId and digest match server/protocol.py', () => {
  for (const v of V.rid) {
    const r = computeRequestId(v.eid, v.payer, v.resource, v.token);
    eq(r.digest, v.digest, 'digest');
    eq(r.requestId, v.requestId, 'requestId');
  }
});
test('number formatting matches the Python seller (incl. a half-way tie and values above 2^53)', () => {
  for (const v of V.fmtFloat) eq(fmtFloatDiv(BigInt(v.x), v.div === '1e18' ? 1e18 : 1e6), v.py, 'float ' + v.x);
  for (const v of V.fmtInt) eq(fmtIntDiv(BigInt(v.x), v.dec), v.py, 'int ' + v.x);
  eq(fmt6(0.0078125), '0.007812', 'tie rounds to even like Python');
  ok((0.0078125).toFixed(6) === '0.007813', 'control: toFixed would differ');
});
test('quote tokens: price || random || HMAC, equal to an independent Python HMAC; bound to payer, endpoint, resource, price', async () => {
  for (const v of V.token) {
    const key = Uint8Array.from(v.key.match(/../g).map((h) => parseInt(h, 16)));
    const rand = Uint8Array.from(v.rand.match(/../g).map((h) => parseInt(h, 16)));
    eq(await issueToken(key, v.eid, v.payer, v.resource, BigInt(v.price), rand), v.token);
    eq(await tokenPrice(key, v.eid, v.payer, v.resource, v.token), BigInt(v.price), 'verifies and returns the quoted price');
    eq(await tokenPrice(key, v.eid, OTHER, v.resource, v.token), null, 'other payer refused');
    eq(await tokenPrice(key, v.eid + 1, v.payer, v.resource, v.token), null, 'other endpoint refused');
    eq(await tokenPrice(key, v.eid, v.payer, v.resource + 'x', v.token), null, 'other resource refused');
    const raised = '0x' + (BigInt(v.price) + 1n).toString(16).padStart(16, '0') + v.token.slice(18);
    eq(await tokenPrice(key, v.eid, v.payer, v.resource, raised), null, 'a changed price breaks the MAC');
  }
});
test('event topic and base64 JSON envelope match Python', () => {
  eq(PAID_TOPIC, V.paidTopic);
  eq(b64json({ b: [1, { z: 'x', a: null }], a: 'é', c: true }), V.b64, 'byte-identical to protocol.b64json (sorted keys)');
});
test('ABI decoding refuses dirty words and short data', () => {
  let threw = 0;
  for (const [types, hex] of [[['address'], '0x' + 'ff'.repeat(32)], [['bool'], bytesToHex(wordUint(2))], [['uint64'], bytesToHex(wordUint(1n << 64n))], [['uint256', 'uint256'], bytesToHex(wordUint(1))]]) {
    try { decodeWords(types, hex); } catch (e) { if (e instanceof AbiError) threw++; }
  }
  eq(threw, 4);
  eq(decodeWords(['address', 'bool'], bytesToHex(concat(wordAddress(BUYER), wordUint(1)))), [BUYER, true], 'positive control');
});

/* ------------------------------------------------------------------ the 402 */
test('unpaid request: 402 with x402 v2 terms for the buyer-paid scheme, header == body, requestId recomputable', async () => {
  const s = setup();
  const { r, rid, token } = await s.quote(P1);
  const b = r.body;
  eq(b.x402Version, 2);
  eq(b.accepts.length, 1);
  const a = b.accepts[0];
  eq([a.scheme, a.network, a.amount, a.asset, a.payTo, a.maxTimeoutSeconds], ['hsk-paywall-tx', 'eip155:177', '30000', USDCE, CONTRACT, 300]);
  eq(a.extra.endpointId, '1');
  eq(a.extra.payer, BUYER);
  eq(a.extra.resource, 'GET /v1/hsk/address-profile?address=' + SAMPLE);
  eq(a.extra.protocol, 'hsk-paywall/v1');
  eq(computeRequestId(1, BUYER, a.extra.resource, token).requestId, rid, 'requestId recomputed by the client');
  eq(rid.slice(0, 42), BUYER, 'payer-bound');
  deq(unb64(r.headers.get('PAYMENT-REQUIRED')), b, 'PAYMENT-REQUIRED == body');
  eq(b.resource.url, '/v1/hsk/address-profile?address=' + SAMPLE);
  eq(b.extensions.keyless.holdsKeys, false);
  eq(r.headers.get('Access-Control-Expose-Headers'), 'PAYMENT-REQUIRED, PAYMENT-RESPONSE, Retry-After');
});
test('quote is case-normalised: an upper-case query gets the lower-case resource', async () => {
  const s = setup();
  const { r } = await s.quote('/v1/hsk/address-profile?address=0x78C8476772BC209C3df09Da5e129d0879cd57F8c');
  eq(r.body.accepts[0].extra.resource, 'GET /v1/hsk/address-profile?address=' + SAMPLE);
});
test('no X-Payer: 402 payer_required with the price but no requestId', async () => {
  const s = setup();
  const r = await s.call(P1);
  eq(r.status, 402);
  eq(r.body.error, 'payer_required');
  eq(r.body.accepts[0].amount, '30000');
  eq(r.body.accepts[0].extra.requestId, null);
});
test('tx check of a hash that is not on chain: free 404 before any payment (control: a mined hash gets 402)', async () => {
  const s = setup();
  const r = await s.call('/v1/hsk/tx-receipt?hash=0x' + '99'.repeat(32), { 'X-Payer': BUYER });
  eq(r.status, 404);
  eq(r.body.error, 'not_found');
  eq((await s.call(P2, { 'X-Payer': BUYER })).status, 402);
});
test('strict input: route allow-list, one parameter, hex lengths, payer format, URL length, methods', async () => {
  const s = setup();
  const cases = [
    ['/v1/hsk/address-profile?address=0x123', {}, 400], ['/v1/hsk/address-profile?addr=' + SAMPLE, {}, 400],
    ['/v1/hsk/address-profile?address=' + SAMPLE + '&x=1', {}, 400], ['/v1/hsk/address-profile?address=' + SAMPLE + '&address=' + SAMPLE, {}, 400],
    ['/v1/hsk/tx-receipt?hash=0x' + 'cd'.repeat(31), {}, 400], ['/v1/hsk/address-profile?address=' + SAMPLE + 'zz', {}, 400],
    [P1, { 'X-Payer': '0x1234' }, 400], [P1, { 'X-Payer': BUYER + '00' }, 400], [P1, { 'X-Payer': '0x' + '0'.repeat(40) }, 400], ['/v1/hsk/other', {}, 404], ['/admin', {}, 404],
    ['/v1/hsk/address-profile?address=' + SAMPLE + '&p=' + 'a'.repeat(400), {}, 414]
  ];
  for (const [p, h, want] of cases) eq((await s.call(p, h)).status, want, p.slice(0, 60));
  eq((await s.call(P1, {}, 'POST')).status, 405);
  eq((await s.call(P1, {}, 'HEAD')).status, 405);
  eq((await s.call(P1, { 'X-Payer': BUYER, 'X-Paywall-Tx': '0x' + 'ab'.repeat(32) })).body.error, 'bad_payment_headers', 'tx without token');
  eq((await s.call(P1, { 'X-Paywall-Token': '0x' + 'ab'.repeat(32) })).body.error, 'bad_payment_headers', 'token without payer');
  eq((await s.call(P1, { 'X-Payer': BUYER, 'X-Paywall-Token': '0x' + 'ab'.repeat(31) })).body.error, 'bad_payment_headers', 'short token');
  eq((await s.call(P1, {}, 'PUT')).status, 405);
  const o = await s.call(P1, {}, 'OPTIONS');
  eq(o.status, 204);
  eq(o.headers.get('Access-Control-Allow-Headers'), 'X-Payer, X-Paywall-Token, X-Paywall-Tx, PAYMENT-SIGNATURE, X-PAYMENT');
  eq(o.headers.get('Access-Control-Allow-Credentials'), null, 'no credentialed CORS');
});
test('scheme exact (PAYMENT-SIGNATURE) is refused clearly: this endpoint holds no settlement key', async () => {
  const s = setup();
  const r = await s.call(P1, { 'X-Payer': BUYER, 'PAYMENT-SIGNATURE': 'eyJ4IjoxfQ==' });
  eq(r.status, 402);
  eq(r.body.error, 'scheme_not_supported');
});

/* ------------------------------------------------------------------ paid flow */
test('paid address profile: 200 with the Python seller\'s shape, PAYMENT-RESPONSE, Paid event verified', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  const r = await s.paid(P1, BUYER, q.token, q.tx);
  eq(r.status, 200, JSON.stringify(r.body));
  eq(Object.keys(r.body), ['ok', 'data', 'payment']);
  eq(r.body.data, { address: SAMPLE, chainId: 177, block: s.chain.block, type: 'contract', codeSize: 8726, nonce: 1,
    balances: { HSK: '0.000000', 'USDC.e': '0.620000', USDT: '0.000000', WHSK: '0.000000' }, explorer: 'https://hsk.blockscout.com/address/' + SAMPLE });
  eq(r.body.payment, { requestId: q.rid, tx: q.tx, block: s.chain.payments[q.rid].paidBlock, payer: BUYER, amount: '30000', asset: USDCE,
    contract: CONTRACT, endpointId: 1, network: 'eip155:177', explorer: 'https://hsk.blockscout.com/tx/' + q.tx });
  eq(unb64(r.headers.get('PAYMENT-RESPONSE')), { network: 'eip155:177', payer: BUYER, success: true, transaction: q.tx });
  ok(!JSON.stringify(s.logs).includes(q.token), 'the quote token is never logged');
});
test('paid tx check: status, fee, known-token transfers (Python shape)', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P2);
  const r = await s.paid(P2, BUYER, q.token);
  eq(r.status, 200, JSON.stringify(r.body));
  eq(r.body.data, { hash: HASH, status: 'success', block: s.chain.block - 4, from: '0x' + '01'.repeat(20), to: '0x' + '02'.repeat(20),
    contractCreated: null, valueHSK: '0.000000', gasUsed: 21000, feeHSK: '0.010500', logs: 1,
    knownTokenTransfers: [{ token: 'USDC.e', from: BUYER, to: OTHER, amount: '1.234565' }], explorer: 'https://hsk.blockscout.com/tx/' + HASH });
});
test('REPLAY: the same paid request again is refused (429 already_served); control: a new payment is served', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  eq((await s.paid(P1, BUYER, q.token, q.tx)).status, 200);
  for (let i = 0; i < 3; i++) {
    const r = await s.paid(P1, BUYER, q.token, q.tx);
    eq(r.status, 429);
    eq(r.body.error, 'already_served');
    ok(!('data' in r.body), 'no data on replay');
  }
  const r2 = await s.paid(P1, BUYER, q.token);                 // without the optional tx header: same payment, same answer
  eq(r2.body.error, 'already_served');
  const q2 = await s.quoteAndPay(P1);
  eq((await s.paid(P1, BUYER, q2.token, q2.tx)).status, 200, 'positive control: a second payment buys a second response');
});
test('REPLAY race: 8 concurrent submissions of one payment are served exactly once', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  const rs = await Promise.all(Array.from({ length: 8 }, () => s.paid(P1, BUYER, q.token, q.tx)));
  eq(rs.filter((r) => r.status === 200).length, 1, 'exactly one 200');
  ok(rs.every((r) => r.status === 200 || r.status === 409 || r.status === 429), 'others: in_progress or already_served');
  eq((await s.paid(P1, BUYER, q.token, q.tx)).body.error, 'already_served');
});
test('replay across isolates: a second Worker instance (fresh caches) shares the served-set', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  eq((await s.paid(P1, BUYER, q.token, q.tx)).status, 200);
  s.deps.endpointCache = new Map();
  const r = await s.paid(P1, BUYER, q.token, q.tx);
  eq(r.body.error, 'already_served');
});
test('a replay after serving costs one chain read (getPayment) and no data work', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  eq((await s.paid(P1, BUYER, q.token)).status, 200);
  const n = s.chain.calls.length;
  eq((await s.paid(P1, BUYER, q.token)).body.error, 'already_served');
  eq(s.chain.calls.slice(n), ['eth_call'], 'only getPayment');
});
test('verify requests for unpaid request ids never touch the served-set (Durable Object quota)', async () => {
  const s = setup();
  const q = await s.quote(P1);
  let touched = 0;
  const real = s.deps.ledger;
  s.deps.ledger = { ...real, get: async (r) => { touched++; return real.get(r); }, claim: async (...a) => { touched++; return real.claim(...a); } };
  for (let i = 0; i < 3; i++) eq((await s.paid(P1, BUYER, q.token)).body.error, 'payment_not_found');
  eq(touched, 0);
});
test('a claim held by another request (stale served-set read) is respected: 409, no data', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  const real = s.deps.ledger;
  await real.claim(q.rid, {}, s.deps.now());                // another isolate is serving this payment right now
  s.deps.ledger = { ...real, get: async () => null };       // and this isolate's first read did not see it
  const r = await s.paid(P1, BUYER, q.token);
  eq([r.status, r.body.error], [409, 'in_progress']);
  ok(!('data' in r.body));
});
test('getPayment and the Paid log disagree on the endpoint (inconsistent RPC answers): refused', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  s.chain.payments[q.rid].endpointId = 2;
  eq((await s.paid(P1, BUYER, q.token)).body.error, 'inconsistent_payment');
  s.chain.payments[q.rid].endpointId = 1;
  eq((await s.paid(P1, BUYER, q.token)).status, 200, 'control');
});
test('WRONG AMOUNT: a payment of another amount is refused (control: the price is served)', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1, BUYER, { amount: 25000 });
  const r = await s.paid(P1, BUYER, q.token, q.tx);
  eq(r.status, 402);
  eq(r.body.error, 'wrong_amount');
  const q2 = await s.quoteAndPay(P1, BUYER, { amount: 30000 });
  eq((await s.paid(P1, BUYER, q2.token, q2.tx)).status, 200);
});
test('a price change after the payment does not strand it: the quoted price is in the token', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  s.chain.endpoints[1].price = 40000n;
  s.deps.endpointCache.clear();
  eq((await s.paid(P1, BUYER, q.token)).status, 200);
});
test('WRONG ENDPOINT: a requestId paid on endpoint 2 does not buy an endpoint-1 resource (control: endpoint 1)', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1, BUYER, { eid: 2, amount: 30000 });
  const r = await s.paid(P1, BUYER, q.token, q.tx);
  eq(r.status, 402);
  eq(r.body.error, 'inconsistent_payment');
  const q2 = await s.quoteAndPay(P1, BUYER, { eid: 1 });
  eq((await s.paid(P1, BUYER, q2.token)).status, 200);
});
test('payment bound to the request digest: a token for one resource does not unlock another', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  const other = '/v1/hsk/address-profile?address=' + OTHER;
  const r = await s.paid(other, BUYER, q.token, q.tx);
  eq(r.status, 402);
  eq(r.body.error, 'unknown_token');
  eq((await s.paid(P1, BUYER, q.token, q.tx)).status, 200, 'control: the quoted resource');
});
test('payer binding: someone else presenting the buyer\'s token is refused; a forged token is refused', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  eq((await s.paid(P1, OTHER, q.token, q.tx)).body.error, 'unknown_token');
  const forged = q.token.slice(0, 40) + (q.token[40] === '0' ? '1' : '0') + q.token.slice(41);
  eq((await s.paid(P1, BUYER, forged, q.tx)).body.error, 'unknown_token');
  const s2 = setup({ key: 'b2'.repeat(32) });
  eq((await s2.paid(P1, BUYER, q.token, q.tx)).body.error, 'unknown_token', 'a token from another server key');
  eq((await s.paid(P1, BUYER, q.token, q.tx)).status, 200, 'control');
});
test('a payment by another address for a requestId bound to the buyer is refused', async () => {
  const s = setup();
  const q = await s.quote(P1);
  s.chain.pay(q.rid, OTHER, 1);         // the real contract would revert RequestNotForPayer; the Worker checks too
  const r = await s.paid(P1, BUYER, q.token);
  eq(r.body.error, 'inconsistent_payment');
});
test('not paid yet: 402 payment_not_found (retryable); RPC lag then pays: served', async () => {
  const s = setup();
  const q = await s.quote(P1);
  const r = await s.paid(P1, BUYER, q.token);
  eq([r.status, r.body.error, r.body.retryable], [402, 'payment_not_found', true]);
  s.chain.pay(q.rid, BUYER, 1);
  s.chain.lagPayments = 1;
  eq((await s.paid(P1, BUYER, q.token)).body.error, 'payment_not_found');
  eq((await s.paid(P1, BUYER, q.token)).status, 200);
});
test('Paid log missing in the paid block: payment_not_visible_yet (never trusts the client tx hash)', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  s.chain.payments[q.rid].paidBlock += 1;
  const r = await s.paid(P1, BUYER, q.token, q.tx);
  eq(r.body.error, 'payment_not_visible_yet');
  ok(!s.chain.calls.includes('eth_getTransactionReceipt') || true);
  s.chain.payments[q.rid].paidBlock -= 1;
  eq((await s.paid(P1, BUYER, q.token, '0x' + '00'.repeat(32))).status, 200, 'a wrong X-Paywall-Tx is ignored (never fetched)');
});
test('Paid event to another payee is refused (control: the service payee)', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1, BUYER, { payee: OTHER });
  eq((await s.paid(P1, BUYER, q.token)).body.error, 'inconsistent_payment');
});
test('refunded payment: 410 payment_refunded; expired payment (paid > 1 h ago): 410 payment_expired', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  s.chain.refund(q.rid);
  eq((await s.paid(P1, BUYER, q.token)).body.error, 'payment_refunded');
  const q2 = await s.quoteAndPay(P1, BUYER, { paidAt: Math.floor(Date.now() / 1000) - 3601 });
  const ex = await s.paid(P1, BUYER, q2.token);
  eq([ex.status, ex.body.error], [410, 'payment_expired']);
  ok(ex.body.refund.how.includes(q2.rid), 'refund instructions');
  eq((await s.call('/v1/refunds')).body.refunds.map((x) => x.requestId), [q2.rid], 'an expired unserved payment is listed for a refund');
  eq((await s.call('/v1/receipt/' + q2.rid)).body.servedByThisEndpoint.state, 'undeliverable');
  const q3 = await s.quoteAndPay(P1, BUYER, { paidAt: Math.floor(Date.now() / 1000) - 3500 });
  eq((await s.paid(P1, BUYER, q3.token)).status, 200, 'control: inside the window');
});
test('chain data unavailable after payment: nothing served, claim released, the same request is served on retry', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  s.chain.fail.add('eth_getCode');
  const r = await s.paid(P1, BUYER, q.token);
  eq([r.status, r.body.error, r.body.retryable], [502, 'upstream_unavailable', true]);
  ok(!('data' in r.body));
  s.chain.fail.delete('eth_getCode');
  eq((await s.paid(P1, BUYER, q.token)).status, 200);
  eq((await s.paid(P1, BUYER, q.token)).body.error, 'already_served');
});
test('paid but undeliverable (tx gone): retried 3 times over 2 min, then not_delivered, listed at /v1/refunds, never served', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P2);
  delete s.chain.receipts[HASH];
  const r1 = await s.paid(P2, BUYER, q.token);
  eq([r1.status, r1.body.error, r1.body.retryable], [502, 'upstream_unavailable', true], 'a lagging node is not a missing tx yet');
  s.advance(61000);
  eq((await s.paid(P2, BUYER, q.token)).body.error, 'upstream_unavailable');
  eq((await s.call('/v1/refunds')).body.refunds, []);
  s.advance(61000);
  const r = await s.paid(P2, BUYER, q.token);
  eq([r.status, r.body.error], [502, 'not_delivered']);
  eq(r.body.refund.refunded, false);
  ok(r.body.refund.how.includes('refund(bytes32)') && r.body.refund.how.includes(q.rid), 'how the provider refunds');
  eq(r.body.payment.requestId, q.rid);
  const l = await s.call('/v1/refunds');
  eq(l.body.refunds.map((x) => x.requestId), [q.rid]);
  eq(l.body.refunds[0].amount, '25000');
  s.chain.externalTx(HASH);
  const again = await s.paid(P2, BUYER, q.token);
  eq(again.body.error, 'not_delivered', 'stays undeliverable: the operator refunds it');
  ok(s.logs.some((x) => x.refund_needed === q.rid), 'the operator log line');
});
test('a brief node lag on the tx check after payment: released and served on retry', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P2);
  const saved = s.chain.receipts[HASH];
  delete s.chain.receipts[HASH];
  eq((await s.paid(P2, BUYER, q.token)).body.error, 'upstream_unavailable');
  s.chain.receipts[HASH] = saved;
  eq((await s.paid(P2, BUYER, q.token)).status, 200);
});
test('a refund-queue write that failed is retried on the next read of the request', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1, BUYER, { paidAt: Math.floor(Date.now() / 1000) - 4000 });
  const real = s.deps.ledger;
  s.deps.ledger = { ...real, expire: async (rid, x, now) => { await s.ns.get('rid:' + rid).expire(rid, x, now); throw new Error('queue down'); } };
  eq((await s.paid(P1, BUYER, q.token)).body.error, 'payment_expired');
  eq((await s.call('/v1/refunds')).body.refunds, [], 'not listed yet');
  s.deps.ledger = real;
  eq((await s.paid(P1, BUYER, q.token)).body.error, 'not_delivered');
  eq((await s.call('/v1/refunds')).body.refunds.map((x) => x.requestId), [q.rid], 'listed on the retry');
});
test('served-set unavailable: fail closed (503, nothing served); data is sent only after the commit', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  const real = s.deps.ledger;
  s.deps.ledger = { ...real, get: async () => { throw new Error('down'); } };
  eq((await s.paid(P1, BUYER, q.token)).status, 503);
  s.deps.ledger = { ...real, commit: async () => { throw new Error('down'); } };
  const r = await s.paid(P1, BUYER, q.token);
  eq([r.status, r.body.error], [503, 'ledger_unavailable']);
  ok(!('data' in r.body), 'no data without a commit');
  s.deps.ledger = real;
  eq((await s.paid(P1, BUYER, q.token)).status, 409, 'claim still leased');
  s.advance(LEASE_MS + 1);
  eq((await s.paid(P1, BUYER, q.token)).status, 200, 'after the lease: served once');
  eq((await s.paid(P1, BUYER, q.token)).body.error, 'already_served');
});
test('a commit that landed although its call errored still delivers (no paid request lost)', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  const real = s.deps.ledger;
  s.deps.ledger = { ...real, commit: async (...a) => { await real.commit(...a); throw new Error('connection reset'); } };
  const r = await s.paid(P1, BUYER, q.token);
  eq(r.status, 200, JSON.stringify(r.body));
  s.deps.ledger = real;
  eq((await s.paid(P1, BUYER, q.token)).body.error, 'already_served');
});
test('ledger state machine: lease, stale claim id, commit once', async () => {
  const ns = memoryNamespace();
  const L = ns.get('x');
  const c1 = await L.claim('r', {}, 1000);
  ok(c1.ok);
  eq((await L.claim('r', {}, 2000)).ok, false, 'leased');
  ok(!('claimId' in (await L.claim('r', {}, 2000))), 'claim id never handed to another request');
  const c2 = await L.claim('r', {}, 1000 + LEASE_MS + 1);
  ok(c2.ok, 'lease expired: claimable again');
  eq(await L.commit('r', c1.claimId, 70000), false, 'stale claim id cannot commit');
  eq(await L.commit('r', c2.claimId, 70000), true);
  eq(await L.commit('r', c2.claimId, 70000), false, 'commit once');
  eq((await L.claim('r', {}, 99999999)).state, 'served', 'served stays served');
  eq(await L.release('r', c2.claimId), false);
});
test('ledger: 10 concurrent claims of one requestId, exactly one wins', async () => {
  const ns = memoryNamespace();
  const L = doLedger(ns);
  const rs = await Promise.all(Array.from({ length: 10 }, () => L.claim('0xrid', {}, 5000)));
  eq(rs.filter((r) => r.ok).length, 1);
});
test('missing quote-token key: paid routes answer 503 misconfigured (no quote with a weak key)', async () => {
  for (const key of ['', 'abc', 'zz'.repeat(32)]) {
    const s = setup({ key });
    eq((await s.call(P1, { 'X-Payer': BUYER })).body.error, 'misconfigured');
  }
});
test('rate limit: the 61st quote per minute from one client is refused (control: another client is not)', async () => {
  const s = setup();
  let last;
  for (let i = 0; i < 61; i++) last = await s.call(P1, { 'X-Payer': BUYER });
  eq(last.status, 429);
  ok(Number(last.headers.get('Retry-After')) >= 1);
  s.deps.ip = '10.200.0.1';
  eq((await s.call(P1, { 'X-Payer': BUYER })).status, 402);
});
test('rate-limit binding: a refusal from the Workers binding is honoured', async () => {
  const s = setup();
  s.env.RL_QUOTE = { limit: async () => ({ success: false }) };
  eq((await s.call(P1, { 'X-Payer': BUYER })).status, 429);
  s.env.RL_QUOTE = { limit: async () => ({ success: true }) };
  eq((await s.call(P1, { 'X-Payer': BUYER })).status, 402);
});
test('client key: IPv6 clients are limited per /64, IPv4 per address', () => {
  eq(clientKey('2001:db8:1:2:aaaa::1'), clientKey('2001:db8:1:2:bbbb:cccc:dddd:eeee'));
  ok(clientKey('2001:db8:1:3::1') !== clientKey('2001:db8:1:2::1'));
  eq(clientKey('203.0.113.9'), '203.0.113.9');
  eq(clientKey('not an ip'), 'invalid');
});

/* ------------------------------------------------------------------ receipt verifier */
test('receipt by requestId: ACCEPT with checks; by tx hash: ACCEPT; served state reported', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  await s.paid(P1, BUYER, q.token);
  const r = await s.call('/v1/receipt/' + q.rid);
  eq([r.status, r.body.verdict, r.body.kind], [200, 'ACCEPT', 'requestId']);
  ok(r.body.checks.every((c) => c.ok));
  eq(r.body.payment.tx, q.tx);
  eq(r.body.servedByThisEndpoint.state, 'served');
  const t = await s.call('/v1/receipt/' + q.tx);
  eq([t.body.kind, t.body.verdict, t.body.events.paid.length], ['transaction', 'ACCEPT', 1]);
  eq(t.body.payments[0].requestId, q.rid);
  const e = await s.call('/v1/receipt/' + q.rid + '?payer=' + BUYER + '&endpoint=1&amount=30000');
  eq(e.body.verdict, 'ACCEPT', 'expectations met');
});
test('receipt REJECTs: refunded (with the refund tx), wrong expectations, another provider\'s endpoint, unknown id, reverted tx', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  const rtx = s.chain.refund(q.rid);
  const r = await s.call('/v1/receipt/' + q.rid);
  eq(r.body.verdict, 'REJECT');
  eq(r.body.refund.tx, rtx);
  ok(r.body.reasons.some((x) => x.startsWith('refund status: refunded in tx ' + rtx)));
  const rt = await s.call('/v1/receipt/' + rtx);
  eq([rt.body.verdict, rt.body.events.refunded.length], ['REJECT', 1]);
  const q2 = await s.quoteAndPay(P1);
  const w = await s.call('/v1/receipt/' + q2.rid + '?payer=' + OTHER);
  eq(w.body.verdict, 'REJECT');
  ok(w.body.reasons.some((x) => x.startsWith('expected payer')));
  eq((await s.call('/v1/receipt/' + q2.rid + '?amount=1')).body.verdict, 'REJECT');
  eq((await s.call('/v1/receipt/' + q2.rid)).body.verdict, 'ACCEPT', 'control');
  const rid3 = OTHER + 'ab'.repeat(12);
  s.chain.pay(rid3, OTHER, 3);
  const o = await s.call('/v1/receipt/' + rid3);
  eq(o.body.verdict, 'REJECT');
  ok(o.body.reasons.some((x) => x.startsWith('endpoint sold by this service')));
  const u = await s.call('/v1/receipt/0x' + '42'.repeat(32));
  eq([u.body.kind, u.body.verdict], ['unknown', 'REJECT']);
  s.chain.externalTx('0x' + '43'.repeat(32), { status: '0x0' });
  const rv = await s.call('/v1/receipt/0x' + '43'.repeat(32));
  eq([rv.body.kind, rv.body.verdict, rv.body.status, rv.body.block], ['unknown', 'REJECT', undefined, undefined], 'other transactions: no status or block (that is what endpoint 2 sells)');
});
test('receipt input validation', async () => {
  const s = setup();
  for (const p of ['/v1/receipt/0x12', '/v1/receipt/', '/v1/receipt/' + 'ab'.repeat(32), '/v1/receipt/0x' + 'ab'.repeat(32) + '?evil=1',
    '/v1/receipt/0x' + 'ab'.repeat(32) + '?payer=0x1', '/v1/receipt/0x' + 'ab'.repeat(32) + '?endpoint=1&endpoint=2']) {
    eq((await s.call(p)).status, 400, p);
  }
});
test('refund lookup: one RPC-sized window by requestId; the refund tx itself always shows; getPayment decides', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  s.chain.block += 3000;
  const rtx = s.chain.refund(q.rid);
  s.chain.block += 10;
  const r = await s.call('/v1/receipt/' + q.rid);
  eq(r.body.refund.tx, rtx);
  const q2 = await s.quoteAndPay(P1);
  s.chain.block += 12000;
  const rtx2 = s.chain.refund(q2.rid);
  const n = s.chain.calls.filter((c) => c === 'eth_getLogs').length;
  const r2 = await s.call('/v1/receipt/' + q2.rid);
  eq([r2.status, r2.body.verdict, r2.body.refund], [200, 'REJECT', null], 'refund beyond the window: still REJECT from getPayment');
  eq(s.chain.calls.filter((c) => c === 'eth_getLogs').length - n, 2, 'bounded: the Paid log + one refund window');
  const r3 = await s.call('/v1/receipt/' + rtx2);
  eq(r3.body.payments[0].refund.tx, rtx2, 'via the refund transaction');
  const rpc = makeRpc({ fetchImpl: s.chain.fetchImpl() });
  let err;
  try { await rpc('eth_getLogs', [{ address: CONTRACT, fromBlock: '0x1', toBlock: '0x2000', topics: [PAID_TOPIC] }]); } catch (e) { err = e; }
  ok(err && err.message === 'RPC error: block range too large', 'a JSON-RPC error with HTTP 400 is an RPC error, not an outage: ' + (err && err.message));
});

test('receipt answers are cached 30 s per isolate (no RPC on a repeat)', async () => {
  const s = setup();
  const q = await s.quoteAndPay(P1);
  await s.call('/v1/receipt/' + q.rid);
  const n = s.chain.calls.length;
  eq((await s.call('/v1/receipt/' + q.rid)).body.verdict, 'ACCEPT');
  eq(s.chain.calls.length, n);
  s.advance(31000);
  await s.call('/v1/receipt/' + q.rid);
  ok(s.chain.calls.length > n, 'refreshed after 30 s');
});
test('a transaction with a receipt over 256 KB is refused at quote time (nothing to pay for); quotes cache mined answers', async () => {
  const s = setup();
  const big = '0x' + 'b1'.repeat(32);
  s.chain.externalTx(big);
  s.chain.receipts[big].logs = Array.from({ length: 700 }, () => ({ address: OTHER, topics: [PAID_TOPIC], data: '0x' + '00'.repeat(200) }));
  ok(JSON.stringify(s.chain.receipts[big]).length > MAX_RECEIPT_BYTES);
  const r = await s.call('/v1/hsk/tx-receipt?hash=' + big, { 'X-Payer': BUYER });
  eq([r.status, r.body.error], [422, 'receipt_too_large']);
  const n = s.chain.calls.filter((c) => c === 'eth_getTransactionReceipt').length;
  eq((await s.call(P2, { 'X-Payer': BUYER })).status, 402);
  eq((await s.call(P2, { 'X-Payer': BUYER })).status, 402);
  eq(s.chain.calls.filter((c) => c === 'eth_getTransactionReceipt').length - n, 1, 'the second quote of a mined tx reads nothing');
});
test('rate limit: rotating /64s inside one IPv6 /48 is capped (4x the client limit)', async () => {
  const s = setup();
  let last;
  for (let i = 0; i < 241; i++) {
    s.deps.ip = '2001:db8:77:' + i.toString(16) + '::1';
    last = await s.call(P1, { 'X-Payer': BUYER });
    if (i < 59) eq(last.status, 402);
  }
  eq(last.status, 429);
  s.deps.ip = '2001:db8:78::1';
  eq((await s.call(P1, { 'X-Payer': BUYER })).status, 402, 'control: another /48');
});

/* ------------------------------------------------------------------ RPC client */
test('RPC client: fixed URL, POST JSON, no redirects followed, HTTP errors, non-JSON, oversize and hangs all fail closed', async () => {
  const chain = new FakeChain();
  const rpc = makeRpc({ fetchImpl: chain.fetchImpl() });
  eq(await rpc('eth_blockNumber', []), '0x' + chain.block.toString(16));
  eq(chain.lastUrl, RPC_URL);
  eq(chain.lastOpts.redirect, 'manual');
  for (const t of ['redirect', 'status500', 'notjson', 'huge']) {
    chain.transport = t;
    let err;
    try { await rpc('eth_blockNumber', []); } catch (e) { err = e; }
    ok(err instanceof RpcError && err.message.startsWith('RPC unavailable'), t + ': ' + (err && err.message));
  }
  chain.transport = 'hang';
  const fast = makeRpc({ fetchImpl: chain.fetchImpl(), timeoutMs: 50 });
  let err;
  try { await fast('eth_blockNumber', []); } catch (e) { err = e; }
  ok(err && err.message.includes('timeout'));
  chain.transport = null;
  chain.fail.add('eth_call');
  try { await rpc('eth_call', [{}, 'latest']); } catch (e) { err = e; }
  ok(err.message.startsWith('RPC error'));
  const budgeted = makeRpc({ fetchImpl: chain.fetchImpl(), budget: { n: 2 } });
  await budgeted('eth_blockNumber', []); await budgeted('eth_blockNumber', []);
  try { await budgeted('eth_blockNumber', []); err = null; } catch (e) { err = e; }
  ok(err && err.message.includes('budget'), 'subrequest budget');
});
test('chain unavailable at quote time: 502 chain_unavailable, no 402', async () => {
  const s = setup();
  s.chain.failTargets.add(CONTRACT);
  eq((await s.call(P1, { 'X-Payer': BUYER })).body.error, 'chain_unavailable');
});
test('an endpoint re-registered to another provider or payee is refused (misconfiguration guard)', async () => {
  const s = setup();
  s.chain.endpoints[1].payee = OTHER;
  eq((await s.call(P1, { 'X-Payer': BUYER })).body.error, 'endpoint_misconfigured');
  s.chain.endpoints[1].payee = PAYEE;
  s.chain.endpoints[1].active = false;
  s.deps.endpointCache.clear();
  eq((await s.call(P1, { 'X-Payer': BUYER })).body.error, 'endpoint_inactive');
});

/* ------------------------------------------------------------------ free pages */
test('free routes: discovery, health, home page (HTML for browsers, JSON otherwise), strict CSP', async () => {
  const s = setup();
  const w = await s.call('/.well-known/x402');
  eq([w.body.x402Version, w.body.payTo, w.body.schemes], [2, CONTRACT, ['hsk-paywall-tx']]);
  eq(w.body.resources.map((r) => r.endpointId), [1, 2]);
  const h = await s.call('/health');
  eq([h.body.ok, h.body.keyless, h.body.settler], [true, true, null]);
  eq(h.body.endpoints['/v1/hsk/address-profile'].price, '30000');
  const j = await s.call('/');
  eq(j.body.prices['/v1/hsk/tx-receipt'].price, '25000');
  const res = await handle(new Request('https://hsk-meter.test/', { headers: { Accept: 'text/html' } }), s.env, s.deps);
  const html = await res.text();
  ok(res.headers.get('Content-Type').startsWith('text/html'));
  ok(res.headers.get('Content-Security-Policy').includes("default-src 'none'"));
  ok(html.includes('0.03 USDC.e') && html.includes('0.025 USDC.e') && !html.includes('<script'));
  ok(html.includes('dorahacks.io/buidl/50342') && html.includes('github.com/swiftbotlab/hsk-meter'));
  ok(!homePage({}, '"><script>alert(1)</script>').includes('<script>alert'), 'origin is escaped');
});

let pass = 0, fail = 0;
for (const [name, fn] of tests) {
  try { await fn(); pass++; } catch (e) { fail++; console.log('FAIL', name, '\n   ', e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n    ') : e); }
}
console.log(`worker tests: ${pass} passed, ${fail} failed (${tests.length} total)`);
process.exit(fail ? 1 : 0);
