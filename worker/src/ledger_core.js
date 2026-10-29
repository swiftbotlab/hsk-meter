/* The served-set of the HSK Meter Worker (Swift Bot Labs): one Durable Object instance per paid requestId, so every
   state change of one payment is serialized (strongly consistent, unlike Workers KV), plus one instance named
   "refund-queue" that lists paid requests that could not be served (the provider refunds them on-chain).

   States of a requestId: (none) -> serving (claimed, with a lease) -> served | undeliverable;
   serving -> released (the chain data could not be read; it can be claimed again). A claim whose lease expired
   (the Worker died mid-request) can be claimed again. Data is returned to a buyer only after commit() said yes. */

export const LEASE_MS = 60000;
const QUEUE_CAP = 2000;   // when full, new entries are refused (never evicted silently); the per-request record stays

function randomId() {
  const b = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}
function publicView(r) {
  if (!r) return null;
  const { claimId, servedBy, ...rest } = r;     // never hand a claim id to another request
  void claimId; void servedBy;
  return rest;
}

/* the state machine over an async key-value store and a mutex: shared by the Durable Object and the test double */
export function ledgerCore(store, locked) {
  return {
    async getRecord(rid) { return publicView(await store.get(rid)); },
    claim(rid, info, now) {
      return locked(async () => {
        const r = await store.get(rid);
        if (r && (r.state === 'served' || r.state === 'undeliverable')) return { ok: false, ...publicView(r) };
        if (r && r.state === 'serving' && now - r.at < LEASE_MS) return { ok: false, ...publicView(r) };
        const claimId = randomId();
        const attempts = ((r && r.attempts) || 0) + 1;
        const firstAt = (r && r.firstAt) || now;
        await store.put(rid, { ...(info || {}), state: 'serving', at: now, claimId, attempts, firstAt });
        return { ok: true, claimId, attempts, firstAt };
      });
    },
    commit(rid, claimId, now) {
      return locked(async () => {
        const r = await store.get(rid);
        if (!r || r.state !== 'serving' || r.claimId !== claimId) return false;
        await store.put(rid, { ...r, state: 'served', at: now, claimId: null, servedBy: claimId });
        return true;
      });
    },
    /* after a commit call that errored: did that commit land? */
    async committedBy(rid, claimId) {
      const r = await store.get(rid);
      return !!(r && r.state === 'served' && claimId && r.servedBy === claimId);
    },
    /* a paid request not served within the window: undeliverable (unless it was served, or is being served now) */
    expire(rid, x, now) {
      return locked(async () => {
        const r = await store.get(rid);
        if (r && (r.state === 'served' || r.state === 'undeliverable')) return r.state === 'undeliverable';
        if (r && r.state === 'serving' && now - r.at < LEASE_MS) return false;
        await store.put(rid, { ...(r || {}), state: 'undeliverable', at: now, claimId: null,
          reason: String((x && x.reason) || '').slice(0, 160), payment: x && x.payment });
        return true;
      });
    },
    release(rid, claimId) {
      return locked(async () => {
        const r = await store.get(rid);
        if (!r || r.state !== 'serving' || r.claimId !== claimId) return false;
        await store.put(rid, { ...r, state: 'released', claimId: null });
        return true;
      });
    },
    markUndeliverable(rid, claimId, x, now) {
      return locked(async () => {
        const r = await store.get(rid);
        if (!r || r.state !== 'serving' || r.claimId !== claimId) return false;
        await store.put(rid, { ...r, state: 'undeliverable', at: now, claimId: null,
          reason: String((x && x.reason) || '').slice(0, 160), payment: x && x.payment });
        return true;
      });
    },
    enqueue(item) {
      return locked(async () => {
        const q = (await store.get('q')) || [];
        if (q.some((e) => e.requestId === item.requestId)) return true;
        if (q.length >= QUEUE_CAP) return false;
        q.push(item);
        await store.put('q', q);
        return true;
      });
    },
    async list() { return (await store.get('q')) || []; }
  };
}

/* the Worker's view of the ledger (ids derived from the requestId; the queue is one named instance) */
export function doLedger(ns) {
  const one = (rid) => ns.get(ns.idFromName('rid:' + rid));
  const queue = () => ns.get(ns.idFromName('refund-queue'));
  const enqueue = (rid, x, at) => {
    const p = (x && x.payment) || {};
    return queue().enqueue({ requestId: rid, payer: p.payer, endpointId: p.endpointId, amount: p.amount, tx: p.tx,
      reason: String((x && x.reason) || '').slice(0, 160), queuedAt: new Date(at).toISOString() });
  };
  return {
    get: (rid) => one(rid).getRecord(rid),
    claim: (rid, info, now) => one(rid).claim(rid, info, now),
    commit: (rid, claimId, now) => one(rid).commit(rid, claimId, now),
    committedBy: (rid, claimId) => one(rid).committedBy(rid, claimId),
    release: (rid, claimId) => one(rid).release(rid, claimId),
    async markUndeliverable(rid, claimId, x, now) {
      const ok = await one(rid).markUndeliverable(rid, claimId, x, now);
      if (ok) await enqueue(rid, x, now);
      return ok;
    },
    async expire(rid, x, now) {
      const ok = await one(rid).expire(rid, x, now);
      if (ok) await enqueue(rid, x, now);
      return ok;
    },
    /* an undeliverable record read again: make sure it is listed (enqueue ignores duplicates) */
    ensureQueued: (rid, rec) => enqueue(rid, { payment: rec.payment, reason: rec.reason }, rec.at || Date.now()),
    listRefunds: () => queue().list()
  };
}

/* an in-memory Durable Object namespace with the same semantics (one serialized instance per name): tests and the
   local compatibility server use it */
export function memoryNamespace() {
  const objects = new Map();
  return {
    idFromName: (name) => name,
    get(name) {
      if (!objects.has(name)) {
        const m = new Map();
        let chain = Promise.resolve();
        const locked = (fn) => { const p = chain.then(fn); chain = p.catch(() => {}); return p; };
        const store = { get: async (k) => structuredClone(m.get(k)), put: async (k, v) => { m.set(k, structuredClone(v)); } };
        objects.set(name, ledgerCore(store, locked));
      }
      return objects.get(name);
    },
    objects
  };
}
