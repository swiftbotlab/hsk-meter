# Security

## Contract (`HskPayPerRequest`)

- No owner, no admin functions, no upgrade path, no native coin (nothing is payable, no `receive`/`fallback`).
- Checks-effects-interactions plus `nonReentrant` on every state-changing function. Reentrancy protection is proven with a
  positive control (a hook token that re-enters `refund`, `withdraw` and `pay`), because USDC.e itself has no hooks.
- Payments: the amount must equal the price in force; the contract checks the token balance delta equals the price.
- Authorizations: `receiveWithAuthorization` can only be executed by its `to` (this contract), and the nonce binds the
  signature to one `(endpointId, requestId)`, so a signature cannot be redirected, front-run into a bare transfer, or reused.
- Tokens sent to the contract directly are not credited to anyone and cannot be moved (no admin). Accounting invariants:
  `balance >= totalCredits == sum(credits)` and `totalCredits + totalWithdrawn == totalPaid - totalRefunded`.
- USDC.e can blacklist or pause: a blacklisted payer cannot pay or be refunded (the refund can be retried later); a
  blacklisted payee can still withdraw to another address.
- Known properties (no exploit): `withdraw(to = the contract itself)` would strand those tokens (the payee's choice);
  request ids without a payer prefix can be paid by anyone (the seller only issues payer-bound ids); refunds have no time
  limit and come out of the payee's unwithdrawn credit, which all endpoints of that payee share; smart-wallet (ERC-1271)
  payers are accepted on-chain.
- Anyone can register endpoints on the contract, so contract-wide totals are not this service's numbers: count only
  endpoints 1 and 2 of provider `0x30CCc6d816854d1a66fBfecE15b1515a3E941160`.

## Seller server

- Network: reads the chain only through the official RPC (`https://mainnet.hsk.xyz`, host allowlist, no redirects,
  2 MB response cap, 10 s timeouts). An in-process egress guard (`server/egress.py`) refuses every other outgoing
  connection, including loopback and LAN addresses; tested with a positive control (`server/test_security.py`,
  `deploy/sandbox_probe.py`).
- Inputs are strict: one query parameter per paid path matched by regex, `X-Payer` must be an address, payment headers are
  capped at 8 KB of base64 and 6 KB of JSON, every field is type- and pattern-checked (amounts are ASCII decimal strings),
  URLs over 400 characters are refused. Malformed values are answered with a 4xx (covered by tests).
- Quotes carry a server token = nonce + HMAC over (contract, endpoint, payer, resource), so clients cannot choose request ids.
- A transaction check for a hash that is not on chain gets a free `404` at quote time, and the hash is checked again
  right before settlement.
- An `exact` payment is checked in this order, and nothing is sent until every step passes:
  1. the EIP-3009 signature is recovered locally (65 bytes, v 27/28, low s) and must be from `authorization.from`:
     a forged signature costs no RPC call, no rate-limit slot and no gas;
  2. the payer must be a plain account (no code: smart wallets and EIP-7702 delegated accounts use the self-paid
     scheme), hold the price in USDC.e, and not have used the authorization nonce;
  3. the transaction is simulated with `eth_estimateGas` and `eth_call` using the exact gas limit, `maxFeePerGas` and
     `maxPriorityFeePerGas` that are then signed.
- Rate limits: per client (IPv4 address or IPv6 /64; `CF-Connecting-IP` is trusted only when the server listens on
  loopback and the connection comes from loopback, i.e. a local tunnel) 60 quotes and 30 payment attempts per minute;
  per payer 3 settlement attempts per minute; globally 12 settlement transactions per minute, counted only for
  transactions actually sent. A payer whose settlement reverts on-chain is refused for 24 h.
- Gas: separate daily budgets for settlements (2 HSK) and refunds (0.5 HSK). A settlement is refused while its expected
  gas costs more than the sale (HSK valued at a configured ceiling, default $0.10). Prices are set to at least twice
  the worst settlement gas measured. When the provider holds less than twice the daily budgets, the seller raises an
  alert (log line, journal entry, `ALERT` file in the state directory, `/health`).
- One request id is processed by one thread at a time (atomic reservation): concurrent submissions of one payment
  settle once and are served once. A paid response is served at most 3 times, within 1 h of the payment; a replayed
  payment header never settles twice.
- If the authorization was already used (for example the buyer submitted it to the contract itself) or a settlement is
  slow, the seller reads `getPayment(requestId)` and the `Paid` log by request id and serves the payer; it never sends a
  second settlement while one is pending.
- Refunds: when nothing can be delivered after settlement (upstream failure, or an HSP-draft mandate the verifier does
  not accept), the payment is refunded on-chain. A refunded payment is never served (`410`); a payment that was served
  is never refunded. At most 2 refunds per payer per UTC day (more are queued for the next day). Refunds that fail are
  retried by a background reconciler (up to 5 attempts).
- Self-paid scheme (`hsk-paywall-tx`): the seller reads `getPayment(requestId)` first and finds the `Paid` log by request
  id; a transaction hash sent by the client is never fetched.
- HSP-draft: a mandate must require only capabilities this adapter proves and stay valid for at least 120 s, checked
  before settlement. A payment has one receipt: the same mandate gets the same receipt back, another mandate is refused.
- State (settled, served, refunded and pending payments, HSP receipts, payer bans, refund counts, gas spent today) is
  journaled to a 0600 JSONL file and replayed at start, so a restart does not reset serve caps, refund caps, bans or
  the daily gas budgets (the per-minute rate limits are kept in memory).
- At most 128 open connections and 32 requests in progress (`503` beyond), 10 s idle timeout.
- Logs never contain tokens, payment signatures or keys (request-id prefix, payer and tx hash only). The journal keeps
  the HSP receipt of a payment and the payer's mandate signature (never a payment authorization).
- `--smoke-refund-address` is an operator test switch (refused unless the server listens on loopback): requests for
  that one address are refunded instead of served. It is off by default.

## Process isolation

`deploy/run_sandboxed.sh` runs the seller under bubblewrap. Visible inside: `/usr`, `/etc`, the repository's `server/`
and `deploy/` and the deployment record (read-only), the Python environment (read-only), the key directory (read-only)
and the state directory (read-write); private `/tmp`, `/run` and `/home`, own PID/IPC/UTS namespaces, a clean
environment, all capabilities dropped. The launcher stays in the foreground as bubblewrap's parent: SIGTERM to it stops
the whole sandbox, SIGKILL to it kills the sandbox too (`--die-with-parent`), and a seller started in the background
keeps running after the shell that started it exits. Run a long-lived seller as a systemd user unit (template:
`deploy/hsk-meter-seller.service`); `HSK_METER_DIE_WITH_PARENT=1` makes the sandbox die with whatever started the
launcher instead. `deploy/sandbox_probe.py` shows what the process can reach: inside the sandbox
it cannot see other key files or the rest of the home directory, cannot write its own code or its keys, and (with the
egress guard) cannot connect to loopback services such as a browser debugging port, the LAN or any host other than the
RPC. Positive controls: the same probe outside the sandbox reaches all of them, and inside the sandbox with the guard
off the network targets become reachable.

## Keys

- The seller reads three files from its key directory, each holding exactly one value (mode 0600, a regular file, not a
  symlink): `provider.env` (the provider key), `adapter.env` (the HSP-draft adapter key) and `token.env` (the quote-token
  HMAC key). It refuses a file that holds any other variable and never creates key files.
- The provider key is hot on the seller: it submits settlements and refunds and pays their gas. If it leaks, an attacker
  can refund paid requests back to their payers, change prices or pause endpoints, and spend its gas balance. It cannot
  withdraw earned credit: that needs the payee key, which the seller never loads and, in the sandbox, cannot read.
- The HSP-draft adapter key only signs receipts. Rotate both by registering new endpoints from a new provider key.
- The buyer reads only `HKPW_BUYER_PK` from its key file. Key files are never committed or printed.

## Buyer (`agent/buyer.py`)

- Before signing anything: network, asset and settlement contract are checked; the endpoint must belong to the pinned
  provider and pay the pinned payee (`--expect-provider`, `--expect-payee`); the price must match the contract and stay
  under `--max-price`.
- The spend cap counts a payment before the authorization is signed (or the pay transaction sent). The reservation
  becomes spent when the payment is verified, and is released only when the chain shows nothing can be charged any more
  (`getPayment` unpaid and, after `validBefore`, `authorizationState == false`).
- After signing, any answer without the data that is not a definitive seller refusal (no answer, a redirect, a `5xx`, a
  `200` without data or with a body that is not JSON) leaves the outcome unknown. The signed payment is written to the
  session ledger (mode 0600, under its lock) before the request is sent and kept for 50 minutes, inside the seller's
  1 h re-serve window. The next purchase of the same URL resends that same signed payment instead of signing a new
  one, also after a restart or from another process on the same ledger; a request id is charged at most once on-chain,
  so the retry never pays twice. A settlement transaction named in `PAYMENT-RESPONSE` is used only after its `Paid`
  event is verified on-chain.
- A `502` that says the payment was already served and is not refunded (the seller delivered it before, for example
  into an answer that was lost, and its data source failed on this re-serve) keeps the signed payment too: retrying
  after the seller's `Retry-After` resends the same payment. The entry is dropped only on a verified, pending or queued
  refund, or on a definitive refusal such as `429 already_served` or `410 payment_expired`.
- Re-serve window: after a delivered answer the signed request stays in the ledger for 10 minutes (inside the seller's
  1 h / 3-serve window). A call for the same URL in that window, for example a retry after a client timeout, resends
  it and the seller re-serves the paid request for free. Only an explicit new purchase (`new_purchase` in the MCP tool,
  `--new-purchase` on the command line) pays again; it never skips a payment whose outcome is still unknown. The
  headers kept are authorizations that are already used and cannot be charged again.
- Time budget: one purchase is bounded at 45 s by default (`--time-budget`), below common MCP client timeouts. The paid
  request's timeout, the seller's "still settling" retries and the receipt polls stop at the budget; nothing is signed
  with less than 15 s left. A payment that is not finished by then is reported as unconfirmed and resent on the next
  call, never bought again. A receipt poll cut short is reported as unknown, never as missing. Single RPC reads keep
  their own 10 s timeouts.

## MCP server (`agent/mcp_server.py`)

- The MCP client is treated as untrusted input. It can only name a URL and, optionally, ask for HSP verification or a
  lower price limit. The key, the allowed seller origins (exact scheme, host and port), the pinned provider, payee and
  adapter, the per-request price limit, the session cap and the payments-per-minute limit are fixed by the operator at
  start. Unknown arguments, URLs with credentials or fragments, non-`/v1/` paths and non-printable characters are refused
  before any request is made.
- Every payment goes through the buyer's guard above. Seller data is returned as untrusted content and is never
  interpreted. Messages over 1 MB, batches and malformed JSON are rejected; logs go to stderr without keys or signatures.
- Retry semantics of `hsk_meter_pay`: calling it again with the same URL never pays twice. While a payment's outcome is
  unconfirmed (`resendOnRetry`, with `retryAfterS` when the seller gave one) the call resends the same signed payment.
  For 10 minutes after a delivery the call is re-served for free under the same payment (`resent`, `redelivery`, a
  `note`), unless the client passes `new_purchase: true`, which pays for a new response. `new_purchase` is ignored, and
  the payment is resent, while an earlier payment for that URL has no final outcome. `hsk_meter_status` lists both
  kinds (`pendingResends`, `reServable`) without headers.
- Cancellation: stdin is read on a separate thread, so `notifications/cancelled` reaches the call it names. A running
  call stops waiting, keeps its pending resend in the ledger and sends no response (as MCP asks); a call cancelled
  while still queued is never run. The client's next call for the same URL then resends or re-serves the payment.

## HashKey ecosystem reads (`server/oracle.py`, `server/kyc.py`)

- Read-only: the APRO price feeds and the KYC SBT are read with `eth_call` through the same allowlisted RPC; nothing
  is signed or sent, and the egress guard and sandbox are unchanged.
- Price feeds: a reading is used only if the proxy reports the pinned pair and 8 decimals, the round is finished
  (`answeredInRound >= roundId`), the answer is positive and inside a sane range (USDC/USD 0.5-1.5, HSK/USD
  0.0001-1000), the round time is not in the future and the round is at most 26 h old. Anything else counts as
  unavailable. A failed read is retried after 30 s at the earliest (no RPC storm), an RPC blip keeps the current
  reading, and a reading that fails validation is dropped at once.
- What a wrong or manipulated feed can do: the gas guard prices HSK at `max(--hsk-usd, HSK/USD)`, so a feed can only
  make it stricter. A feed reporting a huge HSK price makes the seller refuse settlements before sending anything
  (tested); one reporting a tiny price changes nothing. The quote's `usd-estimate` is informational: the buyer signs
  the USDC.e amount in `accepts[]`, and the buyer computes `amountUsd` from its own feed read, never from the seller's
  claim. The settlement path reads only the cached reading, never the chain.
- Dashboard: the USD value comes from the feed aggregator's own `AnswerUpdated` logs on the explorer (address-filtered,
  so only that contract can add them), with the same checks, inserted with `textContent`; the CSP is unchanged.
- KYC compliant mode is off unless the operator passes `--require-kyc`. When on, the decision is the SBT's own
  `isHuman` verdict at the required level; an unreadable SBT fails closed (`502`, no quote, no settlement). Results are
  cached for 60 s per address, so a revocation can take up to 60 s to apply; a payment made in that window is served,
  and one whose SBT lapsed between quote and a self-paid payment is refunded on-chain before anything is served. The
  SBT address is not source-verified on the explorer (see the README for how it was checked); `--kyc-sbt` overrides it.

## HSP gateway rail (`server/hsp_gateway.py`, `server/hsp_merchant_agent.py`, `agent/hsp_payer.py`)

- Off by default. It switches on only when `secrets/hsp/hsp.env` holds `APP_KEY`, `APP_SECRET` and `BASE_URL`. That
  file and the merchant private key must be regular files (no symlinks), owned by the service user, mode `0600`, or
  they are refused. `BASE_URL` must be one of the three documented hosts over https. Staging and production take
  mainnet tokens and are refused unless `ALLOW_MAINNET=1` is set on purpose; a mainnet network on the QA gateway is
  refused too.
- Secrets never leave the process: no logging of `app_secret`, the merchant key, JWTs or signing strings; `repr()` of
  the key and config objects shows public parts only. The ES256K signer (eth-keys' secp256k1 code) derives
  its nonces deterministically, RFC 6979-style (HMAC-DRBG over the key and the digest), so no RNG is involved.
- Outgoing calls: no redirects, 15 s timeouts, 1 MB response cap, strict JSON (no duplicate keys, no floats, no
  NaN). The receiving agent installs the egress guard for the data RPC host, the gateway host and, with
  `--verify-onchain`, the two testnet RPC hosts.
- Receiving agent: loopback only. Rate limits: 60 quotes per minute per client, 10 new gateway orders per minute per
  payer, 60 per minute in total, and 30 paid-status checks per minute per client. An unpaid open order is re-offered
  instead of creating another. It serves only when a payment record for THIS order matches: cart id,
  `payment_request_id`, token, payee, chain, an amount of at least 99% of the USD total, a tx hash, and the payer equal
  to `X-Payer`, which must prove it holds that wallet: `X-HSP-Proof` is an EIP-191 signature by `X-Payer` over the
  flow id and the resource (the flow id alone is not a secret: it is in the `payment_url`). The record comes from a verified webhook or the HMAC-signed merchant query, and with
  `--verify-onchain` the token `Transfer` must also be found in the transaction itself. A paid order is served at most
  3 times within 1 h. No refund API is documented, so a paid order whose data source fails stays servable rather than
  being dropped.
- Webhooks: 64 KB cap, `t=`/`v1=` parsed strictly, 300 s tolerance, HMAC over the raw bytes compared in constant
  time, documented statuses only. The 0600 journal applies each delivery at most once: replays of a captured
  delivery, retries, late lower statuses and anything after a terminal state change nothing.
- Payment agent: nothing is fetched from, and nothing is signed for, a gateway that is not pinned. The merchant key
  is pinned out of band; a key inside the offer is only compared. The JWT must be ES256K and verify, and `cart_hash`
  must equal the SHA-256 of the canonical cart. The documented cart rules must hold, and the cart must be for the
  exact resource requested. The token must be in the documented table with an on-chain-verified EIP-712 domain
  (EIP-3009 only: Permit2's spender is undocumented). Network, payee, per-payment maximum, budget and payment count
  are AP2-style constraints. The amount is reserved in a 0600 ledger before signing, and one flow gets one signature
  (a retry re-uses it). The authorization is valid for 5 minutes at most and never past the cart's expiry. When the
  gateway's own copy of the order can be read, its cart, `merchant_authorization` and `multi_pay` must match the
  offer. Submission goes only to the pinned gateway's `/api/v1/payment/` routes (the wire format the hosted checkout
  uses; see HSP_STATUS.md); `--sign-only` submits nothing. A resubmission after a lost answer sends the same mandate
  (same EIP-3009 nonce), so it can never pay twice. The ledger keeps the signed mandate, which can move exactly that
  amount to that payee, so it is kept owner-only.
- `user_authorization` is an HS256 JWT whose key is the payer's own address (that is how the checkout builds it). The
  address is public, so this JWT binds the cart and mandate hashes but authenticates nobody: anyone can make one. The
  authority to move funds is the wallet's EIP-3009 signature inside `payment_payload`, which fixes payee, amount,
  token, chain and validity. The payment agent never treats an HS256 JWT it receives as proof of anything.
- The mock gateway (`server/hsp_mockgw.py`) is for tests only: loopback, in memory, no chain, no funds.

## Public endpoint (`worker/`, Cloudflare Worker, keyless)

The always-on endpoint `https://hsk-meter.swiftbotlab.workers.dev` sells the same two requests as the seller, over the
same wire protocol, but it holds **no private key**. It offers only the self-paid scheme `hsk-paywall-tx`: the buyer
calls `pay(endpointId, requestId, amount)` itself and the Worker verifies that payment on-chain. It cannot submit
signed `exact` authorizations, pay gas, refund, change prices or touch credit.

What it holds, and what a compromise means:

- Its only secret is `PAYWALL_TOKEN_KEY`, the HMAC key of the quote tokens (a Worker secret, never in the code, the
  repository or the logs). It cannot move funds. With it, someone could mint quote tokens, which still have to be paid
  on-chain from the bound payer address before anything is served. Rotating it invalidates outstanding quotes; a
  request paid against an old token can then no longer be served and is refunded by the provider. The token's HMAC
  context names this service, so a token of the full seller never verifies here, even if a key were ever shared.
- Whoever controls the Cloudflare account could change the code, for example to serve without payment or to report
  false receipts. They still cannot move funds: payments credit the payee inside the contract, and only the payee key,
  which is never on any server, can withdraw. Buyers do not have to trust the endpoint's word: `agent/buyer.py`
  re-checks the `Paid` event and `getPayment` itself.

Threats and what stops them:

- Data without a matching payment: the request id is recomputed from `X-Payer`, the exact resource and the quote
  token. The token carries the quoted price, and its HMAC must verify for that payer, endpoint, resource and price.
  The Worker then reads `getPayment(requestId)` and the `Paid` log by request id in the block `getPayment` names (a
  transaction hash sent by the client is never fetched). Payer, endpoint, amount (must equal the quoted price), payee,
  refund flag and age (paid less than 1 h ago) must all match, and the endpoint must belong to the pinned provider and
  payee. A zero `X-Payer` is refused (it would yield an unbound request id).
- Data twice for one payment (replays, concurrent requests in different isolates or Cloudflare locations): every paid
  request id has its own Durable Object, which serializes its state changes (strongly consistent; Workers KV is
  eventually consistent and could let a race through, so it is not used). A request first claims the id (a lease of
  60 s), computes the answer, commits "served", and only then sends the data; a commit whose call errored is
  confirmed by its claim id before anything is sent. A replay gets `429 already_served` after one `getPayment` read.
  If the Durable Object cannot be reached, nothing is served (`503`). Each payment buys one response (the full seller
  allows 3 re-serves; this endpoint allows none, so a response lost in transit is not re-sent).
- Paid but not served: if the chain cannot be read, or the transaction to check is not found (a lagging node), the
  claim is released and the same request can be retried. A transaction check that still finds nothing after 3 tries
  over 2 minutes, and a payment whose buyer comes back after the 1 h window without having been served, are marked
  undeliverable and listed at `/v1/refunds` (re-listed on every later retry if the listing failed). The provider
  refunds them on-chain with `refund(requestId)`: refunds are an operator step here because the Worker has no key. A
  buyer who pays and never comes back is not detected by the Worker; `/v1/receipt/<requestId>` then shows "not served,
  the window is over" for the provider to refund.
- SSRF: the only outbound host is the pinned constant `https://mainnet.hsk.xyz`; no request input becomes part of a
  URL. Redirects are not followed, responses are capped at 1 MB (256 KB for transaction receipts), each call times out
  after 8 s and one request may make at most 2 x 40 RPC calls.
- Inputs: a route allow-list, exactly one query parameter per paid route with an exact hex pattern, header formats
  checked (`X-Payer` 20 bytes, token and tx 32 bytes), URLs over 400 characters refused, `GET` and `OPTIONS` only.
- Cost amplification: per client (IPv4 address or IPv6 /64) 60 quotes, 30 paid retries, 30 receipt checks and 120
  free requests per minute, plus 4x that per IPv6 /48 and 20x per isolate, counted in the isolate and by Cloudflare's
  rate-limit bindings at each location. Verify requests for unpaid request ids never touch the Durable Objects. A
  transaction check for a hash that is not on chain gets a free `404`, and one whose receipt is over 256 KB a free
  `422`, before any payment; mined answers are cached for 10 minutes. The receipt verifier reads one 5,000-block window
  for a refund (`getPayment` decides), looks at no more than 2 payments per transaction, caches answers for 30 s, and
  says nothing about transactions without the contract's events (checking those is what endpoint 2 sells).
- Availability: on the Workers free plan every request, refused or not, counts against the account's daily request
  quota, and nothing inside a Worker can prevent that. A flood can make the endpoint unavailable until the quota resets
  (00:00 UTC); it cannot make it serve without payment. The fix is a WAF rate-limit rule in front of a custom domain.
- CORS: `Access-Control-Allow-Origin: *` without credentials (a public read API with no cookies or sessions); only
  the protocol's headers are allowed and exposed. The home page is static HTML without scripts under
  `Content-Security-Policy: default-src 'none'`; values shown in it are escaped.
- Logs: path, status, duration, request-id prefix, payer and transaction hash; never tokens or headers. Per-request
  invocation logs are turned off, so request headers are not stored by the platform either.
- Payments are verified at the `latest` block, as the full seller does; a reorg after serving would undo a payment of
  a few cents.
- Tests: `worker/test/run.mjs` (offline, 53 tests: replay, races, wrong amount, wrong endpoint, wrong payer, forged
  tokens, price changes, refunded, expired, refund listing, lease expiry, served-set outages, RPC faults, rate limits,
  receipt verdicts), checked against the Python reference with shared vectors; mutation-checked (26 of 27 guard
  mutants killed; the survivor, dropping the explicit redirect check, is equivalent because redirects are never
  followed). `worker/test/test_buyer_compat.py` runs the unchanged buyer against the Worker end to end on the offline
  chain model. An independent security review found no way to get data without a matching payment or twice for one
  payment; its findings on refund listing, cost amplification, receipt size, token context and logging are fixed as
  described above.

## Known limitations

All three are low severity and not fixed yet. The first and the third never cost the buyer an extra payment; the
second can lead to one extra purchase, only in the rare case described.

- Serve limit vs. re-serve window: the seller serves a paid response at most 3 times. Inside the buyer's 10-minute
  re-serve window, the 4th call for the same URL gets the seller's `429 already_served` and returns an error
  (`paid_not_delivered`, with correct advice); the 5th call pays again. The advice returned with the 3rd serve still
  says another call within 10 minutes is a free re-serve. An agent that polls one URL sees an error on every 4th call.
  Planned fix: count serves on the buyer side and stop re-serving at the seller's limit, or word the advice as
  "up to 3 serves".
- Two processes on one session ledger (theoretical, not reproduced): when process A re-serves a delivered entry for a
  `(payer, url)` while process B buys the same URL with `new_purchase` and writes its new signed payment ahead to the
  same ledger key, A's final write can overwrite B's entry. If B finishes, its own final write restores it. If B is
  killed before that write, its unknown-outcome entry is lost, and once A's window has passed the next call for that
  URL buys again even if B's payment settled. All three conditions are needed at once: a shared ledger, `new_purchase`
  and a crash. Planned fix: write the delivered entry only when the key is empty or still holds the same request id
  (compare-and-set under the ledger lock, as the drop already does).
- Cancellation matching: the MCP stdin reader recognises `tools/call` and `notifications/cancelled` by a raw byte
  match before it parses the line. A client that escapes the slash in the method name (valid JSON, for example
  `"notifications\/cancelled"`) is still handled correctly, but its call is not registered for cancellation, so a
  cancel is ignored and the call runs to completion. This only affects cancellation: the 10-minute re-serve window
  still covers the client's retry. Planned fix: match on the parsed method.

## Dependencies

- Solidity: OpenZeppelin 5.1.0 and forge-std v1.16.2, vendored unmodified; provenance and hashes in
  `contracts/DEPENDENCIES.md`, re-checked by `contracts/verify_deps.sh`.
- Python: pinned in `requirements.txt`.
- Worker: `@noble/hashes` 2.4.0 (keccak-256) and, for deploying only, `wrangler` 4.151.1, pinned with integrity
  hashes in `worker/package-lock.json`; installed with `npm ci --ignore-scripts`.

## Reporting

Please open a private security advisory on the repository or contact Swift Bot Labs.
