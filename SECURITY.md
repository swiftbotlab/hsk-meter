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
environment, all capabilities dropped. `deploy/sandbox_probe.py` shows what the process can reach: inside the sandbox
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

## Dependencies

- Solidity: OpenZeppelin 5.1.0 and forge-std v1.16.2, vendored unmodified; provenance and hashes in
  `contracts/DEPENDENCIES.md`, re-checked by `contracts/verify_deps.sh`.
- Python: pinned in `requirements.txt`.

## Reporting

Please open a private security advisory on the repository or contact Swift Bot Labs.
