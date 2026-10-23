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

## Reporting

Please open a private security advisory on the repository or contact Swift Bot Labs.
