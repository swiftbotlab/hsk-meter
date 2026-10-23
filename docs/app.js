// HSK Meter live dashboard (Swift Bot Labs). Reads the settlement contract's event logs from the public Blockscout
// explorer in the browser, decodes them from raw topics/data (no reliance on the explorer's optional decoding) and
// counts only endpoints 1 and 2 of our provider. Every value from the API is validated and inserted with
// textContent; nothing from the network is ever parsed as HTML.
//
// Where the numbers come from:
// - Endpoints 1 and 2 are pinned to their registration transactions below. The contract fixes an endpoint's provider
//   and payee at registration and has no function that changes them, so ownership is never inferred from the logs.
// - snapshot.json (tools/dashboard_snapshot.py) holds every counted event from the deploy block up to a fixed block,
//   checked against the contract's own counters before it was written. The page builds on it and adds newer events.
// - Newer events: one topic-filtered query per counted event type on the explorer API v2 (newest first, down to the
//   block already covered), so other event types, such as anyone's approveProvider() calls, never enter the window.
//   If a type has more pages than the cap, the page asks the explorer's getLogs API with exact filters that only
//   payments to endpoints 1-2 or our own provider and payee can match. If that fails too (it allows only a few calls
//   per hour), the affected tiles say "partial data" instead of showing a total.
// - USD value: the APRO USDC/USD price feed on HashKey Chain (listed on docs.hskchain.net, Tools > Oracle). The page
//   reads the newest AnswerUpdated event of the feed's aggregator from the same explorer API v2 (its own address, so
//   only the aggregator can write those logs). tools/dashboard_snapshot.py checks that the feed proxy still points at
//   that aggregator and stores the proxy's latestRoundData() as the fallback. A round older than 26 h (the feeds'
//   heartbeat is 24 h), from the future or outside 0.5-1.5 USD is not used.
"use strict";
(function () {
  const HOST = "https://hsk.blockscout.com";
  const API_V2 = HOST + "/api/v2";
  const API_LOGS = HOST + "/api";          // Etherscan-style getLogs: exact topic filters and a block range
  const EXPLORER = HOST;
  const CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c";
  const DEPLOY_BLOCK = 29212085;
  const PROVIDER = "0x30ccc6d816854d1a66fbfece15b1515a3e941160";
  const PAYEE = "0xa8a49a6ca872d54a8fd5fb121139de9bff62a48d";
  const ENDPOINTS = {              // registration tx, block and price of each counted endpoint (provider/payee above)
    1: { name: "Address profile", regTx: "0xec7dd370d753d746a264e3bd13acdca1733b323132e5cbe5184a573ec56f27b4", regBlock: 29212354, price: 10000n },
    2: { name: "Transaction check", regTx: "0x97e3dd2d6b5cc86a0a81231f84deaf6ae706bbbcf6a2bf916c3750f170464a82", regBlock: 29212356, price: 5000n },
  };
  const OUR_IDS = [1, 2];
  const KNOWN = {                  // checksummed display form of the demo roles (topics carry lowercase addresses)
    "0x30ccc6d816854d1a66fbfece15b1515a3e941160": "0x30CCc6d816854d1a66fBfecE15b1515a3E941160",
    "0xa8a49a6ca872d54a8fd5fb121139de9bff62a48d": "0xA8A49A6Ca872D54a8FD5FB121139De9bff62a48D",
    "0x5270fe01fd3d6148b687da0b5693b0425cfca361": "0x5270fE01FD3D6148B687dA0b5693B0425CfCA361",
  };
  const MAX_V2_PAGES = 10;         // per event type and refresh: 500 logs newer than the covered block
  const MAX_LOG_PAGES = 3;         // per exact query and refresh; getLogs returns at most 1000 logs per call
  const LOG_PAGE = 1000;
  const LAG_BLOCKS = 300;          // the newest ~10 minutes before the explorer's head are always read again
  const MAX_ITEMS = 50000;
  const REFRESH_MS = 60000;
  const DAYS_SHOWN = 30;
  const T = {
    paid: "0x0f3c07e40340ee795b942048b66e7145339580e319ff1669f015e659b810063b",
    paidAuth: "0x9f4dc7c120af85f9cf7a6b7aa25e3982776d963a36e2e3e9bc04c4670d79e431",
    refunded: "0xadb1dca52dfad065e50a1e25c2ee47ae54013a1f2d6f8ea5abace52eb4b7a4c8",
    withdrawn: "0xd1c19fbcd4551a5edfb66d43d2e337c04837afda3482b42bdf569a8fccdae5fb",
    registered: "0x77dd24cff796128cd07781fd29ca7e3193c52b494cfe6761cffb516a7c989c88",
    priceChanged: "0x2bce37c591c5b0d254c3056688b080a088f160fff82b6e79f456c8a20d5570f6",
  };
  const LIVE_TYPES = ["paid", "refunded", "withdrawn", "paidAuth", "priceChanged", "registered"];
  const FEED = {                   // APRO USDC/USD on HashKey Chain mainnet (8 decimals)
    pair: "USDC/USD",
    proxy: "0x244Ce344df8837c9d938867E2Ffbf0E4B0169B56",
    aggregator: "0x86b79c0f9b58ca8fd72da05af3ed4c99c8140c79",
  };
  const T_ANSWER = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f";   // AnswerUpdated(int256,uint256,uint256)
  const FEED_MAX_AGE_S = 26 * 3600;
  const FEED_MIN = 50000000n, FEED_MAX = 150000000n;   // 0.5 .. 1.5 USD
  const FEED_REFRESH_MS = 300000;
  const RE_WORD = /^0x[0-9a-f]{64}$/;
  const RE_DATA = /^0x(?:[0-9a-f]{64})*$/;
  const RE_TX = /^0x[0-9a-f]{64}$/;
  const RE_QTY = /^0x[0-9a-f]{1,12}$/;
  const RE_TS = /^0x[0-9a-f]{1,10}$/;
  const pad = (hex) => "0x" + hex.replace(/^0x/, "").padStart(64, "0");
  const ADDR_PREFIX = "0x" + "0".repeat(24);
  // Exact getLogs filters (also used by tools/dashboard_snapshot.py). Only payments to endpoints 1-2 or our own
  // provider and payee keys can add matching logs, so nobody else can push our events out of these answers:
  // - topic1 = 1 or 2: EndpointRegistered, PriceChanged, ActiveSet, Paid and Refunded of our endpoints (plus at most
  //   one PaidWithAuthorization whose requestId is exactly 1 or 2); topic3 = provider: the PaidWithAuthorization
  //   logs the provider submitted (and endpoints or payments where the provider itself is payee or payer);
  // - topic1 or topic3 = payee: its Withdrawn and ProviderApproval logs and every EndpointRegistered that pays it
  //   (registering an endpoint for a payee needs that payee's approval).
  const EXACT_QUERIES = [
    { topic1: pad("1"), topic3: pad(PROVIDER), topic1_3_opr: "or" },
    { topic1: pad("2") },
    { topic1: pad(PAYEE), topic3: pad(PAYEE), topic1_3_opr: "or" },
  ];

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const svgEl = (tag, attrs) => {
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };

  // ------------------------------------------------------------------ formatting
  function fmtUsd(units) {        // BigInt, 6 decimals -> "0.03", "1.235"
    const neg = units < 0n;
    let v = neg ? -units : units;
    const whole = v / 1000000n;
    let frac = (v % 1000000n).toString().padStart(6, "0").replace(/0+$/, "");
    if (frac.length < 2) frac = frac.padEnd(2, "0");
    return (neg ? "-" : "") + whole.toLocaleString("en-US") + "." + frac;
  }
  function fmtUsd4(micro) {       // BigInt micro-USD -> "$0.1349" (rounded down to 4 decimals)
    const v = micro < 0n ? 0n : micro;
    return "$" + (v / 1000000n).toLocaleString("en-US") + "." + ((v % 1000000n) / 100n).toString().padStart(4, "0");
  }
  function fmtPrice(answer) {     // BigInt with 8 decimals -> "0.99982693"
    const frac = (answer % 100000000n).toString().padStart(8, "0").replace(/0+$/, "");
    return (answer / 100000000n).toString() + (frac ? "." + frac : "");
  }
  const usdOf = (units, answer) => (units * answer) / 100000000n;   // USDC.e units (6 dec) -> micro-USD
  const short = (h) => h.slice(0, 6) + "…" + h.slice(-4);
  const dayKey = (ms) => new Date(ms).toISOString().slice(0, 10);
  const fmtDay = (key) => new Date(key + "T00:00:00Z").toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  const fmtTime = (ms) => new Date(ms).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" }) + " UTC";
  function addrLink(addr, cls) {
    addr = KNOWN[addr] || addr;
    const a = el("a", "mono" + (cls ? " " + cls : ""), short(addr));
    a.href = EXPLORER + "/address/" + addr;
    a.target = "_blank"; a.rel = "noopener";
    a.title = addr;
    return a;
  }
  function txLink(tx, label) {
    const a = el("a", "mono", label || short(tx));
    a.href = EXPLORER + "/tx/" + tx;
    a.target = "_blank"; a.rel = "noopener";
    a.title = tx;
    return a;
  }

  // ------------------------------------------------------------------ validate + classify
  function cleanItem(it) {
    if (!it || typeof it !== "object") return null;
    const addr = it.address && typeof it.address === "object" ? it.address.hash : it.address;
    if (typeof addr !== "string" || addr.toLowerCase() !== CONTRACT) return null;
    const tx = typeof it.transaction_hash === "string" ? it.transaction_hash.toLowerCase() : "";
    if (!RE_TX.test(tx)) return null;
    if (!Array.isArray(it.topics) || it.topics.length < 1 || it.topics.length > 4) return null;
    const topics = [];
    for (const t of it.topics) {
      if (t === null) continue;
      if (typeof t !== "string" || !RE_WORD.test(t.toLowerCase())) return null;
      topics.push(t.toLowerCase());
    }
    if (!topics.length) return null;
    const data = typeof it.data === "string" ? it.data.toLowerCase() : "";
    if (data.length > 2 + 64 * 16 || !RE_DATA.test(data)) return null;
    const block = Number(it.block_number);
    const index = Number(it.index);
    const ts = Date.parse(it.block_timestamp);
    if (!Number.isSafeInteger(block) || !Number.isSafeInteger(index) || !Number.isFinite(ts)) return null;
    return { tx, topics, data, block, index, ts };
  }

  // getLogs (Etherscan-style) answer row -> the API v2 item shape cleanItem() reads.
  function fromRpcLog(r) {
    if (!r || typeof r !== "object") return null;
    const bn = typeof r.blockNumber === "string" ? r.blockNumber.toLowerCase() : "";
    const li = typeof r.logIndex === "string" ? r.logIndex.toLowerCase() : "";
    const ts = typeof r.timeStamp === "string" ? r.timeStamp.toLowerCase() : "";
    if (!RE_QTY.test(bn) || !RE_QTY.test(li) || !RE_TS.test(ts)) return null;
    return { address: r.address, transaction_hash: r.transactionHash, topics: r.topics, data: r.data,
      block_number: parseInt(bn, 16), index: parseInt(li, 16), block_timestamp: new Date(parseInt(ts, 16) * 1000).toISOString() };
  }

  const word = (data, i) => BigInt("0x" + data.slice(2 + 64 * i, 2 + 64 * (i + 1)));
  const topicAddr = (t) => (t.slice(0, 26) === ADDR_PREFIX ? "0x" + t.slice(26) : null);
  const ID_WORDS = { [pad("1")]: 1, [pad("2")]: 2 };
  const ourId = (t) => ID_WORDS[t] || 0;

  // The kind of a validated log, or null when the page has no use for it (other endpoints, other accounts, other
  // event types). Only these logs are kept.
  function classify(lg) {
    const t = lg.topics, n = t.length, len = lg.data.length;
    switch (t[0]) {
      case T.paid: return n === 4 && len === 2 + 128 && ourId(t[1]) ? "paid" : null;
      case T.refunded: return n === 4 && len === 2 + 64 && ourId(t[1]) ? "refunded" : null;
      case T.withdrawn: return n === 3 && len === 2 + 64 && topicAddr(t[1]) === PAYEE ? "withdrawn" : null;
      case T.paidAuth: return n === 4 && topicAddr(t[3]) === PROVIDER ? "paidAuth" : null;
      case T.priceChanged: return n === 2 && len === 2 + 128 && ourId(t[1]) ? "priceChanged" : null;
      case T.registered: return n === 4 && len >= 2 + 64 && topicAddr(t[3]) === PAYEE ? "registered" : null;
      default: return null;
    }
  }

  // ------------------------------------------------------------------ USD price feed
  // A feed reading {answer, round, updatedAt (s), fromSnapshot} after the checks above, or null.
  function feedOk(answer, round, updatedAt, nowMs) {
    if (typeof answer !== "bigint" || answer < FEED_MIN || answer > FEED_MAX) return false;
    if (!Number.isSafeInteger(updatedAt) || updatedAt <= 0 || updatedAt * 1000 > nowMs + 300000) return false;
    return nowMs - updatedAt * 1000 <= FEED_MAX_AGE_S * 1000 && round > 0n;
  }
  function parseAnswerLog(raw, nowMs) {
    if (!raw || typeof raw !== "object") return null;
    const addr = raw.address && typeof raw.address === "object" ? raw.address.hash : raw.address;
    if (typeof addr !== "string" || addr.toLowerCase() !== FEED.aggregator) return null;
    if (!Array.isArray(raw.topics)) return null;
    const t = raw.topics.filter((x) => x !== null).map((x) => (typeof x === "string" ? x.toLowerCase() : ""));
    const data = typeof raw.data === "string" ? raw.data.toLowerCase() : "";
    if (t.length !== 3 || t[0] !== T_ANSWER || !RE_WORD.test(t[1]) || !RE_WORD.test(t[2]) || !/^0x[0-9a-f]{64}$/.test(data)) return null;
    if (parseInt(t[1].slice(2, 3), 16) >= 8) return null;          // int256 with the sign bit set: negative
    const answer = BigInt(t[1]), round = BigInt(t[2]), up = BigInt(data);
    if (up > 4102444800n) return null;
    const updatedAt = Number(up);
    return feedOk(answer, round, updatedAt, nowMs) ? { answer, round, updatedAt, fromSnapshot: false } : null;
  }
  function newestAnswer(items, nowMs) {
    let best = null;
    for (const raw of items || []) {
      const r = parseAnswerLog(raw, nowMs);
      if (r && (!best || r.updatedAt > best.updatedAt)) best = r;
    }
    return best;
  }
  function parseSnapFeed(f, nowMs) {
    if (!f || typeof f !== "object" || f.pair !== FEED.pair || typeof f.proxy !== "string" ||
        f.proxy.toLowerCase() !== FEED.proxy.toLowerCase() || f.decimals !== 8) return null;
    if (typeof f.answer !== "string" || !/^[0-9]{1,20}$/.test(f.answer) || typeof f.roundId !== "string" || !/^[0-9]{1,30}$/.test(f.roundId)) return null;
    const answer = BigInt(f.answer), round = BigInt(f.roundId);
    return feedOk(answer, round, f.updatedAt, nowMs) ? { answer, round, updatedAt: f.updatedAt, fromSnapshot: true } : null;
  }

  // ------------------------------------------------------------------ fetch
  async function fetchJson(url, timeoutMs) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const r = await fetch(url, { signal: ctl.signal, credentials: "omit", referrerPolicy: "no-referrer", cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const len = Number(r.headers.get("content-length") || 0);
      if (len > 5000000) throw new Error("response too large");
      return await r.json();
    } finally {
      clearTimeout(timer);
    }
  }

  // All logs of one event type newer than block `after`, newest first. complete=false: pages remain after the cap.
  async function fetchV2Type(topic0, after) {
    const items = [];
    let params = null;
    for (let page = 0; page < MAX_V2_PAGES; page++) {
      const q = new URLSearchParams({ topic: topic0 });
      if (params) {
        for (const k in params) {
          const v = params[k];
          if (k !== "topic" && /^[a-z_]{1,40}$/.test(k) && (typeof v === "number" || (typeof v === "string" && /^[0-9a-zA-Z_.:\-]{0,100}$/.test(v)))) q.set(k, String(v));
        }
      }
      const body = await fetchJson(API_V2 + "/addresses/" + CONTRACT + "/logs?" + q.toString(), 20000);
      if (!body || !Array.isArray(body.items)) throw new Error("unexpected API answer");
      let reached = false;
      for (const raw of body.items) {
        const b = Number(raw && raw.block_number);
        if (!Number.isSafeInteger(b)) continue;
        if (b <= after) reached = true;
        else items.push(raw);
      }
      params = body.next_page_params && typeof body.next_page_params === "object" ? body.next_page_params : null;
      if (reached || !params) return { items, complete: true };
    }
    return { items, complete: false };
  }

  function logsResult(body) {
    if (!body || typeof body !== "object" || !Array.isArray(body.result)) throw new Error("unexpected getLogs answer");
    if (body.status === "1") return body.result;
    if (body.status === "0" && body.result.length === 0 && /^No logs found/.test(String(body.message))) return [];
    throw new Error("getLogs error");
  }

  // The exact queries from block `from` to the newest block. complete=false: a query still had full pages.
  async function fetchExact(from) {
    const items = [];
    for (const filter of EXACT_QUERIES) {
      let fromBlock = from, done = false;
      for (let page = 0; page < MAX_LOG_PAGES && !done; page++) {
        const q = new URLSearchParams({ module: "logs", action: "getLogs", address: CONTRACT, fromBlock: String(fromBlock), toBlock: "latest" });
        for (const k in filter) q.set(k, filter[k]);
        const rows = logsResult(await fetchJson(API_LOGS + "?" + q.toString(), 20000));
        let last = -1;
        for (const r of rows) {
          const it = fromRpcLog(r);
          if (!it) continue;
          items.push(it);
          if (it.block_number > last) last = it.block_number;
        }
        if (rows.length < LOG_PAGE) done = true;
        else if (last <= fromBlock) break;        // 1000+ logs in one block: cannot page further
        else fromBlock = last;                    // the next page repeats block `last`; duplicates are dropped
      }
      if (!done) return { items, complete: false };
    }
    return { items, complete: true };
  }

  // The newest block the explorer has indexed, or null.
  async function fetchHead() {
    try {
      const body = await fetchJson(API_V2 + "/main-page/blocks", 15000);
      let h = 0;
      if (Array.isArray(body)) for (const b of body) { const n = Number(b && b.height); if (Number.isSafeInteger(n) && n > h) h = n; }
      return h > DEPLOY_BLOCK ? h : null;
    } catch (e) {
      return null;
    }
  }

  async function fetchSnapshot() {
    const body = await fetchJson("snapshot.json?t=" + Date.now(), 15000);
    if (!body || !Array.isArray(body.items) || body.complete !== true || !Number.isSafeInteger(body.toBlock) ||
        body.toBlock < DEPLOY_BLOCK || !Number.isSafeInteger(body.fromBlock) || body.fromBlock > DEPLOY_BLOCK) throw new Error("bad snapshot");
    const at = Date.parse(body.generatedAt);
    return { items: body.items, toBlock: body.toBlock, at: Number.isFinite(at) ? at : null, usdFeed: body.usdFeed || null };
  }

  // The newest AnswerUpdated log of the feed's aggregator (first page of the API v2, newest first), or null.
  async function fetchFeed() {
    const q = new URLSearchParams({ topic: T_ANSWER });
    const body = await fetchJson(API_V2 + "/addresses/" + FEED.aggregator + "/logs?" + q.toString(), 15000);
    if (!body || !Array.isArray(body.items)) throw new Error("unexpected API answer");
    return newestAnswer(body.items.slice(0, 50), Date.now());
  }

  // ------------------------------------------------------------------ the page's log store
  // items: every counted log seen (key tx:index). cover[type]: every log of that type up to this block is in items.
  function newStore(snap) {
    const s = { items: new Map(), cover: {}, snapshot: !!snap, snapshotAt: snap ? snap.at : null, snapFeed: snap ? snap.usdFeed : null };
    for (const t of LIVE_TYPES) s.cover[t] = snap ? snap.toBlock : DEPLOY_BLOCK - 1;
    if (snap) addItems(s, snap.items);
    return s;
  }
  function addItems(s, raws) {
    for (const raw of raws) {
      if (s.items.size >= MAX_ITEMS) break;
      const it = cleanItem(raw);
      if (it && classify(it)) s.items.set(it.tx + ":" + it.index, raw);
    }
  }
  const FULL = Object.freeze(Object.fromEntries(LIVE_TYPES.map((t) => [t, true])));

  // One refresh. Throws if the API v2 fails (the store is then left as it was). Returns which types are complete.
  async function refreshLive(s) {
    const head = await fetchHead();     // read first: a complete answer after this covers every block up to it
    const res = await Promise.all(LIVE_TYPES.map((t) => fetchV2Type(T[t], s.cover[t])));
    const cov = {}, items = [];
    LIVE_TYPES.forEach((t, i) => { cov[t] = res[i].complete; items.push(...res[i].items); });
    const missing = LIVE_TYPES.filter((t) => !cov[t]);
    if (missing.length) {
      try {
        const ex = await fetchExact(Math.min(...missing.map((t) => s.cover[t])) + 1);
        items.push(...ex.items);
        if (ex.complete) for (const t of missing) cov[t] = true;
      } catch (e) { /* rate limited or unavailable: those types stay partial */ }
    }
    addItems(s, items);
    if (head !== null) for (const t of LIVE_TYPES) if (cov[t]) s.cover[t] = Math.max(s.cover[t], head - LAG_BLOCKS);
    return cov;
  }

  // ------------------------------------------------------------------ aggregate
  function aggregate(rawItems) {
    const seen = new Set();
    const logs = [];
    for (const raw of rawItems.slice(0, MAX_ITEMS)) {
      const it = cleanItem(raw);
      if (!it) continue;
      const kind = classify(it);
      if (!kind) continue;
      const k = it.tx + ":" + it.index;
      if (seen.has(k)) continue;
      seen.add(k);
      it.kind = kind;
      logs.push(it);
    }
    logs.sort((a, b) => a.block - b.block || a.index - b.index);   // oldest first

    const endpoints = {};
    for (const id of OUR_IDS) {
      const p = ENDPOINTS[id];
      endpoints[id] = { id, name: p.name, regTx: p.regTx, provider: PROVIDER, payee: PAYEE, price: p.price, paid: 0, refunded: 0 };
    }
    const payments = [];           // ours, oldest first
    const byRid = new Map();
    const authTx = new Set();      // tx:requestId of EIP-3009 payments the provider submitted
    const others = new Set();      // other endpoint ids that also pay our payee
    const refunds = [];
    let withdrawn = 0n, withdrawals = 0;
    for (const lg of logs) {
      const t = lg.topics;
      try {
        if (lg.kind === "paid") {
          const payer = topicAddr(t[3]);
          if (!payer || "0x" + lg.data.slice(2 + 24, 2 + 64) !== PAYEE || byRid.has(t[2])) continue;
          const p = { id: ourId(t[1]), rid: t[2], payer, amount: word(lg.data, 1), tx: lg.tx, ts: lg.ts, gasless: false, refund: null };
          payments.push(p);
          byRid.set(p.rid, p);
          endpoints[p.id].paid++;
        } else if (lg.kind === "refunded") {
          const r = { id: ourId(t[1]), rid: t[2], amount: word(lg.data, 0), tx: lg.tx, ts: lg.ts };
          refunds.push(r);
          endpoints[r.id].refunded++;
          const p = byRid.get(r.rid);
          if (p) p.refund = r;
        } else if (lg.kind === "withdrawn") {
          withdrawn += word(lg.data, 0);
          withdrawals++;
        } else if (lg.kind === "paidAuth") {
          authTx.add(lg.tx + ":" + t[1]);
        } else if (lg.kind === "priceChanged") {
          endpoints[ourId(t[1])].price = word(lg.data, 1);
        } else if (lg.kind === "registered") {
          const id = BigInt(t[1]);
          if (!ourId(t[1]) && id <= 1000000000n) others.add(Number(id));
        }
      } catch (e) { /* malformed log: skipped */ }
    }
    for (const p of payments) p.gasless = authTx.has(p.tx + ":" + p.rid);
    return { payments, refunds, withdrawn, withdrawals, endpoints, otherPayeeIds: [...others].sort((a, b) => a - b), logCount: logs.length };
  }

  // ------------------------------------------------------------------ render: tiles
  const PARTIAL = "partial data";
  function setTile(id, text, partial) {
    const e = $(id);
    e.textContent = text;
    e.classList.toggle("partial", !!partial);
  }
  function payeeLine(a, cov, net) {
    const o = a.otherPayeeIds;
    if (o.length) return "payee-wide: also endpoint" + (o.length === 1 ? " " : "s ") + o.slice(0, 3).join(", ") + (o.length > 3 ? " +" + (o.length - 3) : "");
    if (!(cov.paid && cov.refunded && cov.registered)) return "payee-wide, all its endpoints";
    return "credit left " + fmtUsd(net - a.withdrawn) + " USDC.e";
  }
  function renderTiles(a, cov) {
    const n = a.payments.length;
    const paid = a.payments.reduce((s, p) => s + p.amount, 0n);
    const refunded = a.refunds.reduce((s, r) => s + r.amount, 0n);
    if (cov.paid && cov.refunded) {
      const gasless = a.payments.filter((p) => p.gasless).length;
      const payers = new Set(a.payments.map((p) => p.payer)).size;
      setTile("k-count", n.toLocaleString("en-US"));
      $("k-count-sub").textContent = (cov.paidAuth ? gasless + " gasless (EIP-3009)" : "gasless count unavailable") + " · " + payers + (payers === 1 ? " payer" : " payers");
      setTile("k-paid", fmtUsd(paid));
      $("k-paid-sub").textContent = "net of refunds " + fmtUsd(paid - refunded);
      setTile("k-refund", n ? (Math.round((a.refunds.length / n) * 1000) / 10).toLocaleString("en-US") + "%" : "–");
      $("k-refund-sub").textContent = a.refunds.length + (a.refunds.length === 1 ? " refund" : " refunds") + " · " + fmtUsd(refunded) + " USDC.e back";
    } else {
      for (const id of ["k-count", "k-paid", "k-refund"]) setTile(id, PARTIAL, true);
      $("k-count-sub").textContent = "more events than this page loads";
      $("k-paid-sub").textContent = "full record on hsk.blockscout.com";
      $("k-refund-sub").textContent = "full record on hsk.blockscout.com";
    }
    if (cov.withdrawn) {
      setTile("k-withdrawn", fmtUsd(a.withdrawn));
      $("k-withdrawn-sub").textContent = payeeLine(a, cov, paid - refunded);
    } else {
      setTile("k-withdrawn", PARTIAL, true);
      $("k-withdrawn-sub").textContent = "payee-wide, all its endpoints";
    }
  }

  // ------------------------------------------------------------------ render: USD value
  function renderUsd(a, cov, feed) {
    const sub = $("usd-sub");
    sub.textContent = "";
    if (!(cov.paid && cov.refunded)) {
      setTile("usd-net", "–");
      sub.textContent = "needs the full payment totals";
      return;
    }
    if (!feed) {
      setTile("usd-net", "–");
      sub.textContent = "APRO " + FEED.pair + " price unavailable right now";
      return;
    }
    const paid = a.payments.reduce((s, p) => s + p.amount, 0n);
    const refunded = a.refunds.reduce((s, r) => s + r.amount, 0n);
    setTile("usd-net", fmtUsd4(usdOf(paid - refunded, feed.answer)));
    const link = el("a", "mono", short(FEED.proxy));
    link.href = EXPLORER + "/address/" + FEED.proxy;
    link.target = "_blank"; link.rel = "noopener"; link.title = FEED.proxy;
    sub.append(fmtUsd4(usdOf(paid, feed.answer)) + " paid, " + fmtUsd4(usdOf(refunded, feed.answer)) + " refunded at APRO " +
      FEED.pair + " " + fmtPrice(feed.answer) + " · round of " + fmtTime(feed.updatedAt * 1000) +
      (feed.fromSnapshot ? " (snapshot)" : "") + " · feed ", link);
  }

  // ------------------------------------------------------------------ render: per-day chart
  function dayRows(a) {
    const m = new Map();
    const get = (k) => { if (!m.has(k)) m.set(k, { day: k, paidN: 0, paid: 0n, refN: 0, ref: 0n }); return m.get(k); };
    for (const p of a.payments) { const d = get(dayKey(p.ts)); d.paidN++; d.paid += p.amount; }
    for (const r of a.refunds) { const d = get(dayKey(r.ts)); d.refN++; d.ref += r.amount; }
    if (!m.size) return [];
    const keys = [...m.keys()].sort();
    const first = Date.parse(keys[0] + "T00:00:00Z");
    const last = Math.max(Date.parse(dayKey(Date.now()) + "T00:00:00Z"), Date.parse(keys[keys.length - 1] + "T00:00:00Z"));
    const rows = [];
    for (let t = first; t <= last; t += 86400000) rows.push(get(dayKey(t)));
    return rows.slice(-DAYS_SHOWN);
  }

  function niceStep(max, count) {
    const raw = max / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const f of [1, 2, 2.5, 5, 10]) if (f * mag >= raw) return f * mag;
    return 10 * mag;
  }
  function decimalsFor(step) {
    let d = 0;
    while (d < 6 && Math.abs(Math.round(step * Math.pow(10, d)) - step * Math.pow(10, d)) > 1e-6) d++;
    return d;
  }
  const toNum = (units) => Number(units) / 1e6;

  function barPath(x, y, w, h, r) {   // rounded top, square baseline
    if (h <= 0) return "";
    r = Math.min(r, w / 2, h);
    return "M" + x + "," + (y + h) + "V" + (y + r) + "Q" + x + "," + y + " " + (x + r) + "," + y +
      "H" + (x + w - r) + "Q" + (x + w) + "," + y + " " + (x + w) + "," + (y + r) + "V" + (y + h) + "Z";
  }

  let lastRows = [];
  function renderChart(rows) {
    lastRows = rows;
    const box = $("chart");
    box.textContent = "";
    const W = Math.max(280, Math.round(box.clientWidth || 600));
    const H = W < 500 ? 220 : 260;
    const m = { l: 44, r: 8, t: 18, b: 26 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img",
      "aria-label": "Column chart of USDC.e paid and refunded per UTC day. The same numbers are in the table below." });
    box.appendChild(svg);
    if (!rows.length) {
      const t = svgEl("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "empty" });
      t.textContent = "No payments yet";
      svg.appendChild(t);
      return;
    }
    const maxV = Math.max(...rows.map((r) => Math.max(toNum(r.paid), toNum(r.ref))), 0.001);
    const step = niceStep(maxV, 4);
    const top = Math.ceil(maxV / step) * step;
    const dec = decimalsFor(step);
    const y = (v) => m.t + ph - (v / top) * ph;
    const ticks = Math.round(top / step);
    for (let k = 0; k <= ticks; k++) {
      const v = k * step;
      const yy = Math.round(y(v)) + 0.5;
      svg.appendChild(svgEl("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy, class: v === 0 ? "base" : "grid" }));
      const lab = svgEl("text", { x: m.l - 6, y: yy + 4, "text-anchor": "end", class: "tick" });
      lab.textContent = v.toFixed(dec);
      svg.appendChild(lab);
    }
    const n = rows.length;
    const band = pw / n;
    const barW = Math.max(3, Math.min(24, (band - 2 - Math.max(4, band * 0.3)) / 2));
    const labelEvery = Math.ceil(n / Math.max(1, Math.floor(pw / 56)));   // label from today backwards
    let maxIdx = 0;
    rows.forEach((r, i) => { if (r.paid > rows[maxIdx].paid) maxIdx = i; });
    rows.forEach((r, i) => {
      const cx = m.l + band * i + band / 2;
      const x1 = cx - barW - 1, x2 = cx + 1;
      const hp = ph - (y(toNum(r.paid)) - m.t), hr = ph - (y(toNum(r.ref)) - m.t);
      const hit = svgEl("rect", { x: m.l + band * i, y: m.t, width: band, height: ph, class: "band", tabindex: 0,
        "aria-label": fmtDay(r.day) + ": " + fmtUsd(r.paid) + " USDC.e paid in " + r.paidN + " requests, " + fmtUsd(r.ref) + " refunded in " + r.refN });
      svg.appendChild(hit);
      if (r.paid > 0n) svg.appendChild(svgEl("path", { d: barPath(x1, y(toNum(r.paid)), barW, Math.max(hp, 1), 4), class: "bar-paid" }));
      if (r.ref > 0n) svg.appendChild(svgEl("path", { d: barPath(x2, y(toNum(r.ref)), barW, Math.max(hr, 1), 4), class: "bar-refund" }));
      if (i === maxIdx && r.paid > 0n) {
        const v = svgEl("text", { x: x1 + barW / 2, y: y(toNum(r.paid)) - 5, "text-anchor": "middle", class: "val" });
        v.textContent = fmtUsd(r.paid);
        svg.appendChild(v);
      }
      if ((n - 1 - i) % labelEvery === 0) {
        const t = svgEl("text", { x: cx, y: H - 8, "text-anchor": "middle", class: "tick" });
        t.textContent = fmtDay(r.day);
        svg.appendChild(t);
      }
      const show = () => { hit.classList.add("on"); showTip(r, cx, box); };
      const hide = () => { hit.classList.remove("on"); hideTip(); };
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("pointermove", show);
      hit.addEventListener("pointerleave", hide);
      hit.addEventListener("focus", show);
      hit.addEventListener("blur", hide);
    });
    // keep the bars above the transparent hit bands visually but let the bands take the pointer
    svg.querySelectorAll(".bar-paid,.bar-refund,.val").forEach((e) => { e.style.pointerEvents = "none"; svg.appendChild(e); });
  }

  function tipRow(color, value, label) {
    const row = el("div", "tt-row");
    const key = el("span", "tt-key");
    key.style.background = color;
    row.append(key, el("span", "tt-val", value), el("span", "tt-lab", label));
    return row;
  }
  function showTip(r, cx, box) {
    const tip = $("tooltip");
    tip.textContent = "";
    const cs = getComputedStyle(document.documentElement);
    tip.append(el("p", "tt-day", fmtDay(r.day) + " (UTC)"),
      tipRow(cs.getPropertyValue("--series-paid").trim(), fmtUsd(r.paid), "paid · " + r.paidN + (r.paidN === 1 ? " request" : " requests")),
      tipRow(cs.getPropertyValue("--series-refund").trim(), fmtUsd(r.ref), "refunded · " + r.refN));
    tip.hidden = false;
    const card = box.parentElement;
    const bx = box.offsetLeft, by = box.offsetTop;
    let left = bx + cx + 12;
    if (left + tip.offsetWidth > card.clientWidth - 8) left = bx + cx - tip.offsetWidth - 12;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = (by + 8) + "px";
  }
  function hideTip() { $("tooltip").hidden = true; }

  function renderDayTable(rows) {
    const tb = $("day-table");
    tb.textContent = "";
    for (const r of rows.slice().reverse()) {
      if (!r.paidN && !r.refN) continue;
      const tr = el("tr");
      tr.append(el("td", "", fmtDay(r.day)), el("td", "num", String(r.paidN)), el("td", "num", fmtUsd(r.paid)),
        el("td", "num", String(r.refN)), el("td", "num", fmtUsd(r.ref)));
      tb.appendChild(tr);
    }
  }

  // ------------------------------------------------------------------ render: latest payments + endpoints
  function renderLatest(a) {
    const ol = $("latest");
    ol.textContent = "";
    const list = a.payments.slice().reverse().slice(0, 25);
    if (!list.length) { const li = el("li"); li.appendChild(el("span", "empty-row", "No payments yet.")); ol.appendChild(li); return; }
    for (const p of list) {
      const li = el("li");
      li.append(el("span", "what", ENDPOINTS[p.id].name), el("span", "amt", fmtUsd(p.amount) + " USDC.e"));
      const d = el("div", "details");
      d.append(el("span", "", fmtTime(p.ts)), el("span", "", "endpoint " + p.id + " · " + (p.gasless ? "gasless x402 (EIP-3009)" : "self-paid pay()")));
      const tx = el("span", "");
      tx.append(document.createTextNode("paid "), txLink(p.tx));
      const st = el("span", "status");
      if (p.refund) {
        st.append(el("span", "ic", "↩"), document.createTextNode("refunded "), txLink(p.refund.tx));
      } else {
        st.append(el("span", "ic", "✓"), document.createTextNode("settled"));
      }
      const payer = el("span", "");
      payer.append(document.createTextNode("payer "), addrLink(p.payer));
      d.append(tx, st, payer);
      li.appendChild(d);
      ol.appendChild(li);
    }
  }

  function renderEndpoints(a, cov) {
    const ul = $("endpoints");
    ul.textContent = "";
    for (const id of OUR_IDS) {
      const e = a.endpoints[id];
      const li = el("li");
      li.append(el("span", "ep-name", "Endpoint " + id + " · " + e.name),
        el("span", "ep-price", cov.priceChanged ? fmtUsd(e.price) + " USDC.e" : "–"));
      const sub = el("span", "ep-sub");
      sub.append(document.createTextNode((cov.paid && cov.refunded ? e.paid + " paid · " + e.refunded + " refunded · " : "") + "provider "),
        addrLink(e.provider), document.createTextNode(" · registered "), txLink(e.regTx));
      li.appendChild(sub);
      ul.appendChild(li);
    }
    const o = a.otherPayeeIds;
    $("excluded").textContent = o.length
      ? "Endpoint" + (o.length === 1 ? " " : "s ") + o.join(", ") + " also pay" + (o.length === 1 ? "s" : "") +
        " this payee. Those payments are not counted above, but the payee's withdrawals and credit include them."
      : "Payments to other endpoints of this contract are not counted.";
  }

  function renderPartialNote(cov) {
    const note = $("partial-note");
    const gaps = LIVE_TYPES.filter((t) => !cov[t]);
    note.hidden = !gaps.length;
    note.textContent = gaps.length
      ? "Partial data: the explorer has more matching events than this page loads, and its exact query is busy. " +
        "Tiles without a total are incomplete, and the chart and list may miss events. The full record is on hsk.blockscout.com."
      : "";
  }

  // ------------------------------------------------------------------ main loop
  let busy = false, lastAgg = null, lastLiveAt = 0, store = null, liveFeed = null, feedAt = 0;
  function setState(text, warn) {
    const s = $("source-state");
    s.textContent = text;
    s.classList.toggle("warn", !!warn);
  }
  function currentFeed() {
    if (liveFeed && feedOk(liveFeed.answer, liveFeed.round, liveFeed.updatedAt, Date.now())) return liveFeed;
    return store ? parseSnapFeed(store.snapFeed, Date.now()) : null;
  }
  async function refreshFeed() {     // never throws; a failure keeps the last reading (it ages out after 26 h)
    if (Date.now() - feedAt < FEED_REFRESH_MS) return;
    try {
      const f = await fetchFeed();
      feedAt = Date.now();
      if (f) liveFeed = f;
    } catch (e) { /* unavailable: currentFeed() falls back to the snapshot's reading */ }
  }
  function renderAll(a, cov) {
    lastAgg = a;
    renderTiles(a, cov);
    renderUsd(a, cov, currentFeed());
    const rows = dayRows(a);
    renderChart(rows);
    renderDayTable(rows);
    renderLatest(a);
    renderEndpoints(a, cov);
    renderPartialNote(cov);
  }
  async function ensureStore() {
    if (store && (store.snapshot || lastLiveAt)) return store;
    let snap = null;
    try { snap = await fetchSnapshot(); } catch (e) { snap = null; }
    store = newStore(snap);
    return store;
  }
  async function load() {
    if (busy) return;
    busy = true;
    document.body.classList.add("busy");
    $("refresh").disabled = true;
    let s = null;
    const feedP = refreshFeed();
    try {
      s = await ensureStore();
      const cov = await refreshLive(s);
      await feedP;
      renderAll(aggregate([...s.items.values()]), cov);
      lastLiveAt = Date.now();
      const partial = LIVE_TYPES.some((t) => !cov[t]);
      const now = new Date(lastLiveAt).toISOString().slice(11, 19);
      setState(partial ? "Partial data · hsk.blockscout.com · updated " + now + " UTC" : "Live · hsk.blockscout.com · updated " + now + " UTC", partial);
    } catch (e) {
      await feedP;
      if (lastLiveAt) {
        setState("Refresh failed; showing data from " + fmtTime(lastLiveAt), true);
      } else if (s && s.snapshot) {
        renderAll(aggregate([...s.items.values()]), FULL);
        setState("Explorer API unreachable: showing a snapshot from " + (s.snapshotAt ? fmtTime(s.snapshotAt) : "an earlier build"), true);
      } else {
        setState("Could not load data. Open the contract on hsk.blockscout.com instead.", true);
      }
    } finally {
      busy = false;
      document.body.classList.remove("busy");
      $("refresh").disabled = false;
    }
  }

  if (typeof window === "undefined") {          // loaded by tools/test_dashboard.js (Node): export the pure parts only
    module.exports = { aggregate, classify, cleanItem, fromRpcLog, dayRows, fmtUsd, decimalsFor, niceStep, T, PROVIDER, PAYEE,
      CONTRACT, DEPLOY_BLOCK, ENDPOINTS, EXACT_QUERIES, LIVE_TYPES, MAX_V2_PAGES, FEED, T_ANSWER, parseAnswerLog, newestAnswer,
      parseSnapFeed, usdOf, fmtUsd4, fmtPrice };
    return;
  }
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (lastAgg) { hideTip(); renderChart(lastRows); } }, 150);
  });
  document.addEventListener("DOMContentLoaded", () => {
    $("refresh").addEventListener("click", load);
    load();
    setInterval(() => { if (document.visibilityState === "visible") load(); }, REFRESH_MS);
  });
})();
