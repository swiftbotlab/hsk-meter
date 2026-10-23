# HSK Meter

**One agent pays another for every API request, in USDC.e on HashKey Chain, settled on-chain per request.**
If the seller cannot deliver after it has been paid, the payment is refunded on-chain automatically, and on the
default x402 path the buyer needs no gas token. With an HSP-style mandate attached, the seller ships the data only after a verifier has
checked the adapter-signed receipt against the buyer's signed mandate.

By Swift Bot Labs, for the HashKey Chain Horizon Hackathon (India), track **Agent Payments & A2A Settlement**.

**Live dashboard:** [swiftbotlab.github.io/hsk-meter](https://swiftbotlab.github.io/hsk-meter/) reads the contract's
events straight from the public explorer in your browser: totals, USDC.e per day, refund rate and the latest payments,
each linked to its transaction.

| | |
|---|---|
| Network | HashKey Chain mainnet, chain id `177` |
| Settlement contract | [`HskPayPerRequest` `0x78C8476772BC209C3df09Da5e129d0879cd57F8c`](https://hsk.blockscout.com/address/0x78C8476772BC209C3df09Da5e129d0879cd57F8c?tab=contract), source verified on Blockscout and [Sourcify](https://repo.sourcify.dev/177/0x78C8476772BC209C3df09Da5e129d0879cd57F8c) (exact match) |
| Settlement token | USDC.e [`0x054ed45810DbBAb8B27668922D110669c9D88D0a`](https://hsk.blockscout.com/token/0x054ed45810DbBAb8B27668922D110669c9D88D0a) (Circle `FiatTokenV2_2`, EIP-3009) |
| Payment protocol | x402 v2 (`402 Payment Required`, `PAYMENT-SIGNATURE`, `PAYMENT-RESPONSE`), network `eip155:177` |
| Seller | provider [`0x30CCc6d816854d1a66fBfecE15b1515a3E941160`](https://hsk.blockscout.com/address/0x30CCc6d816854d1a66fBfecE15b1515a3E941160), endpoints `1` and `2` |

## Live proof on mainnet

Every row is a real request made over HTTP through the seller in this repository, paid by the buyer client in
this repository. Rows 1-5 ran v0.1 at the launch prices; rows 6-8 ran the hardened v0.2 inside its sandbox.

| # | Request | Payment path | Transaction |
|---|---|---|---|
| 1 | address profile, 0.01 USDC.e | x402 `exact` + HSP-style mandate, buyer paid no gas, verifier ACCEPT | [`0x98526a6b…`](https://hsk.blockscout.com/tx/0x98526a6b276b3763cde68297c29f89f194a5fed99a0add55794f57ece123da79) |
| 2 | tx check (of #1), 0.005 | x402 `exact` | [`0x672b4957…`](https://hsk.blockscout.com/tx/0x672b495773c17c6216ec24d7981091d2209d43e545e17b463a7c9744d830dfc5) |
| 3 | tx check of a hash that does not exist, 0.005 | x402 `exact`, then automatic **refund** | paid [`0x63d7f42d…`](https://hsk.blockscout.com/tx/0x63d7f42d638b79fb1a16404ca025a1524c6c635f601c6f99d8c0df611f4a0f52), refunded [`0xaa744ebd…`](https://hsk.blockscout.com/tx/0xaa744ebdacf908b511dd5e23cc592cd49c55e9f466012a0b26e3388e909c40fc) |
| 4 | address profile, 0.01 | buyer calls `pay()` itself and proves it | [`0xfb36cf54…`](https://hsk.blockscout.com/tx/0xfb36cf54ef11efccca707b465ac9ff95dda1f075eab59136485369ce76783856) |
| 5 | tx check, 0.005 | x402 `exact`; the same header replayed: served 3 times, settled once, then refused | [`0x04ddbc59…`](https://hsk.blockscout.com/tx/0x04ddbc5973a69cd5620f3eb516b6ddbc6be2be4630efeda507fa3ca0c181917e) |
| 6 | address profile, 0.03 | x402 `exact` + HSP-style mandate, verifier ACCEPT, adapter pinned | [`0x5bf9515b…`](https://hsk.blockscout.com/tx/0x5bf9515b09692b156cfbe080b451ab0a8a74e2a2e41e2bb8d93aecc0f770a7c9) |
| 7 | tx check, 0.025 | x402 `exact` | [`0x3a88578a…`](https://hsk.blockscout.com/tx/0x3a88578a594d3e16d493c68fd168e20e0a750d92ce16bd7f216721877f7147ca) |
| 8 | address profile, 0.03 | x402 `exact`; delivery failed (operator test switch), automatic **refund** | paid [`0x543e14fb…`](https://hsk.blockscout.com/tx/0x543e14fbf0aa9e15932405e70df912bca1d0b1da40be8b33ad1d4b88e557f9b5), refunded [`0xd6e4e725…`](https://hsk.blockscout.com/tx/0xd6e4e7257aa7a472566dc0a27712b58a23b556a9ddb39e93504f582707fdfb0c) |

Setup transactions: deployment [`0xe08891e3…`](https://hsk.blockscout.com/tx/0xe08891e35a7197a62a5cb165a6e2dea194842f3dba00711fc65207cec120fe73),
endpoint registrations [`0xec7dd370…`](https://hsk.blockscout.com/tx/0xec7dd370d753d746a264e3bd13acdca1733b323132e5cbe5184a573ec56f27b4)
and [`0x97e3dd2d…`](https://hsk.blockscout.com/tx/0x97e3dd2d6b5cc86a0a81231f84deaf6ae706bbbcf6a2bf916c3750f170464a82),
price changes [`0x62188d0a…`](https://hsk.blockscout.com/tx/0x62188d0abc975d81fd2212ea2197dd2d990fd2be3a168a48b7ba995a58980ef4)
and [`0x86e73f35…`](https://hsk.blockscout.com/tx/0x86e73f35a5ebb00efc93b8cc449838b812ec601ccb336e904c99530832b6576c),
payee withdrawal [`0x159f8304…`](https://hsk.blockscout.com/tx/0x159f830494dd86272b9e2ba53d9801a5b4a3c190f38a5ef46a54ccf36e67bbe8).

Prices are set on-chain per endpoint: address profile **0.03 USDC.e**, transaction check **0.025 USDC.e**. Each
price covers at least twice the worst settlement gas measured (246,503 gas at 500 gwei = 0.123 HSK, about $0.011),
because the seller pays the gas of every `exact` settlement.

> [!NOTE]
> Anyone can register endpoints on the contract, so contract-wide totals are not this service's numbers. Count only
> endpoints `1` and `2` of provider `0x30CCc6d816854d1a66fBfecE15b1515a3E941160`.

## How a request is paid

```mermaid
sequenceDiagram
    autonumber
    participant B as Buyer agent (agent/buyer.py)
    participant S as Seller agent (server/seller.py)
    participant C as HskPayPerRequest on HashKey Chain
    B->>S: GET /v1/hsk/address-profile with X-Payer
    S->>C: read the endpoint price
    S-->>B: 402 PAYMENT-REQUIRED, requestId bound to the buyer, HSP mandate offer
    Note over B: checks network, token, contract, provider, payee, price and spend cap, then reserves the amount
    B->>B: signs EIP-3009 ReceiveWithAuthorization and the HSP mandate
    B->>S: GET again with PAYMENT-SIGNATURE
    S->>S: recovers the signature locally, checks the payer on-chain, simulates the exact transaction
    S->>C: payWithAuthorization (seller pays the gas)
    C-->>S: Paid event, payee credited
    alt delivery works
        S->>S: does the paid work, adapter signs the receipt, verifier must ACCEPT
        S-->>B: 200 with data, PAYMENT-RESPONSE and the HSP receipt
        B->>C: re-checks the Paid event and getPayment, re-runs the verifier with the pinned adapter
    else delivery fails after payment
        S->>C: refund(requestId)
        C-->>B: Refunded event, USDC.e back to the buyer
        S-->>B: error with the refund transaction
    end
```

The contract (`contracts/src/HskPayPerRequest.sol`, no admin, no upgrades, nothing payable):

- Providers register priced endpoints; a payee must consent to the provider that pays it.
- `payWithAuthorization` pulls exactly the price with USDC.e `receiveWithAuthorization`. The EIP-3009 nonce must equal
  `authorizationNonce(endpointId, requestId) = keccak256(abi.encode(contract, endpointId, requestId))`, so one signature
  pays one request on one endpoint and nothing else. Only the contract can execute a `ReceiveWithAuthorization`, so a
  copied signature cannot be front-run into a bare transfer.
- `pay(endpointId, requestId, amount)` is the same payment for buyers that approve and pay themselves.
- A `requestId` carries its payer in the top 20 bytes, so nobody else can pay (or burn) it, and it is paid at most once.
- Price changes apply to new payments only; a stale quote gets `WrongAmount`, never a different charge.
- The provider can refund a paid request once, out of the payee's unwithdrawn credit. Payees pull their credit with
  `withdraw`.

### x402 v2 profile

Standard parts: the `PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` / `PAYMENT-RESPONSE` headers (base64 JSON),
`x402Version: 2`, `accepts[]` with `scheme: "exact"`, `network: "eip155:177"`, `amount`, `asset`, `payTo`,
`maxTimeoutSeconds`, and a `PaymentPayload` of `{signature, authorization: {from, to, value, validAfter, validBefore, nonce}}`.

Specific to this rail (declared in `accepts[0].extra`): the signed type is `ReceiveWithAuthorization`
(`primaryType`, `settlement: "contract"`), `payTo` is the settlement contract, and the nonce comes from the quote
(`extra.nonce`) because it binds the payment to one request. A client that signs a random-nonce
`TransferWithAuthorization` is refused before anything is sent. `accepts[1]` (`hsk-paywall-tx`) is the self-paid
alternative.

## Quickstart

Requirements: Python 3 (tested with 3.14) and `pip install -r requirements.txt` (pinned `eth-account`, `eth-abi`,
`eth-utils`, `hexbytes`). Key files hold one `NAME=0x...` value each, mode `0600`, and are never committed.

### Buyer: pay for a request

The buyer needs USDC.e on HashKey Chain and no HSK (the seller pays the gas of `exact` settlements).

```sh
echo "HKPW_BUYER_PK=0x..." > buyer.env && chmod 600 buyer.env      # your buyer key, kept out of the repo

python3 agent/buyer.py "https://SELLER/v1/hsk/address-profile?address=0x78C8476772BC209C3df09Da5e129d0879cd57F8c" \
  --key-file buyer.env \
  --expect-provider 0x30CCc6d816854d1a66fBfecE15b1515a3E941160 \
  --expect-payee 0xA8A49A6Ca872D54a8FD5FB121139De9bff62a48D \
  --max-price 0.05 --session-cap 0.20 \
  --hsp --pin-adapter 0x4D281e0a98fD40Bb824F88D5e037bd8d03C4403a
```

The buyer refuses to sign unless the quote is for chain 177, USDC.e, this contract, the pinned provider and payee,
and the on-chain price, and unless the amount fits under `--max-price` and the session cap. It then verifies the
`Paid` (or `Refunded`) event and `getPayment` itself. `--scheme tx` pays with its own `pay()` transaction instead
(that needs a little HSK for gas).

### Seller: sell an API per request

1. Register your endpoints on the contract from a provider key, and let the payee consent once, for example with
   Foundry's `cast` and an encrypted keystore account:

   ```sh
   RPC=https://mainnet.hsk.xyz; METER=0x78C8476772BC209C3df09Da5e129d0879cd57F8c
   cast send $METER "approveProvider(address,bool)" $PROVIDER true --rpc-url $RPC --account payee
   cast send $METER "registerEndpoint(uint128,address,string)" 30000 $PAYEE "GET /v1/your/path" --rpc-url $RPC --account provider
   ```

   Prices are in USDC.e base units (6 decimals): `30000` = 0.03 USDC.e.
2. Map each paid path to its endpoint id in `RESOURCES` in `server/seller.py`.
3. Put three files in a key directory: `provider.env` (`HKPW_PROVIDER_PK`), `adapter.env` (`HKPW_ADAPTER_PK`, signs
   HSP receipts) and `token.env` (`PAYWALL_TOKEN_KEY`, 32 random bytes in hex). The payee key never goes on the server.
4. Run it in the bubblewrap sandbox (recommended) or directly:

   ```sh
   HSK_METER_KEY_DIR=/path/to/keys HSK_METER_VENV=/path/to/venv deploy/run_sandboxed.sh seller --port 8402
   python3 server/seller.py --key-dir /path/to/keys --port 8402      # without the sandbox
   ```

   Free routes: `GET /`, `GET /health`, `GET /.well-known/x402`. The payee withdraws earned credit with
   `withdraw(to, amount)` from its own wallet.

## Security model

Full details and the threat model are in [SECURITY.md](SECURITY.md). In short:

- **Contract:** no owner, no admin, no upgrade path, nothing payable; `nonReentrant` plus checks-effects-interactions
  everywhere; the payment amount must equal the price in force and the token balance delta must match; refunds happen
  at most once and only from the provider. Tests run against the real USDC.e implementation code read from HashKey
  Chain, including a model-based invariant suite; eight hand-written contract mutants were all caught by the unit tests.
- **Seller:** a payment is checked in order and nothing is sent until every step passes: local signature recovery, then
  the payer on-chain (plain account, balance, unused nonce), then a simulation with the exact gas and fee fields that
  get signed. Per-client, per-payer and global rate limits; separate daily gas budgets for settlements and refunds;
  a low-balance alert; atomic per-request reservation (a payment settles once and is served at most 3 times within an
  hour); a refunded payment is never served; state is journaled and replayed at start.
- **Isolation:** the seller runs under bubblewrap with only its code, its Python environment, its own key directory
  (read-only) and its state directory visible, and an in-process egress guard that allows only the RPC host.
- **Buyer:** spend cap reserved before anything is signed, provider and payee pinned, every claim re-checked on-chain.

## HSP: what follows the published spec, and what is still draft

The public HSP developer guide describes a payer-signed EIP-712 **mandate**, a **receipt** signed by a settlement
**adapter**, optional **attestations**, and one verifier rule: ACCEPT if and only if the required capabilities are a
subset of the satisfied capabilities, with outcome classes ACCEPT, RETRYABLE, POLICY and PERMANENT, and
paymentId = mandate hash. The HSP SDK and its EIP-712 type definitions are not published yet, and the sandbox
Coordinator was not online while this was built.

Implemented today (`server/hsp_draft.py`, labeled `hsp-draft/swiftbotlab-0`):

- An EIP-712 encoding of the documented mandate fields, bound to the paywall request (`reference = requestId`).
- An adapter for this contract rail that signs a receipt only after the `Paid` event is observed on HashKey Chain.
- The verifier: signatures, mandate and receipt match, deadline, on-chain re-check, and the subset rule. The seller
  ships only on ACCEPT; the buyer re-runs the verifier with the adapter address pinned out of band
  (`--pin-adapter 0x4D281e0a98fD40Bb824F88D5e037bd8d03C4403a`). A mandate that is not accepted after settlement is
  refunded.

> [!IMPORTANT]
> Still draft until the SDK ships (each is marked `TODO(hsp-...)` in the code): swapping our draft types for the official
> `@hsp/core` types, running the `@hsp/devkit` conformance suite against this adapter and registering it, and reporting
> mandates and observations to the hosted Coordinator (`POST /payments`, `/payments/:id/observe`; the client is written
> but disabled until sandbox credentials exist).

## Repository

| Path | What |
|---|---|
| `contracts/` | Foundry project: `src/HskPayPerRequest.sol`, unit and fuzz tests, model-based invariant suite, gas test |
| `contracts/test/fixtures/` | runtime code of USDC.e's implementation and its signature library, read from HashKey Chain (hash-pinned) |
| `contracts/DEPENDENCIES.md` | provenance of the vendored OpenZeppelin 5.1.0 and forge-std v1.16.2 (`verify_deps.sh` re-checks them) |
| `server/` | `seller.py` (x402 v2 seller, settlement, refunds), `protocol.py`, `chain.py`, `hsp_draft.py`, `state.py` (journal), `egress.py` (egress guard) |
| `server/test_*.py`, `server/testkit.py` | protocol tests, security regression tests on an offline chain model, live hostile-input tests |
| `agent/buyer.py` | the paying client with its spend guard |
| `deploy/` | `run_sandboxed.sh` (bubblewrap sandbox for the seller) and `sandbox_probe.py` (what the sandbox can reach) |
| `ops/state/` | the mainnet deployment record and the public addresses of the demo roles |
| `docs/` | the live dashboard (static page on GitHub Pages; `snapshot.json` is its fallback when the explorer API is down) |
| `tools/` | `dashboard_snapshot.py` (refreshes the fallback snapshot) and `test_dashboard.js` (decoder and counter tests) |

## Run the tests

```sh
pip install -r requirements.txt
cd contracts && forge test && cd ..   # 55 tests in 3 suites: unit + fuzz, gas, model-based invariants (256 runs x 200 calls)
python3 server/test_protocol.py       # protocol, typed data, HSP verifier, payload validation (offline)
python3 server/test_security.py       # 39 security regression tests against an offline chain model
python3 agent/test_buyer.py           # the buyer's spend guard
node tools/test_dashboard.js          # dashboard decoder: only endpoints 1 and 2 of our provider count, hostile logs dropped
HSK_METER_BUYER_KEY_FILE=buyer.env python3 server/test_protocol.py --online   # adds read-only checks against the live contract
HSK_METER_BUYER_KEY_FILE=buyer.env python3 server/test_live.py                # hostile-input tests against a running seller (pays nothing)
```

## License

MIT
