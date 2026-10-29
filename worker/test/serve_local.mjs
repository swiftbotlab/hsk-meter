/* Runs the Worker's request handler (src/app.js, unchanged) as a loopback HTTP server, for the cross-language test
   worker/test/test_buyer_compat.py. Test use only:
     node worker/test/serve_local.mjs http://127.0.0.1:<rpc port>
   The RPC must be a loopback URL (the test's offline chain); every other outbound fetch is refused. The served-set
   is the in-memory Durable Object double. Prints {"port": N} once listening. */
import http from 'node:http';
import { randomBytes } from 'node:crypto';
import { handle } from '../src/app.js';
import { memoryNamespace, doLedger } from '../src/ledger_core.js';

const rpcUrl = process.argv[2] || '';
if (!/^http:\/\/127\.0\.0\.1:\d{2,5}$/.test(rpcUrl)) {
  console.error('usage: serve_local.mjs http://127.0.0.1:<port>');
  process.exit(2);
}
const loopbackFetch = (u, o) => {
  if (new URL(u).hostname !== '127.0.0.1') return Promise.reject(new Error('serve_local: non-loopback fetch refused'));
  return fetch(u, o);
};
const env = { PAYWALL_TOKEN_KEY: randomBytes(32).toString('hex') };
const deps = { rpcUrl, fetchImpl: loopbackFetch, ledger: doLedger(memoryNamespace()), endpointCache: new Map(), log: () => {}, ip: '127.0.0.1' };

const srv = http.createServer(async (req, res) => {
  try {
    const headers = new Headers();
    for (const [k, v] of Object.entries(req.headers)) if (typeof v === 'string') headers.set(k, v);
    const r = await handle(new Request('http://127.0.0.1:' + srv.address().port + req.url, { method: req.method, headers }), env, deps);
    const body = Buffer.from(await r.arrayBuffer());
    res.writeHead(r.status, Object.fromEntries(r.headers));
    res.end(body);
  } catch (e) {
    res.writeHead(500, { 'content-type': 'application/json' });
    res.end('{"ok":false,"error":"internal"}');
  }
});
srv.listen(0, '127.0.0.1', () => console.log(JSON.stringify({ port: srv.address().port })));
