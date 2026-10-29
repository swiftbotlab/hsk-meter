/* Offline stand-in for the HashKey Chain JSON-RPC, for the Worker's tests (Swift Bot Labs). State only: endpoints,
   payments, Paid/Refunded logs, receipts, balances. No network. Knobs: fail (methods that error), lagPayments
   (getPayment sees nothing for N calls), status/redirect/huge (transport faults), calls (every method called). */
import { calldata, decodeWords, bytesToHex, wordUint, wordAddress, concat, selector } from '../src/abi.js';
import { CONTRACT, USDCE, USDT, WHSK, PAID_TOPIC, REFUNDED_TOPIC, PROVIDER, PAYEE, TRANSFER_TOPIC } from '../src/core.js';

const SEL = (sig) => bytesToHex(selector(sig));
const S = { getEndpoint: SEL('getEndpoint(uint256)'), getPayment: SEL('getPayment(bytes32)'), balanceOf: SEL('balanceOf(address)') };
const ZERO = '0x' + '0'.repeat(40);
const enc = (words) => bytesToHex(concat(...words));
const topicUint = (n) => bytesToHex(wordUint(n));
const topicAddr = (a) => bytesToHex(wordAddress(a));

export class FakeChain {
  constructor() {
    this.block = 29400000;
    this.endpoints = { 1: { provider: PROVIDER, active: true, payee: PAYEE, price: 30000n }, 2: { provider: PROVIDER, active: true, payee: PAYEE, price: 25000n },
      3: { provider: '0xd680665b084bc70ef97b1fc14b73b1846e374a83', active: true, payee: '0xd680665b084bc70ef97b1fc14b73b1846e374a83', price: 80000n } };
    this.payments = {};      // rid -> {payer, endpointId, refunded, amount, paidAt, paidBlock}
    this.logs = [];
    this.receipts = {};
    this.txs = {};
    this.balances = {};      // token|holder -> BigInt
    this.native = {};
    this.code = {};
    this.nonces = {};
    this.fail = new Set();
    this.failTargets = new Set();
    this.lagPayments = 0;
    this.calls = [];
    this.transport = null;   // 'status500' | 'redirect' | 'huge' | 'notjson' | 'hang'
    this.n = 0;
  }

  /* a payment made with pay() by `payer` (amount defaults to the endpoint price), with its Paid log and receipt */
  pay(rid, payer, eid, { amount, paidAt, payee, block } = {}) {
    this.block += 1;
    const b = block || this.block;
    const e = this.endpoints[eid];
    const amt = amount === undefined ? e.price : BigInt(amount);
    const tx = '0x' + (++this.n).toString(16).padStart(4, '0') + 'aa'.repeat(30);
    this.payments[rid] = { payer: payer.toLowerCase(), endpointId: eid, refunded: false, amount: amt,
      paidAt: paidAt === undefined ? Math.floor(Date.now() / 1000) : paidAt, paidBlock: b };
    const lg = { address: CONTRACT, logIndex: '0x1', blockNumber: '0x' + b.toString(16), transactionHash: tx, removed: false,
      topics: [PAID_TOPIC, topicUint(eid), rid, topicAddr(payer.toLowerCase())],
      data: enc([wordAddress(payee || e.payee), wordUint(amt)]) };
    this.logs.push(lg);
    this.receipts[tx] = { status: '0x1', transactionHash: tx, blockNumber: '0x' + b.toString(16), gasUsed: '0x2b7cb',
      effectiveGasPrice: '0x746a528800', logs: [lg], from: payer.toLowerCase(), to: CONTRACT, contractAddress: null };
    this.txs[tx] = { value: '0x0' };
    return tx;
  }

  refund(rid) {
    const p = this.payments[rid];
    p.refunded = true;
    this.block += 1;
    const tx = '0x' + (++this.n).toString(16).padStart(4, '0') + 'bb'.repeat(30);
    const lg = { address: CONTRACT, logIndex: '0x0', blockNumber: '0x' + this.block.toString(16), transactionHash: tx,
      topics: [REFUNDED_TOPIC, topicUint(p.endpointId), rid, topicAddr(p.payer)], data: enc([wordUint(p.amount)]) };
    this.logs.push(lg);
    this.receipts[tx] = { status: '0x1', transactionHash: tx, blockNumber: '0x' + this.block.toString(16), gasUsed: '0x1d83c',
      effectiveGasPrice: '0x746a528800', logs: [lg], from: PROVIDER, to: CONTRACT };
    return tx;
  }

  /* a plain transaction with a USDC.e transfer, for the tx-receipt route */
  externalTx(hash, { status = '0x1', value = '0x0', transfer } = {}) {
    const logs = [];
    if (transfer) {
      logs.push({ address: transfer.token || USDCE, logIndex: '0x0', blockNumber: '0x' + (this.block - 3).toString(16), transactionHash: hash,
        topics: [TRANSFER_TOPIC, topicAddr(transfer.from), topicAddr(transfer.to)], data: enc([wordUint(BigInt(transfer.amount))]) });
    }
    this.receipts[hash] = { status, transactionHash: hash, blockNumber: '0x' + (this.block - 3).toString(16), gasUsed: '0x5208',
      effectiveGasPrice: '0x746a528800', logs, from: '0x' + '01'.repeat(20), to: '0x' + '02'.repeat(20), contractAddress: null };
    this.txs[hash] = { value };
  }

  setBalance(token, holder, v) { this.balances[token.toLowerCase() + '|' + holder.toLowerCase()] = BigInt(v); }

  rpc(method, params) {
    this.calls.push(method);
    if (this.fail.has(method)) return { error: { code: -32000, message: 'test failure' } };
    switch (method) {
      case 'eth_blockNumber': return { result: '0x' + this.block.toString(16) };
      case 'eth_getCode': return { result: this.code[params[0].toLowerCase()] || '0x' };
      case 'eth_getBalance': return { result: '0x' + (this.native[params[0].toLowerCase()] || 0n).toString(16) };
      case 'eth_getTransactionCount': return { result: '0x' + (this.nonces[params[0].toLowerCase()] || 0).toString(16) };
      case 'eth_getTransactionReceipt': return { result: this.receipts[params[0].toLowerCase()] || null };
      case 'eth_getTransactionByHash': return { result: this.txs[params[0].toLowerCase()] || null };
      case 'eth_getLogs': {
        const f = params[0];
        const lo = parseInt(f.fromBlock, 16), hi = parseInt(f.toBlock, 16);
        if (hi - lo > 5000) return { status: 400, error: { code: -32602, message: 'block range too large' } };
        return { result: this.logs.filter((l) => l.address.toLowerCase() === f.address.toLowerCase() && l.topics[0] === f.topics[0]
          && (!f.topics[2] || l.topics[2] === f.topics[2]) && parseInt(l.blockNumber, 16) >= lo && parseInt(l.blockNumber, 16) <= hi) };
      }
      case 'eth_call': return this.call(params[0]);
      default: return { error: { code: -32601, message: 'method not supported in FakeChain: ' + method } };
    }
  }

  call({ to, data }) {
    const t = to.toLowerCase();
    if (this.failTargets.has(t)) return { error: { code: -32000, message: 'test failure' } };
    const s = data.slice(0, 10);
    const arg = '0x' + data.slice(10);
    if (t === CONTRACT && s === S.getEndpoint) {
      const eid = Number(decodeWords(['uint256'], arg)[0]);
      const e = this.endpoints[eid];
      if (!e) return { result: enc(Array(9).fill(wordUint(0))) };
      return { result: enc([wordAddress(e.provider), wordUint(e.active ? 1 : 0), wordUint(1792771843), wordAddress(e.payee), wordUint(5),
        wordUint(e.price), wordUint(0), wordUint(0), wordUint(0)]) };
    }
    if (t === CONTRACT && s === S.getPayment) {
      const rid = decodeWords(['bytes32'], arg)[0];
      const p = this.payments[rid];
      if (!p || this.lagPayments > 0) {
        if (p) this.lagPayments -= 1;
        return { result: enc([wordAddress(ZERO), wordUint(0), wordUint(0), wordUint(0), wordUint(0), wordUint(0)]) };
      }
      return { result: enc([wordAddress(p.payer), wordUint(p.endpointId), wordUint(p.refunded ? 1 : 0), wordUint(p.amount), wordUint(p.paidAt), wordUint(p.paidBlock)]) };
    }
    if (s === S.balanceOf && [USDCE, USDT, WHSK].map((x) => x.toLowerCase()).includes(t)) {
      const who = decodeWords(['address'], arg)[0];
      return { result: bytesToHex(wordUint(this.balances[t + '|' + who] || 0n)) };
    }
    return { error: { code: 3, message: 'execution reverted', data: '0x' } };
  }

  /* a fetch() for the Worker's RPC client */
  fetchImpl() {
    return async (url, opts) => {
      this.lastUrl = url;
      this.lastOpts = opts;
      if (this.transport === 'redirect') return new Response('', { status: 301, headers: { location: 'http://169.254.169.254/' } });
      if (this.transport === 'status500') return new Response('oops', { status: 500 });
      if (this.transport === 'notjson') return new Response('<html>', { status: 200 });
      if (this.transport === 'huge') return new Response('{"result":"' + 'a'.repeat(1_200_000) + '"}', { status: 200 });
      if (this.transport === 'hang') {
        return new Promise((resolve, reject) => {
          opts.signal.addEventListener('abort', () => { const e = new Error('aborted'); e.name = 'AbortError'; reject(e); });
        });
      }
      const req = JSON.parse(opts.body);
      const { status, ...r } = this.rpc(req.method, req.params);
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: req.id, ...r }), { status: status || 200, headers: { 'content-type': 'application/json' } });
    };
  }
}

export { calldata };
