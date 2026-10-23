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

## Seller server

- Reads the chain only through the official RPC (`https://mainnet.hsk.xyz`, host allowlist, no redirects, response size
  cap, timeouts).
- Inputs are strict: one query parameter per paid path matched by regex, `X-Payer` must be an address, payment headers are
  capped at 8 KB of base64 and 6 KB of JSON, every field is type- and pattern-checked, URLs over 400 characters are refused.
- Quotes carry a server token = nonce + HMAC over (contract, endpoint, payer, resource), so clients cannot choose request ids.
- Every payment is simulated before a transaction is sent; forged signatures, wrong amounts and replays are refused without
  spending gas. Settlement gas has a daily budget; quotes, verifications and settlements are rate limited.
- A paid response is served at most 3 times per request id; a replayed payment header never settles twice.
- Logs never contain tokens, signatures or keys (request-id prefix, payer and tx hash only).

## Keys

- The provider key is hot on the seller: it submits settlements and refunds and pays their gas. If it leaks, an attacker
  can refund paid requests back to their payers, change prices or pause endpoints, and spend its gas balance. It cannot
  withdraw earned credit: that needs the payee key, which never runs on the server.
- The HSP-draft adapter key only signs receipts. Rotate both by registering new endpoints from a new provider key.
- Key files are created with mode 0600 and are never committed or printed.

## Reporting

Please open a private security advisory on the repository or contact Swift Bot Labs.
