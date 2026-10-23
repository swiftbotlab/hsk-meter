// Tests for the dashboard (docs/app.js) and its snapshot tool (tools/dashboard_snapshot.py). Offline: the page runs
// in a Node vm with a small fake DOM against a fake Blockscout (API v2 logs with topic filter and keyset paging,
// getLogs with topic operators, head block), and the snapshot tool runs against the same fake on 127.0.0.1.
//
//   node tools/test_dashboard.js [snapshot.json] [--expect-paid N --expect-refunded N --expect-withdrawn N]
//
// With --expect-* (USDC.e base units, from the contract's totalPaid/totalRefunded/totalWithdrawn) the totals the page
// computes from the snapshot must equal the contract's own counters (valid while only endpoints 1-2 exist).
// HSK_DASH_APP / HSK_DASH_TOOL point the suite at other copies of app.js / dashboard_snapshot.py (positive controls).
"use strict";
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");
const http = require("http");
const { execFile } = require("child_process");

const ROOT = path.join(__dirname, "..");
const APP_PATH = process.env.HSK_DASH_APP || path.join(ROOT, "docs", "app.js");
const TOOL_PATH = process.env.HSK_DASH_TOOL || path.join(ROOT, "tools", "dashboard_snapshot.py");
const APP_SRC = fs.readFileSync(APP_PATH, "utf8");
const A = require(APP_PATH);
const args = process.argv.slice(2);
const opt = (k) => { const i = args.indexOf(k); return i >= 0 ? BigInt(args[i + 1]) : null; };
const snapPath = args[0] && !args[0].startsWith("--") ? args[0] : path.join(ROOT, "docs", "snapshot.json");
const snap = JSON.parse(fs.readFileSync(snapPath, "utf8"));

let passed = 0, failed = 0;
const queue = [];
const test = (name, fn) => queue.push([name, fn]);

// ------------------------------------------------------------------ constants of the real deployment
const CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c";
const CONTRACT_CS = "0x78C8476772BC209C3df09Da5e129d0879cd57F8c";
const DEPLOY_BLOCK = 29212085;
const PROVIDER = "0x30ccc6d816854d1a66fbfece15b1515a3e941160";
const PAYEE = "0xa8a49a6ca872d54a8fd5fb121139de9bff62a48d";
const BUYER = "0x5270fe01fd3d6148b687da0b5693b0425cfca361";
const BUYER2 = "0x1234567890abcdef1234567890abcdef12345678";
const ATTACKER = "0xbad0000000000000000000000000000000000bad";
const PIN = { 1: "0xec7dd370d753d746a264e3bd13acdca1733b323132e5cbe5184a573ec56f27b4", 2: "0x97e3dd2d6b5cc86a0a81231f84deaf6ae706bbbcf6a2bf916c3750f170464a82" };
const T = {
  paid: "0x0f3c07e40340ee795b942048b66e7145339580e319ff1669f015e659b810063b",
  paidAuth: "0x9f4dc7c120af85f9cf7a6b7aa25e3982776d963a36e2e3e9bc04c4670d79e431",
  refunded: "0xadb1dca52dfad065e50a1e25c2ee47ae54013a1f2d6f8ea5abace52eb4b7a4c8",
  withdrawn: "0xd1c19fbcd4551a5edfb66d43d2e337c04837afda3482b42bdf569a8fccdae5fb",
  registered: "0x77dd24cff796128cd07781fd29ca7e3193c52b494cfe6761cffb516a7c989c88",
  priceChanged: "0x2bce37c591c5b0d254c3056688b080a088f160fff82b6e79f456c8a20d5570f6",
  approval: "0xb79c0c27cf054a9747d7903bf1de53411bd52dc10dd105c69abb53c82c1b61e7",
};
const LIVE_SIGS = [T.paid, T.refunded, T.withdrawn, T.paidAuth, T.priceChanged, T.registered];
const EXACT = [
  { topic1: w("1"), topic3: w(PROVIDER), topic1_3_opr: "or" },
  { topic1: w("2") },
  { topic1: w(PAYEE), topic3: w(PAYEE), topic1_3_opr: "or" },
];
function w(x) { return "0x" + (typeof x === "bigint" || typeof x === "number" ? x.toString(16) : String(x).replace(/^0x/, "")).padStart(64, "0"); }
const hx = (n) => "0x" + n.toString(16);

// ------------------------------------------------------------------ a fake chain: logs in the API v2 item shape
class World {
  constructor() { this.logs = []; this.block = DEPLOY_BLOCK + 260; this.n = 0; this.head = null; }
  log(topics, data, o = {}) {
    const block = o.block !== undefined ? o.block : ++this.block;
    if (block > this.block) this.block = block;
    const tx = o.tx || w((0xfeed000000 + ++this.n).toString(16));
    const lg = { address: { hash: CONTRACT_CS }, transaction_hash: tx, topics, data: data || "0x", block_number: block,
      index: o.index || 0, block_timestamp: new Date(Date.UTC(2026, 9, 23, 10) + (block - DEPLOY_BLOCK) * 2000).toISOString() };
    this.logs.push(lg);
    return lg;
  }
  approve(payee, provider) { return this.log([T.approval, w(payee), w(provider)], w("1")); }
  register(id, provider, payee, price, o = {}) {
    return this.log([T.registered, w(id), w(provider), w(payee)], w(price) + w(64).slice(2) + w(4).slice(2) + "54455354".padEnd(64, "0"), o);
  }
  pay(id, amount, o = {}) {
    const payer = o.payer || BUYER, payee = o.payee || PAYEE;
    const rid = w(payer.slice(2) + (0xc0ffee000000 + ++this.n).toString(16).padStart(24, "0"));
    const p = this.log([T.paid, w(id), rid, w(payer)], w(payee) + w(amount).slice(2), { block: o.block, index: o.index });
    if (o.gasless) this.log([T.paidAuth, rid, w("ab" + this.n), w(o.submitter || PROVIDER)], "0x", { block: p.block_number, tx: p.transaction_hash, index: (o.index || 0) + 1 });
    return { rid, payer, id, amount };
  }
  refund(p) { return this.log([T.refunded, w(p.id), p.rid, w(p.payer)], w(p.amount)); }
  withdraw(account, to, amount, o = {}) { return this.log([T.withdrawn, w(account), w(to)], w(amount), o); }
  price(id, oldP, newP) { return this.log([T.priceChanged, w(id)], w(oldP) + w(newP).slice(2)); }
  maxBlock() { return Math.max(...this.logs.map((l) => l.block_number)); }
  headBlock() { return this.head !== null ? this.head : this.maxBlock() + 20; }
}

// The real history in miniature: 9 payments (2 refunded), a withdrawal, two price changes.
function baseWorld() {
  const x = new World();
  x.approve(PAYEE, PROVIDER);
  x.register(1, PROVIDER, PAYEE, 10000, { tx: PIN[1], block: DEPLOY_BLOCK + 269 });
  x.register(2, PROVIDER, PAYEE, 5000, { tx: PIN[2], block: DEPLOY_BLOCK + 271 });
  x.pay(1, 10000, { gasless: true });
  x.pay(2, 5000, { gasless: true });
  x.refund(x.pay(2, 5000, { gasless: true }));
  x.pay(1, 10000);
  x.withdraw(PAYEE, PAYEE, 15000);
  x.price(1, 10000, 30000);
  x.price(2, 5000, 25000);
  x.pay(1, 30000, { gasless: true });
  x.pay(2, 25000, { gasless: true });
  x.refund(x.pay(1, 30000, { gasless: true }));
  x.snapBlock = x.block + 5;
  x.block = x.snapBlock + 10;
  return x;
}
function addLive(x) {           // two payments after the snapshot block
  x.pay(1, 30000, { gasless: true });
  x.pay(2, 25000, { payer: BUYER2 });
  return x;
}
// 9 payments: 170000 paid, 2 refunds 35000, 15000 withdrawn -> credit 120000; 7 gasless, 2 payers
const FULL_EXPECT = { count: "9", paid: "0.17", refund: "22.2%", withdrawn: "0.015", credit: "credit left 0.12 USDC.e" };

function spam(x, n, make) {     // n logs, 400 per block (a loop contract), all newer than everything else
  const start = x.block + 1;
  for (let i = 0; i < n; i++) make(x, { block: start + Math.floor(i / 400), index: i % 400, tx: w("5" + Math.floor(i / 400).toString(16).padStart(4, "0")) });
  return x;
}

// Topic match as Blockscout getLogs applies it (topic{i}, topic{i}_{j}_opr).
function exactMatch(lg, q) {
  const tops = lg.topics.map((t) => (t ? t.toLowerCase() : null));
  const pos = [0, 1, 2, 3].filter((i) => q["topic" + i] !== undefined);
  const m = (i) => tops[i] === q["topic" + i].toLowerCase();
  if (pos.length === 1) return m(pos[0]);
  let ok = true;
  for (let a = 0; a < pos.length; a++) for (let b = a + 1; b < pos.length; b++) {
    const op = q["topic" + pos[a] + "_" + pos[b] + "_opr"];
    if (op !== "and" && op !== "or") throw new Error("missing operator");
    ok = ok && (op === "and" ? m(pos[a]) && m(pos[b]) : m(pos[a]) || m(pos[b]));
  }
  return ok;
}
const asc = (a, b) => a.block_number - b.block_number || a.index - b.index;
function snapshotOf(x, toBlock) {   // what tools/dashboard_snapshot.py writes, built from the fake chain
  const items = x.logs.filter((l) => l.block_number <= toBlock && EXACT.some((q) => exactMatch(l, q))).sort(asc)
    .map((l) => ({ ...l, address: { hash: CONTRACT }, topics: l.topics.map((t) => t.toLowerCase()) }));
  return { generatedAt: new Date(Date.UTC(2026, 9, 23, 10) + (toBlock - DEPLOY_BLOCK) * 2000).toISOString(), fromBlock: DEPLOY_BLOCK,
    toBlock, complete: true, items };
}

// ------------------------------------------------------------------ the fake explorer + RPC
function explorer(x, o = {}) {
  const calls = { all: [], v2: [], logs: [], rpc: [], head: 0 };
  const word = (s, i) => s.slice(2 + 64 * i, 2 + 64 * (i + 1));
  function chainState(block) {         // getEndpoint/creditOf as the contract computes them
    const ep = {}, credit = {};
    const pays = new Map();
    for (const l of x.logs.slice().sort(asc)) {
      if (l.block_number > block) continue;
      const t = l.topics, id = t[1] ? BigInt(t[1]) : 0n;
      if (t[0] === T.registered) ep[id] = { provider: t[2], payee: t[3], price: BigInt("0x" + word(l.data, 0)), paidCount: 0n, totalPaid: 0n, totalRefunded: 0n, refundCount: 0n };
      else if (t[0] === T.priceChanged) ep[id].price = BigInt("0x" + word(l.data, 1));
      else if (t[0] === T.paid) {
        const amt = BigInt("0x" + word(l.data, 1)), payee = "0x" + word(l.data, 0).slice(24);
        ep[id].paidCount++; ep[id].totalPaid += amt; credit[payee] = (credit[payee] || 0n) + amt; pays.set(t[2], { id, amt, payee });
      } else if (t[0] === T.refunded) {
        const p = pays.get(t[2]); ep[id].totalRefunded += p.amt; ep[id].refundCount++; credit[p.payee] -= p.amt;
      } else if (t[0] === T.withdrawn) { const a = "0x" + t[1].slice(26); credit[a] = (credit[a] || 0n) - BigInt("0x" + word(l.data, 0)); }
    }
    return { ep, credit };
  }
  function rpc(req) {
    calls.rpc.push(req.method);
    const head = x.headBlock();
    if (req.method === "eth_blockNumber") return { jsonrpc: "2.0", id: req.id, result: hx(head) };
    if (req.method === "eth_call") {
      const [call, tag] = req.params;
      const st = chainState(tag === "latest" ? head : parseInt(tag, 16));
      const sel = call.data.slice(0, 10), arg = "0x" + call.data.slice(10);
      if (sel === "0x937bbc4a") {
        const e = st.ep[BigInt(arg)] || { provider: w("0"), payee: w("0"), price: 0n, paidCount: 0n, totalPaid: 0n, totalRefunded: 0n, refundCount: 0n };
        const skew = (o.skew && o.skew[Number(BigInt(arg))]) || 0n;
        const out = [e.provider, w(1), w(1761200000), e.payee, w(e.paidCount + skew), w(e.price), w(e.totalPaid), w(e.totalRefunded), w(e.refundCount)];
        return { jsonrpc: "2.0", id: req.id, result: "0x" + out.map((s) => s.slice(2)).join("") };
      }
      if (sel === "0x75807250") return { jsonrpc: "2.0", id: req.id, result: w(st.credit["0x" + arg.slice(26)] || 0n) };
    }
    return { jsonrpc: "2.0", id: req.id, error: { code: -32601, message: "no" } };
  }
  function handle(u, body) {           // -> [status, json]
    calls.all.push(u.href);
    const p = u.pathname.replace(/\/+$/, "");
    if (p.endsWith("/snapshot.json")) return o.snapshot ? [200, o.snapshot] : [404, {}];
    if (p === "/rpc") return [200, rpc(body)];
    if (p === "/api/v2/main-page/blocks") {
      calls.head++;
      if (o.failV2) return [429, {}];
      const h = x.headBlock();
      return [200, [0, 1, 2, 3].map((i) => ({ height: h - i, timestamp: "2026-10-24T00:00:00.000000Z" }))];
    }
    if (p.toLowerCase() === ("/api/v2/addresses/" + CONTRACT + "/logs")) {
      calls.v2.push(u.search);
      if (o.failV2) return [429, {}];
      const topic = u.searchParams.get("topic");
      let list = x.logs.slice().sort((a, b) => -asc(a, b));
      if (topic) list = list.filter((l) => l.topics.some((t) => t && t.toLowerCase() === topic.toLowerCase()));
      let start = 0;
      if (u.searchParams.has("block_number")) {
        const bn = Number(u.searchParams.get("block_number")), ix = Number(u.searchParams.get("index"));
        start = list.findIndex((l) => l.block_number < bn || (l.block_number === bn && l.index < ix));
        if (start < 0) start = list.length;
      }
      const items = list.slice(start, start + 50).map((l) => ({ ...l, topics: [...l.topics, null, null, null].slice(0, 4) }));
      const last = items[items.length - 1];
      const next = start + 50 < list.length ? { block_number: last.block_number, index: last.index, items_count: start + 50 } : null;
      return [200, { items, next_page_params: next }];
    }
    if (p === "/api" && u.searchParams.get("module") === "logs" && u.searchParams.get("action") === "getLogs") {
      calls.logs.push(u.search);
      if (o.failLogs || (o.logsBudget !== undefined && calls.logs.length > o.logsBudget)) return [429, {}];
      const q = Object.fromEntries(u.searchParams.entries());
      if (q.address.toLowerCase() !== CONTRACT) return [200, { status: "0", message: "No logs found", result: [] }];
      const from = Number(q.fromBlock), to = q.toBlock === "latest" ? x.headBlock() : Number(q.toBlock);
      const rows = x.logs.filter((l) => l.block_number >= from && l.block_number <= to && exactMatch(l, q)).sort(asc).slice(0, 1000)
        .map((l) => ({ address: CONTRACT, blockNumber: hx(l.block_number), data: l.data, gasPrice: "0x746a61ca40", gasUsed: "0x2b8f1",
          logIndex: hx(l.index), timeStamp: hx(Math.floor(Date.parse(l.block_timestamp) / 1000)),
          topics: [...l.topics, null, null, null].slice(0, 4), transactionHash: l.transaction_hash, transactionIndex: "0x1" }));
      return [200, rows.length ? { status: "1", message: "OK", result: rows } : { status: "0", message: "No logs found", result: [] }];
    }
    return [404, { message: "not found" }];
  }
  return { calls, handle };
}

// ------------------------------------------------------------------ the page in a vm with a fake DOM
function fakeDom() {
  const byId = {};
  class Text { constructor(t) { this.t = String(t); } get textContent() { return this.t; } }
  class Node {
    constructor(tag) {
      this.tag = tag; this.children = []; this._text = ""; this.attrs = {}; this.style = {}; this.listeners = {};
      this.hidden = false; this.disabled = false; this.className = ""; this.clientWidth = 600; this.offsetLeft = 0;
      this.offsetTop = 0; this.offsetWidth = 180; this.parentElement = null;
      const cls = new Set();
      this.classList = { add: (c) => cls.add(c), remove: (c) => cls.delete(c), toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)), contains: (c) => cls.has(c) };
    }
    set textContent(v) { this._text = String(v); this.children = []; }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
    append(...ns) { for (const n of ns) this.appendChild(typeof n === "string" ? new Text(n) : n); }
    appendChild(n) { n.parentElement = this; this.children.push(n); return n; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
    querySelectorAll() { return []; }
  }
  const card = new Node("section");
  const document = {
    body: new Node("body"), documentElement: new Node("html"), visibilityState: "visible", listeners: {},
    getElementById(id) { if (!byId[id]) { byId[id] = new Node("div"); byId[id].parentElement = card; } return byId[id]; },
    createElement: (t) => new Node(t), createElementNS: (_, t) => new Node(t), createTextNode: (t) => new Text(t),
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
  };
  return { document, byId };
}

async function page(x, o = {}) {
  let ex = explorer(x, o);
  const hosts = [];
  const fetch = async (url) => {
    const u = new URL(url, "https://swiftbotlab.github.io/hsk-meter/");
    if (u.origin !== "https://swiftbotlab.github.io" && u.origin !== "https://hsk.blockscout.com") hosts.push(u.origin);
    const [status, body] = ex.handle(u);
    return { ok: status === 200, status, headers: { get: () => null }, json: async () => JSON.parse(JSON.stringify(body)) };
  };
  const { document, byId } = fakeDom();
  const ctx = { window: { addEventListener() {} }, document, fetch, console, AbortController, URLSearchParams, URL, setTimeout, clearTimeout,
    setInterval: () => 0, getComputedStyle: () => ({ getPropertyValue: () => "#000" }) };
  vm.createContext(ctx);
  vm.runInContext(APP_SRC, ctx, { filename: "app.js" });
  const settle = async () => {
    for (let i = 0; i < 5000; i++) {
      await new Promise((r) => setImmediate(r));
      if (byId["source-state"] && byId["source-state"].textContent && !document.body.classList.contains("busy")) return;
    }
    throw new Error("page did not settle");
  };
  for (const f of document.listeners.DOMContentLoaded || []) f();
  await settle();
  const read = () => {
    const t = (id) => (byId[id] ? byId[id].textContent : "");
    return { state: t("source-state"), count: t("k-count"), countSub: t("k-count-sub"), paid: t("k-paid"), paidSub: t("k-paid-sub"),
      refund: t("k-refund"), withdrawn: t("k-withdrawn"), withdrawnSub: t("k-withdrawn-sub"), excluded: t("excluded"),
      note: t("partial-note"), noteHidden: byId["partial-note"] ? byId["partial-note"].hidden : true,
      partialTiles: ["k-count", "k-paid", "k-refund", "k-withdrawn"].filter((id) => byId[id] && byId[id].classList.contains("partial")) };
  };
  const reload = async (o2) => {
    if (o2) ex = explorer(x, o2);
    byId.refresh.listeners.click[0]();
    await settle();
  };
  return { read, reload, calls: () => ex.calls, hosts };
}

// ------------------------------------------------------------------ the snapshot tool against the fake on loopback
const DRIVER = `
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("snap", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
base = sys.argv[2]
if hasattr(m, "LOGS_API"): m.LOGS_API = base + "/api"
if hasattr(m, "RPC"): m.RPC = base + "/rpc"
if hasattr(m, "API"): m.API = base + "/api/v2"
for k, v in json.loads(sys.argv[3]).items(): setattr(m, k, v)
m.time.sleep = lambda s: None
sys.argv = ["dashboard_snapshot.py", "--out", sys.argv[4]]
m.main()
`;
async function runTool(x, o = {}, overrides = {}) {
  const ex = explorer(x, o);
  const srv = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => { body += c; });
    req.on("end", () => {
      let status = 500, json = { message: "fake explorer error" };
      try { [status, json] = ex.handle(new URL(req.url, "http://127.0.0.1"), body ? JSON.parse(body) : null); } catch (e) { json.message = String(e); }
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(json));
    });
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "hskdash-"));
  const out = path.join(dir, "snapshot.json");
  try {
    const r = await new Promise((resolve) => {
      execFile("python3", ["-I", "-c", DRIVER, TOOL_PATH, "http://127.0.0.1:" + srv.address().port, JSON.stringify(overrides), out],
        { timeout: 120000 }, (err, stdout, stderr) => resolve({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, stdout, stderr }));
    });
    r.written = fs.existsSync(out) ? JSON.parse(fs.readFileSync(out, "utf8")) : null;
    r.calls = ex.calls;
    return r;
  } finally {
    srv.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
}
const totals = (items) => {
  const a = A.aggregate(items);
  return { n: a.payments.length, paid: a.payments.reduce((s, p) => s + p.amount, 0n), ref: a.refunds.reduce((s, r) => s + r.amount, 0n), wd: a.withdrawn };
};

// ================================================================== unit tests (pure functions)
test("committed snapshot decodes", () => {
  const a = A.aggregate(snap.items);
  assert.ok(a.payments.length > 0);
  console.log("     payments " + a.payments.length + ", refunds " + a.refunds.length + ", withdrawn " + a.withdrawn +
    ", gasless " + a.payments.filter((p) => p.gasless).length + (snap.toBlock ? ", to block " + snap.toBlock : ""));
});

test("committed snapshot: complete, from the deploy block, totals equal the contract's counters it was checked against", () => {
  assert.strictEqual(snap.complete, true, "snapshot.complete");
  assert.ok(snap.fromBlock <= DEPLOY_BLOCK && snap.toBlock >= DEPLOY_BLOCK, "block range");
  const c = snap.counters && snap.counters.endpoints;
  assert.ok(c && c["1"] && c["2"], "snapshot.counters");
  const a = A.aggregate(snap.items);
  for (const id of [1, 2]) {
    const mine = a.payments.filter((p) => p.id === id), refs = a.refunds.filter((r) => r.id === id);
    assert.strictEqual(String(mine.length), c[id].paidCount, "paidCount " + id);
    assert.strictEqual(String(mine.reduce((s, p) => s + p.amount, 0n)), c[id].totalPaid, "totalPaid " + id);
    assert.strictEqual(String(refs.length), c[id].refundCount, "refundCount " + id);
    assert.strictEqual(String(refs.reduce((s, r) => s + r.amount, 0n)), c[id].totalRefunded, "totalRefunded " + id);
  }
  if (snap.counters.creditOfPayee !== undefined) {
    const t = totals(snap.items);
    assert.strictEqual(String(t.paid - t.ref - t.wd), snap.counters.creditOfPayee, "creditOf(payee)");
  }
});

test("snapshot totals match the contract's totals (--expect-*)", () => {
  const ep = opt("--expect-paid"), er = opt("--expect-refunded"), ew = opt("--expect-withdrawn");
  if (ep === null) { console.log("     (no --expect-* given: skipped)"); return; }
  const a = A.aggregate(snap.items);
  assert.strictEqual(a.payments.reduce((s, p) => s + p.amount, 0n), ep, "totalPaid");
  assert.strictEqual(a.refunds.reduce((s, r) => s + r.amount, 0n), er, "totalRefunded");
  assert.strictEqual(a.withdrawn, ew, "totalWithdrawn");
  for (const r of a.refunds) assert.ok(a.payments.some((p) => p.refund === r), "every refund is matched to its payment");
});

test("DASH-1: ownership is pinned, not read from the oldest logs (no registration logs loaded at all)", () => {
  const x = baseWorld();
  const noRegs = x.logs.filter((l) => l.topics[0] !== T.registered);
  const t = totals(noRegs);
  assert.strictEqual(t.n, 7, "payments of endpoints 1-2 count without their EndpointRegistered logs");
  assert.strictEqual(t.paid, 115000n);
});

test("DASH-1: a forged EndpointRegistered for id 1 changes nothing (ids 1-2 are ours forever)", () => {
  const x = baseWorld();
  x.register(1, ATTACKER, ATTACKER, 1);
  const t = totals(x.logs);
  assert.strictEqual(t.n, 7);
});

test("payments to other endpoints are not counted", () => {
  const x = baseWorld();
  x.register(3, ATTACKER, ATTACKER, 999999);
  x.pay(3, 999999, { payee: ATTACKER });
  assert.strictEqual(totals(x.logs).n, 7);
});

test("logs from another address, malformed hex and junk are dropped", () => {
  const x = baseWorld();
  const good = x.logs.slice();
  const p = good.find((l) => l.topics[0] === T.paid);
  const other = { ...p, transaction_hash: w("e1"), address: { hash: "0x" + "33".repeat(20) } };
  const badTopic = { ...p, transaction_hash: w("e2"), topics: [T.paid, "0x<script>", w("1"), w("2")] };
  const badData = { ...p, transaction_hash: w("e3"), data: "0xzz" };
  const badTx = { ...p, transaction_hash: "javascript:alert(1)" };
  const hugeData = { ...p, transaction_hash: w("e5"), data: "0x" + "00".repeat(32 * 40) };
  const shortData = { ...p, transaction_hash: w("e6"), data: w("1") };
  const t = totals([...good, other, badTopic, badData, badTx, hugeData, shortData, null, 42, "x", {}]);
  assert.strictEqual(t.n, 7);
});

test("duplicate log entries (overlapping pages, snapshot + live) count once", () => {
  const x = baseWorld();
  const dup = x.logs.map((l) => ({ ...l }));
  assert.strictEqual(totals(x.logs.concat(dup, x.logs)).n, 7);
});

test("getLogs rows normalise to the API v2 shape; bad quantities are dropped", () => {
  const r = { address: CONTRACT, blockNumber: "0x1bdc01e", data: w(5), logIndex: "0x0", timeStamp: "0x6adb89bb",
    topics: [T.withdrawn, w(PAYEE), w(PAYEE), null], transactionHash: w("77") };
  const it = A.fromRpcLog(r);
  assert.strictEqual(it.block_number, 0x1bdc01e);
  assert.strictEqual(it.index, 0);
  assert.ok(A.cleanItem(it), "cleanItem accepts it");
  assert.strictEqual(A.fromRpcLog({ ...r, blockNumber: "1bdc01e" }), null);
  assert.strictEqual(A.fromRpcLog({ ...r, timeStamp: "0xffffffffffffffff" }), null);
  assert.strictEqual(A.fromRpcLog(null), null);
});

test("formatting", () => {
  assert.strictEqual(A.fmtUsd(30000n), "0.03");
  assert.strictEqual(A.fmtUsd(25000n), "0.025");
  assert.strictEqual(A.fmtUsd(1236206n), "1.236206");
  assert.strictEqual(A.fmtUsd(0n), "0.00");
  assert.strictEqual(A.fmtUsd(-5000n), "-0.005");
  assert.strictEqual(A.decimalsFor(0.025), 3);
  assert.strictEqual(A.decimalsFor(0.05), 2);
  assert.strictEqual(A.decimalsFor(1), 0);
});

// ================================================================== the page, end to end
test("page: snapshot + live events give the full totals", async () => {
  const x = addLive(baseWorld());
  const pg = await page(x, { snapshot: snapshotOf(x, x.snapBlock) });
  const r = pg.read();
  assert.ok(r.state.startsWith("Live"), r.state);
  assert.deepStrictEqual([r.count, r.paid, r.refund, r.withdrawn, r.withdrawnSub], [FULL_EXPECT.count, FULL_EXPECT.paid, FULL_EXPECT.refund, FULL_EXPECT.withdrawn, FULL_EXPECT.credit]);
  assert.strictEqual(r.countSub, "7 gasless (EIP-3009) · 2 payers");
  assert.ok(r.noteHidden);
});

test("page: no snapshot at all, everything read live from the deploy block", async () => {
  const x = addLive(baseWorld());
  const pg = await page(x, {});
  const r = pg.read();
  assert.ok(r.state.startsWith("Live"), r.state);
  assert.deepStrictEqual([r.count, r.paid, r.withdrawn], [FULL_EXPECT.count, FULL_EXPECT.paid, FULL_EXPECT.withdrawn]);
});

test("DASH-1: +2,100 ProviderApproval logs (anyone, ~6k gas each) cannot blank the page", async () => {
  const x = spam(addLive(baseWorld()), 2100, (x, o) => x.log([T.approval, w(ATTACKER), w("1")], w("0"), o));
  const pg = await page(x, { snapshot: snapshotOf(x, x.snapBlock) });
  const r = pg.read();
  assert.ok(r.state.startsWith("Live"), r.state);
  assert.deepStrictEqual([r.count, r.paid, r.withdrawn], [FULL_EXPECT.count, FULL_EXPECT.paid, FULL_EXPECT.withdrawn]);
  assert.strictEqual(pg.calls().logs.length, 0, "spam of an uncounted type needs no fallback query");
});

test("DASH-1: +2,100 Withdrawn logs of another account overflow the v2 window; the exact query fills in", async () => {
  const x = spam(addLive(baseWorld()), 2100, (x, o) => x.withdraw(ATTACKER, ATTACKER, 1, o));
  const pg = await page(x, { snapshot: snapshotOf(x, x.snapBlock) });
  const r = pg.read();
  assert.ok(r.state.startsWith("Live"), r.state);
  assert.deepStrictEqual([r.count, r.paid, r.refund, r.withdrawn, r.withdrawnSub], [FULL_EXPECT.count, FULL_EXPECT.paid, FULL_EXPECT.refund, FULL_EXPECT.withdrawn, FULL_EXPECT.credit]);
  assert.strictEqual(pg.calls().logs.length, 3, "three exact getLogs queries");
});

test("DASH-1: window overflows and the exact query is rate limited -> 'partial data', never a wrong total", async () => {
  const x = spam(addLive(baseWorld()), 2100, (x, o) => x.withdraw(ATTACKER, ATTACKER, 1, o));
  const pg = await page(x, { snapshot: snapshotOf(x, x.snapBlock), failLogs: true });
  const r = pg.read();
  assert.ok(r.state.startsWith("Partial data"), r.state);
  assert.strictEqual(r.withdrawn, "partial data");
  assert.deepStrictEqual(r.partialTiles, ["k-withdrawn"]);
  assert.deepStrictEqual([r.count, r.paid], [FULL_EXPECT.count, FULL_EXPECT.paid], "complete types still show their totals");
  assert.ok(!r.noteHidden && /Partial data/.test(r.note));
});

test("DASH-1: 2,100 payments to an attacker's own endpoint + rate-limited exact query -> payment tiles say 'partial data'", async () => {
  const x = addLive(baseWorld());
  x.register(3, ATTACKER, ATTACKER, 1);
  spam(x, 2100, (x, o) => x.pay(3, 1, { payer: ATTACKER, payee: ATTACKER, block: o.block, index: o.index }));
  const pg = await page(x, { snapshot: snapshotOf(x, x.snapBlock), failLogs: true });
  const r = pg.read();
  assert.ok(r.state.startsWith("Partial data"), r.state);
  assert.deepStrictEqual([r.count, r.paid, r.refund], ["partial data", "partial data", "partial data"]);
  assert.strictEqual(r.withdrawn, FULL_EXPECT.withdrawn);
  assert.ok(!/credit left/.test(r.withdrawnSub), "no derived credit from partial payment data");
});

test("DASH-1: every request is topic-filtered or exact, on the CSP host only", async () => {
  const x = spam(addLive(baseWorld()), 2100, (x, o) => x.withdraw(ATTACKER, ATTACKER, 1, o));
  const pg = await page(x, { snapshot: snapshotOf(x, x.snapBlock) });
  const c = pg.calls();
  assert.deepStrictEqual(pg.hosts, [], "no other origin");
  assert.ok(c.v2.length > 0);
  for (const q of c.v2) assert.ok(LIVE_SIGS.includes(new URLSearchParams(q).get("topic")), "v2 logs query without a counted topic: " + q);
  for (const q of c.logs) {
    const p = Object.fromEntries(new URLSearchParams(q).entries());
    assert.ok(Number(p.fromBlock) > x.snapBlock - 1 && Number(p.fromBlock) >= DEPLOY_BLOCK, "fromBlock " + p.fromBlock);
    const filt = Object.fromEntries(Object.entries(p).filter(([k]) => /^topic/.test(k)));
    assert.ok(EXACT.some((e) => JSON.stringify(Object.entries(e).sort()) === JSON.stringify(Object.entries(filt).sort())), "not an exact filter: " + q);
  }
});

test("DASH-1: after one exact fill-in, later refreshes skip a burst older than the covered block", async () => {
  const x = spam(addLive(baseWorld()), 2100, (x, o) => x.withdraw(ATTACKER, ATTACKER, 1, o));
  x.head = x.block + 400;                       // the burst is now > 300 blocks behind the head
  const snapshot = snapshotOf(x, x.snapBlock);
  const pg = await page(x, { snapshot });
  assert.strictEqual(pg.calls().logs.length, 3);
  x.pay(1, 30000, { gasless: true, block: x.head + 1 });
  x.head += 10;
  await pg.reload({ snapshot, failLogs: true });
  const r = pg.read();
  assert.ok(r.state.startsWith("Live"), r.state);
  assert.strictEqual(r.count, "10");
  assert.strictEqual(pg.calls().logs.length, 0, "no exact query needed");
});

test("DASH-2: a failed refresh keeps the newer live numbers instead of the older snapshot", async () => {
  const x = addLive(baseWorld());
  const snapshot = snapshotOf(x, x.snapBlock);
  const pg = await page(x, { snapshot });
  assert.strictEqual(pg.read().count, "9");
  await pg.reload({ snapshot, failV2: true, failLogs: true });
  const r = pg.read();
  assert.strictEqual(r.count, "9", "kept the last live data");
  assert.ok(r.state.startsWith("Refresh failed; showing data from"), r.state);
});

test("DASH-2: with no live load yet, an unreachable API shows the snapshot", async () => {
  const x = addLive(baseWorld());
  const pg = await page(x, { snapshot: snapshotOf(x, x.snapBlock), failV2: true });
  const r = pg.read();
  assert.strictEqual(r.count, "7");
  assert.ok(/showing a snapshot from/.test(r.state), r.state);
});

test("DASH-3: a 3rd endpoint paying the same payee: withdrawals are payee-wide, no negative 'credit left'", async () => {
  const x = new World();
  x.approve(PAYEE, PROVIDER);
  x.register(1, PROVIDER, PAYEE, 30000, { tx: PIN[1], block: DEPLOY_BLOCK + 269 });
  x.register(2, PROVIDER, PAYEE, 25000, { tx: PIN[2], block: DEPLOY_BLOCK + 271 });
  x.pay(1, 30000, { gasless: true });
  x.register(3, PROVIDER, PAYEE, 1000000);
  x.pay(3, 1000000);
  x.withdraw(PAYEE, PAYEE, 1030000);
  const pg = await page(x, {});
  const r = pg.read();
  assert.strictEqual(r.paid, "0.03");
  assert.strictEqual(r.withdrawn, "1.03");
  assert.ok(!/credit left/.test(r.withdrawnSub) && !/-\d/.test(r.withdrawnSub), r.withdrawnSub);
  assert.ok(/payee-wide/.test(r.withdrawnSub) && /endpoint 3/.test(r.withdrawnSub), r.withdrawnSub);
  assert.ok(/Endpoint 3 also pays this payee/.test(r.excluded), r.excluded);
});

test("DASH-3: the derived credit is shown only when the payee has no other endpoint, and is exact then", async () => {
  const x = addLive(baseWorld());
  const r = (await page(x, { snapshot: snapshotOf(x, x.snapBlock) })).read();
  assert.strictEqual(r.withdrawnSub, FULL_EXPECT.credit);
});

// ================================================================== the snapshot tool
test("snapshot tool: complete, from the deploy block, exact filters only, checked against the contract's counters", async () => {
  const x = addLive(baseWorld());
  const r = await runTool(x);
  assert.strictEqual(r.code, 0, r.stderr);
  const s = r.written;
  assert.ok(s && s.complete === true && s.fromBlock === DEPLOY_BLOCK && s.toBlock === x.headBlock() - 12, "header");
  const t = totals(s.items);
  assert.deepStrictEqual([t.n, t.paid, t.ref, t.wd], [9, 170000n, 35000n, 15000n]);
  assert.strictEqual(s.counters.endpoints["1"].paidCount, "5");
  assert.strictEqual(s.counters.creditOfPayee, "120000");
  for (const q of r.calls.logs) {
    const p = Object.fromEntries(new URLSearchParams(q).entries());
    assert.strictEqual(Number(p.fromBlock), DEPLOY_BLOCK);
  }
  assert.strictEqual(r.calls.logs.length, 3);
});

test("DASH-1: snapshot tool ignores spam (+2,100 ProviderApproval, +2,100 Withdrawn of others)", async () => {
  const x = addLive(baseWorld());
  spam(x, 2100, (x, o) => x.log([T.approval, w(ATTACKER), w("1")], w("0"), o));
  spam(x, 2100, (x, o) => x.withdraw(ATTACKER, ATTACKER, 1, o));
  const r = await runTool(x);
  assert.strictEqual(r.code, 0, r.stderr);
  const t = totals(r.written.items);
  assert.deepStrictEqual([t.n, t.paid, t.wd], [9, 170000n, 15000n]);
});

test("DASH-1: snapshot tool exits non-zero and writes nothing when pages remain after its cap", async () => {
  const x = addLive(baseWorld());
  const b0 = x.block + 1;
  for (let i = 0; i < 1100; i++) x.pay(1, 30000, { block: b0 + Math.floor(i / 100), index: i % 100 });
  const r = await runTool(x, {}, { MAX_PAGES: 1 });
  assert.notStrictEqual(r.code, 0, "exit status");
  assert.strictEqual(r.written, null, "no file");
  assert.ok(/pages remain/.test(r.stderr), r.stderr.slice(-300));
});

test("snapshot tool refuses when the logs disagree with getEndpoint() (e.g. the explorer is behind)", async () => {
  const x = addLive(baseWorld());
  const r = await runTool(x, { skew: { 1: 1n } });
  assert.notStrictEqual(r.code, 0);
  assert.strictEqual(r.written, null);
  assert.ok(/getEndpoint\(1\)/.test(r.stderr), r.stderr.slice(-300));
});

test("snapshot tool refuses when endpoint 1 is not the pinned registration", async () => {
  const x = new World();
  x.register(1, ATTACKER, PAYEE, 10000, { block: DEPLOY_BLOCK + 269 });
  x.register(2, PROVIDER, PAYEE, 5000, { tx: PIN[2], block: DEPLOY_BLOCK + 271 });
  x.pay(1, 10000);
  const r = await runTool(x);
  assert.notStrictEqual(r.code, 0);
  assert.strictEqual(r.written, null);
  assert.ok(/pinned registration/.test(r.stderr), r.stderr.slice(-300));
});

(async () => {
  const only = process.env.HSK_DASH_ONLY ? new RegExp(process.env.HSK_DASH_ONLY) : null;
  for (const [name, fn] of queue) {
    if (only && !only.test(name)) continue;
    try {
      await fn();
      passed++;
      console.log("ok   " + name);
    } catch (e) {
      failed++;
      console.log("FAIL " + name + "\n     " + String(e && e.message ? e.message : e).split("\n").slice(0, 6).join("\n     "));
    }
  }
  console.log(passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})();
