# HSK Meter

Pay-per-request APIs for software clients on **HashKey Chain**. A seller quotes every request with an
**x402 v2** `402 Payment Required`, the buyer answers with a signed **EIP-3009** authorization for exactly the
price in **USDC.e**, and an on-chain contract settles it, records a receipt and can refund it.
The buyer needs no gas token at all.

By Swift Bot Labs. Built for the HashKey Chain Horizon Hackathon (India), track "Agent Payments & A2A Settlement".

## Live on HashKey Chain mainnet (chain id 177)

| What | Where |
|---|---|
| Settlement contract `HskPayPerRequest` | [`0x78C8476772BC209C3df09Da5e129d0879cd57F8c`](https://hsk.blockscout.com/address/0x78C8476772BC209C3df09Da5e129d0879cd57F8c?tab=contract) |
| Source verification | Blockscout (full match) and [Sourcify](https://repo.sourcify.dev/177/0x78C8476772BC209C3df09Da5e129d0879cd57F8c) (exact match, creation + runtime) |
| Settlement token | USDC.e [`0x054ed45810DbBAb8B27668922D110669c9D88D0a`](https://hsk.blockscout.com/token/0x054ed45810DbBAb8B27668922D110669c9D88D0a) (Circle FiatTokenV2_2, EIP-3009) |

Prices (set on-chain per endpoint): address profile **0.03 USDC.e**, transaction check **0.025 USDC.e**. Each price
covers at least twice the worst settlement gas we have measured (246,503 gas at 500 gwei = 0.123 HSK, about $0.011),
because the seller pays the gas of every `exact` settlement. Price changes:
[`0x62188d0a…`](https://hsk.blockscout.com/tx/0x62188d0abc975d81fd2212ea2197dd2d990fd2be3a168a48b7ba995a58980ef4) and
[`0x86e73f35…`](https://hsk.blockscout.com/tx/0x86e73f35a5ebb00efc93b8cc449838b812ec601ccb336e904c99530832b6576c).

Real paid requests, all made over HTTP through the seller in this repo (1-5 with v0.1 at the launch prices, 6-8 with
the hardened v0.2 running in its sandbox):

| # | Request | Payment path | Transaction |
|---|---|---|---|
| 1 | address profile, 0.01 USDC.e | x402 `exact` + HSP-draft mandate, buyer paid no gas, verifier ACCEPT | [`0x98526a6b…`](https://hsk.blockscout.com/tx/0x98526a6b276b3763cde68297c29f89f194a5fed99a0add55794f57ece123da79) |
| 2 | tx check (of #1), 0.005 | x402 `exact` | [`0x672b4957…`](https://hsk.blockscout.com/tx/0x672b495773c17c6216ec24d7981091d2209d43e545e17b463a7c9744d830dfc5) |
| 3 | tx check of a hash that does not exist, 0.005 | x402 `exact`, then automatic **refund** | paid [`0x63d7f42d…`](https://hsk.blockscout.com/tx/0x63d7f42d638b79fb1a16404ca025a1524c6c635f601c6f99d8c0df611f4a0f52), refunded [`0xaa744ebd…`](https://hsk.blockscout.com/tx/0xaa744ebdacf908b511dd5e23cc592cd49c55e9f466012a0b26e3388e909c40fc) |
| 4 | address profile, 0.01 | buyer calls `pay()` itself, proves it with the tx hash | [`0xfb36cf54…`](https://hsk.blockscout.com/tx/0xfb36cf54ef11efccca707b465ac9ff95dda1f075eab59136485369ce76783856) |
| 5 | tx check, 0.005 | x402 `exact`; same header replayed 3 more times: served 3x, settled once, then refused | [`0x04ddbc59…`](https://hsk.blockscout.com/tx/0x04ddbc5973a69cd5620f3eb516b6ddbc6be2be4630efeda507fa3ca0c181917e) |
| 6 | address profile, 0.03 | x402 `exact` + HSP-draft mandate, verifier ACCEPT, adapter pinned | [`0x5bf9515b…`](https://hsk.blockscout.com/tx/0x5bf9515b09692b156cfbe080b451ab0a8a74e2a2e41e2bb8d93aecc0f770a7c9) |
| 7 | tx check, 0.025 | x402 `exact` | [`0x3a88578a…`](https://hsk.blockscout.com/tx/0x3a88578a594d3e16d493c68fd168e20e0a750d92ce16bd7f216721877f7147ca) |
| 8 | address profile, 0.03 | x402 `exact`; delivery failed (operator test switch), automatic **refund** | paid [`0x543e14fb…`](https://hsk.blockscout.com/tx/0x543e14fbf0aa9e15932405e70df912bca1d0b1da40be8b33ad1d4b88e557f9b5), refunded [`0xd6e4e725…`](https://hsk.blockscout.com/tx/0xd6e4e7257aa7a472566dc0a27712b58a23b556a9ddb39e93504f582707fdfb0c) |

Since v0.2 a transaction check for a hash that is not on chain is answered with a free `404` before any payment (row 3
shows the v0.1 behaviour); refunds now cover deliveries that fail after settlement.

Deployment [`0xe08891e3…`](https://hsk.blockscout.com/tx/0xe08891e35a7197a62a5cb165a6e2dea194842f3dba00711fc65207cec120fe73),
endpoint registrations [`0xec7dd370…`](https://hsk.blockscout.com/tx/0xec7dd370d753d746a264e3bd13acdca1733b323132e5cbe5184a573ec56f27b4)
and [`0x97e3dd2d…`](https://hsk.blockscout.com/tx/0x97e3dd2d6b5cc86a0a81231f84deaf6ae706bbbcf6a2bf916c3750f170464a82),
payee withdrawal [`0x159f8304…`](https://hsk.blockscout.com/tx/0x159f830494dd86272b9e2ba53d9801a5b4a3c190f38a5ef46a54ccf36e67bbe8).

## How a request is paid

```
buyer                                   seller (server/seller.py)                     HashKey Chain
  | GET /v1/hsk/address-profile?...        |                                               |
  |   X-Payer: 0xBuyer  ------------------->| reads the price from the contract ----------->|
  |<-- 402 + PAYMENT-REQUIRED (x402 v2) ----|  requestId bound to the buyer, nonce,          |
  |                                         |  optional HSP-draft mandate                    |
  | signs ReceiveWithAuthorization          |                                               |
  | (USDC.e, to = contract, value = price,  |                                               |
  |  nonce = authorizationNonce(id, rid))   |                                               |
  | GET again + PAYMENT-SIGNATURE --------->| checks the signature locally, the payer        |
  |                                         | on-chain (plain account, balance, unused       |
  |                                         | nonce), simulates the exact transaction, then  |
  |                                         | payWithAuthorization(...) ------------------->| Paid event, payee credited
  |                                         | reads the Paid event, does the paid work       |
  |<-- 200 + data + PAYMENT-RESPONSE -------|  (no result? refund(requestId) on-chain) ---->| Refunded event
  | verifies the Paid / Refunded event      |                                               |
```

The contract (`contracts/src/HskPayPerRequest.sol`, no admin, no upgrades, nothing payable):

- Providers register priced endpoints; a payee must consent to the provider that pays it.
- `payWithAuthorization` pulls exactly the price with USDC.e `receiveWithAuthorization`. The EIP-3009 nonce must equal
  `authorizationNonce(endpointId, requestId) = keccak256(abi.encode(contract, endpointId, requestId))`, so one signature pays
  one request on one endpoint, and nothing else. Only the contract can execute a `ReceiveWithAuthorization`, so a copied
  signature cannot be front-run into a bare transfer.
- `pay(endpointId, requestId, amount)` is the same payment for buyers that approve and pay themselves.
- A requestId carries its payer in the top 20 bytes, so nobody else can pay (or burn) it. A requestId is paid at most once.
- Price changes apply to new payments only; a stale quote gets `WrongAmount`, never a different charge.
- The provider can refund a paid request once, out of the payee's unwithdrawn credit. Payees pull their credit with `withdraw`.

## x402 v2 profile

Standard: the `PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` / `PAYMENT-RESPONSE` headers (base64 JSON), `x402Version: 2`,
`accepts[]` with `scheme: "exact"`, `network: "eip155:177"`, `amount`, `asset`, `payTo`, `maxTimeoutSeconds`, and a
`PaymentPayload` of `{signature, authorization: {from, to, value, validAfter, validBefore, nonce}}`.

Specific to this rail (declared in `accepts[0].extra`): the signed type is `ReceiveWithAuthorization`
(`primaryType`, `settlement: "contract"`), `payTo` is the settlement contract, and the nonce is assigned by the quote
(`extra.nonce`) because it binds the payment to one request. A client that signs a random-nonce `TransferWithAuthorization`
is refused before anything is sent. `accepts[1]` (`hsk-paywall-tx`) is the self-paid alternative.

## HSP (HashKey Settlement Protocol): what is implemented, and what is not yet

The public HSP developer guide describes the model: a payer-signed EIP-712 **mandate**, a **receipt** signed by a settlement
**adapter**, optional **attestations**, and one verifier rule (ACCEPT iff required capabilities are a subset of satisfied
capabilities; outcome classes ACCEPT / RETRYABLE / POLICY / PERMANENT; paymentId = mandate hash). It does not yet publish the
EIP-712 type definitions or the SDK, and the sandbox Coordinator was not online when this was built.

Implemented (`server/hsp_draft.py`, labeled `hsp-draft/swiftbotlab-0`):

- Our own EIP-712 encoding of the documented mandate fields, bound to the paywall request (`reference = requestId`).
- An adapter for this contract rail: it signs a receipt only after the `Paid` event is observed on HashKey Chain.
- The verifier (signatures, mandate/receipt match, deadline, on-chain re-check, the subset rule). The seller ships only on
  ACCEPT; the buyer re-runs the verifier with the adapter address pinned out-of-band. Request #1 above used this path.

Not yet (marked `TODO(hsp-...)` in the code):

- Swap our draft types for the official `@hsp/core` types once the SDK repository is shared.
- Run the `@hsp/devkit` conformance suite against this adapter and register it with the organizer.
- Register mandates and observations with the hosted Coordinator (`POST /payments`, `/payments/:id/observe`); the client is
  written but disabled until sandbox credentials exist.

## Repository

| Path | What |
|---|---|
| `contracts/` | Foundry project: `src/HskPayPerRequest.sol`, unit + fuzz tests, model-based invariant suite, gas test |
| `contracts/test/fixtures/` | runtime code of USDC.e's implementation and its signature library, read from HashKey Chain (hash-pinned) |
| `contracts/DEPENDENCIES.md` | provenance of the vendored OpenZeppelin 5.1.0 and forge-std v1.16.2 (`verify_deps.sh` re-checks them) |
| `server/` | `seller.py` (x402 v2 seller + settlement + refunds), `protocol.py`, `chain.py`, `hsp_draft.py`, `state.py` (journal), `egress.py` (egress guard) |
| `server/test_*.py`, `server/testkit.py` | protocol tests, security regression tests (offline chain model), live hostile-input tests |
| `agent/buyer.py` | the paying client with a spend guard (pinned provider and payee, cap reserved before signing); verifies every claim on-chain |
| `deploy/` | `run_sandboxed.sh` (bubblewrap sandbox for the seller) and `sandbox_probe.py` (what the sandbox can reach) |
| `ops/state/` | the mainnet deployment record and the public addresses of the demo roles (provider, payee, buyer) |

## Run the tests

```sh
pip install -r requirements.txt       # pinned: eth-account, eth-abi, eth-utils
cd contracts && forge test            # 55 tests in 3 suites: unit + fuzz, gas, model-based invariants (256 runs x 200 calls)
python3 server/test_protocol.py       # protocol, typed data, HSP draft verifier, payload validation (12 offline)
python3 server/test_security.py       # 38 security regression tests against an offline chain model (no network)
python3 agent/test_buyer.py           # the buyer's spend guard (7 tests)
HSK_METER_BUYER_KEY_FILE=... python3 server/test_protocol.py --online   # + read-only checks against the contract
HSK_METER_BUYER_KEY_FILE=... python3 server/test_live.py   # hostile-input tests against a running seller (nothing is paid)
```

The contract tests run against the **real** USDC.e implementation code (`FiatTokenV2_2`) loaded from fixtures, so token
rules (EIP-3009 checks, blacklist, pause, allowances) are the production ones. A mutation check (8 hand-written mutants of
the contract) was killed by the unit suite.

## Run the seller and pay with the buyer

The seller reads its keys from a directory with three files, one value each, mode 0600: `provider.env`
(`HKPW_PROVIDER_PK`), `adapter.env` (`HKPW_ADAPTER_PK`) and `token.env` (`PAYWALL_TOKEN_KEY`, 32 random bytes in hex).
It keeps its journal in a state directory (default `./run`).

```sh
# in a bubblewrap sandbox (recommended): only server/, the venv, the key dir (read-only) and the state dir are visible
HSK_METER_KEY_DIR=/path/to/keys HSK_METER_VENV=/path/to/venv deploy/run_sandboxed.sh seller --port 8402
# or directly
python3 server/seller.py --key-dir /path/to/keys --port 8402

python3 agent/buyer.py "http://127.0.0.1:8402/v1/hsk/address-profile?address=0x78C8476772BC209C3df09Da5e129d0879cd57F8c" \
  --key-file /path/to/buyer.env \
  --expect-provider 0x30CCc6d816854d1a66fBfecE15b1515a3E941160 --expect-payee 0xA8A49A6Ca872D54a8FD5FB121139De9bff62a48D \
  --hsp --pin-adapter 0x4D281e0a98fD40Bb824F88D5e037bd8d03C4403a
```

Security notes: [SECURITY.md](SECURITY.md).

## License

MIT
