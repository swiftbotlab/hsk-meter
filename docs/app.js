// HSK Meter live dashboard (Swift Bot Labs). Reads the settlement contract's event logs from the public Blockscout
// API v2 in the browser, decodes them from raw topics/data (no reliance on the explorer's optional decoding) and
// counts only endpoints 1 and 2 of our provider. Every value from the API is validated and inserted with
// textContent; nothing from the network is ever parsed as HTML.
"use strict";
(function () {
  const API = "https://hsk.blockscout.com/api/v2";
  const EXPLORER = "https://hsk.blockscout.com";
  const CONTRACT = "0x78c8476772bc209c3df09da5e129d0879cd57f8c";
  const PROVIDER = "0x30ccc6d816854d1a66fbfece15b1515a3e941160";
  const PAYEE = "0xa8a49a6ca872d54a8fd5fb121139de9bff62a48d";
  const OUR_ENDPOINTS = { 1: "Address profile", 2: "Transaction check" };
  const KNOWN = {                  // checksummed display form of the demo roles (topics carry lowercase addresses)
    "0x30ccc6d816854d1a66fbfece15b1515a3e941160": "0x30CCc6d816854d1a66fBfecE15b1515a3E941160",
    "0xa8a49a6ca872d54a8fd5fb121139de9bff62a48d": "0xA8A49A6Ca872D54a8FD5FB121139De9bff62a48D",
    "0x5270fe01fd3d6148b687da0b5693b0425cfca361": "0x5270fE01FD3D6148B687dA0b5693B0425CfCA361",
  };
  const MAX_PAGES = 40;            // 50 logs per page
  const MAX_ITEMS = 5000;
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
  const RE_WORD = /^0x[0-9a-f]{64}$/;
  const RE_DATA = /^0x(?:[0-9a-f]{64})*$/;
  const RE_TX = /^0x[0-9a-f]{64}$/;

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

  // ------------------------------------------------------------------ fetch + validate
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
    const data = typeof it.data === "string" ? it.data.toLowerCase() : "";
    if (data.length > 2 + 64 * 16 || !RE_DATA.test(data)) return null;
    const block = Number(it.block_number);
    const index = Number(it.index);
    const ts = Date.parse(it.block_timestamp);
    if (!Number.isSafeInteger(block) || !Number.isSafeInteger(index) || !Number.isFinite(ts)) return null;
    return { tx, topics, data, block, index, ts };
  }

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

  async function fetchLive() {
    const items = [];
    let params = null;
    for (let page = 0; page < MAX_PAGES; page++) {
      let url = API + "/addresses/" + CONTRACT + "/logs";
      if (params) {
        const q = new URLSearchParams();
        for (const k in params) {
          const v = params[k];
          if (/^[a-z_]{1,40}$/.test(k) && (typeof v === "number" || (typeof v === "string" && /^[0-9a-zA-Z_.:\-]{0,100}$/.test(v)))) q.set(k, String(v));
        }
        url += "?" + q.toString();
      }
      const body = await fetchJson(url, 20000);
      if (!body || !Array.isArray(body.items)) throw new Error("unexpected API answer");
      items.push(...body.items);
      if (items.length >= MAX_ITEMS) break;
      params = body.next_page_params && typeof body.next_page_params === "object" ? body.next_page_params : null;
      if (!params) break;
    }
    return items;
  }

  async function fetchSnapshot() {
    const body = await fetchJson("snapshot.json?t=" + Date.now(), 15000);
    if (!body || !Array.isArray(body.items)) throw new Error("bad snapshot");
    const at = Date.parse(body.generatedAt);
    return { items: body.items, at: Number.isFinite(at) ? at : null };
  }

  // ------------------------------------------------------------------ decode + aggregate
  const word = (data, i) => BigInt("0x" + data.slice(2 + 64 * i, 2 + 64 * (i + 1)));
  const topicAddr = (t) => "0x" + t.slice(26);
  const topicInt = (t) => BigInt(t);

  function aggregate(rawItems) {
    const seen = new Set();
    const logs = [];
    for (const raw of rawItems.slice(0, MAX_ITEMS)) {
      const it = cleanItem(raw);
      if (!it) continue;
      const k = it.tx + ":" + it.index;
      if (seen.has(k)) continue;
      seen.add(k);
      logs.push(it);
    }
    logs.sort((a, b) => a.block - b.block || a.index - b.index);   // oldest first

    const endpoints = {};          // id -> {provider, payee, price, paid, ...}
    const payments = [];           // ours, oldest first
    const byRid = new Map();
    const authTx = new Set();      // tx:requestId of EIP-3009 payments
    let refunds = [], withdrawn = 0n, withdrawals = 0, excludedPaid = 0, excludedOther = 0;

    for (const lg of logs) {
      const t0 = lg.topics[0];
      try {
        if (t0 === T.registered && lg.topics.length === 4) {
          const id = Number(topicInt(lg.topics[1]));
          endpoints[id] = { id, provider: topicAddr(lg.topics[2]), payee: topicAddr(lg.topics[3]), price: word(lg.data, 0), paid: 0, refunded: 0 };
        } else if (t0 === T.priceChanged && lg.topics.length === 2) {
          const id = Number(topicInt(lg.topics[1]));
          if (endpoints[id]) endpoints[id].price = word(lg.data, 1);
        } else if (t0 === T.paidAuth && lg.topics.length === 4) {
          authTx.add(lg.tx + ":" + lg.topics[1]);
        }
      } catch (e) { /* malformed log: skipped */ }
    }
    const ours = (id) => OUR_ENDPOINTS[id] !== undefined && endpoints[id] && endpoints[id].provider === PROVIDER;
    const verified = Object.keys(OUR_ENDPOINTS).every((id) => endpoints[id] && endpoints[id].provider === PROVIDER);

    for (const lg of logs) {
      const t0 = lg.topics[0];
      try {
        if (t0 === T.paid && lg.topics.length === 4 && lg.data.length === 2 + 128) {
          const id = Number(topicInt(lg.topics[1]));
          if (!ours(id)) { excludedPaid++; continue; }
          const p = {
            id, rid: lg.topics[2], payer: topicAddr(lg.topics[3]), payee: "0x" + lg.data.slice(2 + 24, 2 + 64),
            amount: word(lg.data, 1), tx: lg.tx, ts: lg.ts, gasless: false, refund: null,
          };
          payments.push(p);
          byRid.set(p.rid, p);
          endpoints[id].paid++;
        } else if (t0 === T.refunded && lg.topics.length === 4) {
          const id = Number(topicInt(lg.topics[1]));
          if (!ours(id)) { excludedOther++; continue; }
          const r = { id, rid: lg.topics[2], amount: word(lg.data, 0), tx: lg.tx, ts: lg.ts };
          refunds.push(r);
          endpoints[id].refunded++;
          const p = byRid.get(r.rid);
          if (p) p.refund = r;
        } else if (t0 === T.withdrawn && lg.topics.length === 3) {
          if (topicAddr(lg.topics[1]) === PAYEE) { withdrawn += word(lg.data, 0); withdrawals++; }
          else excludedOther++;
        }
      } catch (e) { /* malformed log: skipped */ }
    }
    for (const p of payments) p.gasless = authTx.has(p.tx + ":" + p.rid);
    return { payments, refunds, withdrawn, withdrawals, endpoints, verified, excludedPaid, excludedOther, logCount: logs.length };
  }

  // ------------------------------------------------------------------ render: tiles
  function renderTiles(a) {
    const n = a.payments.length;
    const paid = a.payments.reduce((s, p) => s + p.amount, 0n);
    const refunded = a.refunds.reduce((s, r) => s + r.amount, 0n);
    const gasless = a.payments.filter((p) => p.gasless).length;
    const payers = new Set(a.payments.map((p) => p.payer)).size;
    $("k-count").textContent = n.toLocaleString("en-US");
    $("k-count-sub").textContent = gasless + " gasless (EIP-3009) · " + payers + (payers === 1 ? " payer" : " payers");
    $("k-paid").textContent = fmtUsd(paid);
    $("k-paid-sub").textContent = "net of refunds " + fmtUsd(paid - refunded);
    $("k-refund").textContent = n ? (Math.round((a.refunds.length / n) * 1000) / 10).toLocaleString("en-US") + "%" : "–";
    $("k-refund-sub").textContent = a.refunds.length + (a.refunds.length === 1 ? " refund" : " refunds") + " · " + fmtUsd(refunded) + " USDC.e back";
    $("k-withdrawn").textContent = fmtUsd(a.withdrawn);
    $("k-withdrawn-sub").textContent = "credit left " + fmtUsd(paid - refunded - a.withdrawn) + " USDC.e";
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
      li.append(el("span", "what", OUR_ENDPOINTS[p.id]), el("span", "amt", fmtUsd(p.amount) + " USDC.e"));
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

  function renderEndpoints(a) {
    const ul = $("endpoints");
    ul.textContent = "";
    for (const id of Object.keys(OUR_ENDPOINTS)) {
      const e = a.endpoints[id];
      const li = el("li");
      li.append(el("span", "ep-name", "Endpoint " + id + " · " + OUR_ENDPOINTS[id]),
        el("span", "ep-price", e ? fmtUsd(e.price) + " USDC.e" : "–"));
      const sub = el("span", "ep-sub");
      if (e && e.provider === PROVIDER) {
        sub.append(document.createTextNode(e.paid + " paid · " + e.refunded + " refunded · provider "), addrLink(e.provider));
      } else {
        sub.textContent = e ? "Registered by another provider: not counted" : "Registration not found in the loaded logs";
      }
      li.appendChild(sub);
      ul.appendChild(li);
    }
    const ex = a.excludedPaid + a.excludedOther;
    $("excluded").textContent = ex
      ? ex + " event" + (ex === 1 ? "" : "s") + " from other endpoints or accounts on this contract " + (ex === 1 ? "is" : "are") + " excluded."
      : "No activity from other endpoints on this contract so far.";
  }

  // ------------------------------------------------------------------ main loop
  let busy = false, lastAgg = null;
  function setState(text, warn) {
    const s = $("source-state");
    s.textContent = text;
    s.classList.toggle("warn", !!warn);
  }
  function renderAll(a) {
    lastAgg = a;
    renderTiles(a);
    const rows = dayRows(a);
    renderChart(rows);
    renderDayTable(rows);
    renderLatest(a);
    renderEndpoints(a);
  }
  async function load() {
    if (busy) return;
    busy = true;
    document.body.classList.add("busy");
    $("refresh").disabled = true;
    try {
      const items = await fetchLive();
      const a = aggregate(items);
      renderAll(a);
      const now = new Date().toISOString().slice(11, 19);
      setState(a.verified ? "Live · hsk.blockscout.com · updated " + now + " UTC" : "Live, but endpoint ownership could not be verified", !a.verified);
    } catch (e) {
      try {
        const snap = await fetchSnapshot();
        renderAll(aggregate(snap.items));
        setState("Explorer API unreachable: showing a snapshot from " + (snap.at ? fmtTime(snap.at) : "an earlier build"), true);
      } catch (e2) {
        if (!lastAgg) setState("Could not load data. Open the contract on hsk.blockscout.com instead.", true);
        else setState("Refresh failed; showing the last loaded data", true);
      }
    } finally {
      busy = false;
      document.body.classList.remove("busy");
      $("refresh").disabled = false;
    }
  }

  if (typeof window === "undefined") {          // loaded by tools/test_dashboard.js (Node): export the pure parts only
    module.exports = { aggregate, dayRows, fmtUsd, cleanItem, decimalsFor, niceStep, T, PROVIDER, PAYEE, CONTRACT };
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
