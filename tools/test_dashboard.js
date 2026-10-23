// Tests for the dashboard's log decoder and counters (docs/app.js). Offline by default:
//   node tools/test_dashboard.js [snapshot.json] [--expect-paid N --expect-refunded N --expect-withdrawn N]
// With --expect-* (USDC.e base units, read from the contract's totalPaid/totalRefunded/totalWithdrawn), the totals the
// page computes from the snapshot must equal the contract's own counters (valid while only endpoints 1-2 exist).
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const A = require(path.join(__dirname, "..", "docs", "app.js"));

const args = process.argv.slice(2);
const opt = (k) => { const i = args.indexOf(k); return i >= 0 ? BigInt(args[i + 1]) : null; };
const snapPath = args[0] && !args[0].startsWith("--") ? args[0] : path.join(__dirname, "..", "docs", "snapshot.json");
const snap = JSON.parse(fs.readFileSync(snapPath, "utf8"));
let passed = 0;
const test = (name, fn) => { fn(); passed++; console.log("ok   " + name); };

const pad = (hex) => "0x" + hex.replace(/^0x/, "").padStart(64, "0");
const C = A.CONTRACT;
const mk = (topics, data, i, tx) => ({ address: { hash: C }, transaction_hash: tx || pad((0xabc000 + i).toString(16)), topics, data: data || "0x",
  block_number: 100 + i, index: 0, block_timestamp: "2026-10-23T10:00:00Z" });
const reg = (id, provider, i) => mk([A.T.registered, pad(id.toString(16)), pad(provider), pad(A.PAYEE)],
  "0x" + pad("2710").slice(2) + pad("40").slice(2) + pad("0").slice(2), i);
const paid = (id, amount, i) => mk([A.T.paid, pad(id.toString(16)), pad("aa" + i), pad("5270fe01fd3d6148b687da0b5693b0425cfca361")],
  "0x" + pad(A.PAYEE).slice(2) + pad(amount.toString(16)).slice(2), i);

test("snapshot decodes", () => {
  const a = A.aggregate(snap.items);
  assert.ok(a.verified, "endpoints 1 and 2 must be registered by our provider");
  assert.ok(a.payments.length > 0);
  console.log("     payments " + a.payments.length + ", refunds " + a.refunds.length + ", withdrawn " + a.withdrawn +
    ", gasless " + a.payments.filter((p) => p.gasless).length + ", excluded " + (a.excludedPaid + a.excludedOther));
});

test("snapshot totals match the contract's counters", () => {
  const a = A.aggregate(snap.items);
  const paidT = a.payments.reduce((s, p) => s + p.amount, 0n);
  const refT = a.refunds.reduce((s, r) => s + r.amount, 0n);
  const ep = opt("--expect-paid"), er = opt("--expect-refunded"), ew = opt("--expect-withdrawn");
  if (ep === null) { console.log("     (no --expect-* given: skipped)"); return; }
  assert.strictEqual(paidT, ep, "totalPaid");
  assert.strictEqual(refT, er, "totalRefunded");
  assert.strictEqual(a.withdrawn, ew, "totalWithdrawn");
  for (const r of a.refunds) assert.ok(a.payments.some((p) => p.refund === r), "every refund is matched to its payment");
});

test("only endpoints 1 and 2 of our provider count", () => {
  const stranger = "1111111111111111111111111111111111111111";
  const items = [reg(1, A.PROVIDER, 1), reg(2, A.PROVIDER, 2), reg(3, stranger, 3),
    paid(1, 30000, 4), paid(3, 999999, 5), paid(2, 25000, 6)];
  const a = A.aggregate(items);
  assert.strictEqual(a.payments.length, 2);
  assert.strictEqual(a.excludedPaid, 1);
});

test("an endpoint 1 registered by someone else is not counted and flags the page", () => {
  const stranger = "2222222222222222222222222222222222222222";
  const a = A.aggregate([reg(1, stranger, 1), reg(2, A.PROVIDER, 2), paid(1, 30000, 3), paid(2, 25000, 4)]);
  assert.strictEqual(a.verified, false);
  assert.strictEqual(a.payments.length, 1);
});

test("logs from another address, malformed hex and junk are dropped", () => {
  const good = [reg(1, A.PROVIDER, 1), reg(2, A.PROVIDER, 2)];
  const other = { ...paid(1, 30000, 7), address: { hash: "0x" + "33".repeat(20) } };
  const badTopic = { ...paid(1, 30000, 8), topics: [A.T.paid, "0x<script>", pad("1"), pad("2")] };
  const badData = { ...paid(1, 30000, 9), data: "0xzz" };
  const badTx = { ...paid(1, 30000, 10), transaction_hash: "javascript:alert(1)" };
  const hugeData = { ...paid(1, 30000, 11), data: "0x" + "00".repeat(32 * 40) };
  const a = A.aggregate([...good, other, badTopic, badData, badTx, hugeData, null, 42, "x", {}]);
  assert.strictEqual(a.payments.length, 0);
});

test("duplicate log entries (overlapping pages) count once", () => {
  const p = paid(1, 30000, 4);
  const a = A.aggregate([reg(1, A.PROVIDER, 1), reg(2, A.PROVIDER, 2), p, p, { ...p }]);
  assert.strictEqual(a.payments.length, 1);
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

console.log(passed + " passed");
