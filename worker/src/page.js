/* The human-readable home page of the HSK Meter public endpoint (Swift Bot Labs). Static HTML, no scripts. */
import { CONTRACT, USDCE, PROVIDER, PAYEE, EXPLORER, REPO, BUIDL, DASHBOARD, RESOURCES, VERSION } from './core.js';

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const usdc = (units) => (units === null || units === undefined || !/^\d+$/.test(String(units)) ? 'unavailable'
  : (Number(units) / 1e6).toFixed(Number(units) % 10000 ? 3 : 2) + ' USDC.e');

export function homePage(eps, origin) {
  const o = esc(origin);
  const sample = '0x78C8476772BC209C3df09Da5e129d0879cd57F8c';
  const p1 = eps && eps['/v1/hsk/address-profile'] && /^\d+$/.test(String(eps['/v1/hsk/address-profile'].price))
    ? String(eps['/v1/hsk/address-profile'].price) : '&lt;price&gt;';
  const rows = Object.entries(RESOURCES).map(([p, s]) => {
    const e = (eps && eps[p]) || {};
    return `<tr><td><code>${esc(p)}</code></td><td>${esc(s.endpointId)}</td><td class="nw">${esc(usdc(e.price))}</td><td>${esc(s.desc)}</td></tr>`;
  }).join('');
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>HSK Meter</title>
<meta name="description" content="HashKey Chain data APIs paid per request in USDC.e, verified on-chain. By Swift Bot Labs.">
<style>
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#5c5b57;--line:#e3e0d8;--card:#fff;--accent:#0b6e4f;--code:#f2efe8}
@media (prefers-color-scheme: dark){:root{--bg:#111312;--fg:#ecebe7;--muted:#a6a49e;--line:#2c2f2d;--card:#181b1a;--accent:#5fd3a6;--code:#1f2321}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px 56px}h1{font-size:30px;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:19px;margin:34px 0 10px}p{margin:8px 0}.lead{color:var(--muted);font-size:17px}
a{color:var(--accent)}code,pre{font:13.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
code{background:var(--code);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}
pre{background:var(--code);padding:12px 14px;border-radius:8px;overflow-x:auto;white-space:pre;margin:8px 0}
pre code{background:none;padding:0}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.tbl{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14.5px}td,th{border-bottom:1px solid var(--line);padding:8px 6px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}ol{padding-left:22px}li{margin:6px 0}.tag{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:1px 10px;font-size:13px;color:var(--muted);margin-right:6px}
footer{margin-top:40px;color:var(--muted);font-size:14px}.nw{white-space:nowrap}
@media (max-width:560px){td:nth-child(2),th:nth-child(2){display:none}}
</style></head><body><main>
<p><span class="tag">HashKey Chain mainnet · chain 177</span><span class="tag">x402 v2</span><span class="tag">USDC.e</span></p>
<h1>HSK Meter</h1>
<p class="lead">HashKey Chain data APIs, paid per request in USDC.e and settled on-chain per request through the
<a href="${EXPLORER}/address/${CONTRACT}?tab=contract">HskPayPerRequest</a> contract. This public endpoint is always on and
<strong>holds no private key</strong>: you pay the contract yourself, and the endpoint checks that payment on-chain before it answers.</p>

<h2>Prices (read live from the contract)</h2>
<div class="tbl"><table><thead><tr><th>Request</th><th>Endpoint</th><th>Price</th><th>What you get</th></tr></thead><tbody>${rows}</tbody></table></div>

<h2>Pay for one request</h2>
<ol>
<li>Ask for the price. The <code>402</code> answer carries a <code>requestId</code> bound to your address and to this exact request, and a quote token:
<pre><code>curl -i "${o}/v1/hsk/address-profile?address=${sample}" -H "X-Payer: 0xYOUR_ADDRESS"</code></pre></li>
<li>Pay the contract from that address (the amount is the price in USDC.e base units, 6 decimals):
<pre><code>cast send ${USDCE} "approve(address,uint256)" ${CONTRACT} ${p1} --rpc-url https://mainnet.hsk.xyz --account you
cast send ${CONTRACT} "pay(uint256,bytes32,uint256)" 1 &lt;requestId&gt; ${p1} --rpc-url https://mainnet.hsk.xyz --account you</code></pre></li>
<li>Ask again with the proof headers: the endpoint reads <code>getPayment(requestId)</code> and the <code>Paid</code> event, then answers <code>200</code> with the data:
<pre><code>curl "${o}/v1/hsk/address-profile?address=${sample}" -H "X-Payer: 0xYOUR_ADDRESS" \\
  -H "X-Paywall-Token: &lt;token&gt;" -H "X-Paywall-Tx: &lt;pay tx hash&gt;"</code></pre></li>
</ol>
<p>Or let the buyer client from the repository do all three steps, with its spend guard (pinned provider and payee, price cap, session cap):</p>
<pre><code>python3 agent/buyer.py "${o}/v1/hsk/address-profile?address=${sample}" --scheme tx \\
  --key-file buyer.env --expect-provider ${PROVIDER} --expect-payee ${PAYEE}</code></pre>

<h2>Check any payment, free</h2>
<pre><code>curl "${o}/v1/receipt/&lt;requestId or tx hash&gt;"</code></pre>
<p>Decodes the <code>Paid</code> and <code>Refunded</code> events of the settlement contract, cross-checks <code>getPayment</code>, and answers
<strong>ACCEPT</strong> or <strong>REJECT</strong> with every check and its reason.</p>

<h2>What “keyless” means here</h2>
<div class="card"><ul>
<li>No settlement key: signed EIP-3009 payments (x402 scheme <code>exact</code>, where the seller pays the gas) need the full seller from the repository. This endpoint offers the buyer-paid scheme <code>hsk-paywall-tx</code> only.</li>
<li>Each paid <code>requestId</code> is served once, within 1 hour of the payment. A replay gets <code>429 already_served</code>.</li>
<li>No refund key: if a paid request cannot be served, it is listed at <a href="/v1/refunds"><code>/v1/refunds</code></a> and the provider refunds it on-chain with <code>refund(requestId)</code>. If the chain cannot be read for a moment, nothing is served and the same request can simply be retried.</li>
</ul></div>

<h2>Links</h2>
<ul>
<li>Source, tests and the live proof on mainnet: <a href="${REPO}">github.com/swiftbotlab/hsk-meter</a></li>
<li>Settlement contract (verified): <a href="${EXPLORER}/address/${CONTRACT}?tab=contract">${CONTRACT}</a></li>
<li>Live dashboard: <a href="${DASHBOARD}">swiftbotlab.github.io/hsk-meter</a></li>
<li>Hackathon entry: <a href="${BUIDL}">DoraHacks BUIDL 50342</a></li>
<li>Machine-readable terms: <a href="/.well-known/x402"><code>/.well-known/x402</code></a> · status: <a href="/health"><code>/health</code></a></li>
</ul>
<footer>HSK Meter ${esc(VERSION)} · Swift Bot Labs</footer>
</main></body></html>`;
}
