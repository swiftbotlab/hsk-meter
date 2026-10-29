/* HSK Meter public endpoint on Cloudflare Workers (Swift Bot Labs): entry point. Keyless: the Worker holds no key
   that can move funds; see app.js (routes), core.js (request logic) and ledger.js (served-set). */
import { handle } from './app.js';
export { Ledger } from './ledger.js';

export default {
  fetch(req, env) { return handle(req, env); }
};
