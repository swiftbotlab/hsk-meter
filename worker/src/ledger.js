/* Durable Object class of the HSK Meter served-set (Swift Bot Labs); the state machine is in ledger_core.js. */
import { DurableObject } from 'cloudflare:workers';
import { ledgerCore } from './ledger_core.js';

export class Ledger extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    const store = { get: (k) => ctx.storage.get(k), put: (k, v) => ctx.storage.put(k, v) };
    this.core = ledgerCore(store, (fn) => ctx.blockConcurrencyWhile(fn));
  }
  getRecord(rid) { return this.core.getRecord(rid); }
  claim(rid, info, now) { return this.core.claim(rid, info, now); }
  commit(rid, claimId, now) { return this.core.commit(rid, claimId, now); }
  committedBy(rid, claimId) { return this.core.committedBy(rid, claimId); }
  expire(rid, x, now) { return this.core.expire(rid, x, now); }
  release(rid, claimId) { return this.core.release(rid, claimId); }
  markUndeliverable(rid, claimId, x, now) { return this.core.markUndeliverable(rid, claimId, x, now); }
  enqueue(item) { return this.core.enqueue(item); }
  list() { return this.core.list(); }
}
