# HSP gateway integration: status and open questions

Status as of 2026-10-29. HSK Meter's HashKey Merchant (HSP) gateway rail is built from the official API documentation
([hashkey-merchant.github.io](https://hashkey-merchant.github.io/en/index.html): onboarding, authentication,
api-reference, cart-mandate, webhook, appendix) and checked against the official SDKs
([hashkey-merchant/sdks](https://github.com/hashkey-merchant/sdks), commit `4ea5f61`) where the docs are silent.
It switches on as soon as merchant credentials exist. **Nothing here has run against a live HSP gateway yet**: merchant
credentials have been requested and not issued, so every test below runs against our local mock gateway.

## What is implemented and tested

| Part | Where | Tested against |
|---|---|---|
| HMAC-SHA256 request signing (`X-App-Key`, `X-Signature`, `X-Timestamp`, `X-Nonce`; canonical-JSON body hash) | `server/hsp_gateway.py` | the docs' message examples; the docs' own bash/openssl pipeline (same signature, wrong secret differs) |
| ES256K `merchant_authorization` JWT (`iss`, `sub`, `aud`, `iat`, `exp`, `jti`, `cart_hash`), SEC1 and PKCS8 keys | `server/hsp_gateway.py` | openssl verifies our signatures and rejects a tampered message; openssl-generated SEC1/PKCS8 keys parse to the same key; P-256 keys and off-curve points are refused |
| Canonical JSON (sorted keys, compact) | `server/hsp_gateway.py` | both examples in the docs, byte for byte |
| Cart Mandate builder and validator (amounts, modifiers, `method_data_indexes`, USD only, x402 v2) | `server/hsp_gateway.py` | the docs' examples (effective totals 16.00, and 10.00 / 11.00); 12 documented violations refused |
| One-time and reusable orders, payment queries (`/merchant/orders`, `/orders/reusable`, `/payments`, `/payments/reusable`) | `server/hsp_gateway.py` (`Gateway`) | local mock gateway with the documented request/response shapes and error codes (10001, 10002, 10003, 40001, 40002) |
| Webhook receiver (`X-Signature: t=,v1=`, 300 s tolerance, constant-time compare) with an idempotent journal | `server/hsp_gateway.py`, `server/hsp_merchant_agent.py` | forged, tampered, stale and replayed deliveries; retries and duplicate deliveries change nothing |
| Receiving agent: a priced HSK Meter request becomes a Cart Mandate (one item = the exact resource, total = the endpoint price in USD, one USDC row per network) | `server/hsp_merchant_agent.py` | end to end over HTTP; serves only after a confirmed payment by the same payer, at most 3 times |
| Payment agent: verifies the receiving agent's conditions, signs the Payment Mandate (EIP-3009 `TransferWithAuthorization`, EIP-712) in the hosted checkout's wire format, submits it to `POST /api/v1/payment/pay-mandate`, polls the flow, then fetches the paid resource | `agent/hsp_payer.py` | tampered carts, swapped payees, unpinned merchant keys, unpinned gateways, a gateway copy of the order that differs from the offer, over-limit amounts, budget, mainnet and wrong-resource offers are all refused before anything is signed; a retry after a lost answer pays once |
| Payer wire format (see below): `payment_payload` (base64 x402 v2), `user_authorization` (HS256), `transaction_data` | `server/hsp_gateway.py`, `agent/hsp_payer.py` | field order and values checked against the checkout code; recomputed independently with stdlib hmac/hashlib, openssl and a Node.js re-implementation of the checkout's `sortKeys` + `JSON.stringify` + `btoa` + HS256; the mock refuses 15 tampered variants and accepts the untampered one |
| Mock gateway (tests only), merchant and payer sides | `server/hsp_mockgw.py` | `python3 server/hsp_mockgw.py demo` runs the whole flow offline |

Tests: `python3 server/test_hsp_gateway.py` (43 tests, offline; they also run in CI). Thirteen guards were
mutation-checked (six on 2026-10-27, seven for the payer wire format on 2026-10-29): removing any of them makes the
suite fail.

Supported for payment today: USDC (EIP-3009) on `hashkey-testnet` (133) and `base-sepolia` (84532). The token
EIP-712 domains were read on-chain and checked against `DOMAIN_SEPARATOR()`: hashkey-testnet USDC is
`("USD Coin", "2")`, base-sepolia USDC is `("USDC", "2")`, and HashKey Chain mainnet USDC is `("Bridged USDC", "2")`.

## Payer wire format (unverified against a live gateway)

The docs publish the merchant API only. For the payer side they give two route names in the architecture diagram
(`/api/v1/payment/ pay-mandate (submit)` and `flow/:id (status)`) and the ID table (`payment_mandate_id` = ID4 = ID2,
`request_id` = ID5, made by the frontend). The bodies below come from **public prior art**: the repository of an
earlier HashKey hackathon project ([github.com/skymiss18/hashkeyhackthon](https://github.com/skymiss18/hashkeyhackthon),
commit `2f2c682`). It contains a saved copy of the hosted checkout's client bundle (`backend/checkout_bundle.js`), an
agent payer (`backend/src/lib/walletPay.js`) and a test script (`backend/test-fullpay.js`). We read those files as
text on 2026-10-29, did not run them, copied no code, and wrote our own implementation. Where they disagree we follow
the checkout bundle, because it is the gateway's own client.

| Field | What we send | Source |
|---|---|---|
| Route | `POST /api/v1/payment/pay-mandate` (reusable orders: `/pay-mandate/multi-pay`), JSON, no auth headers | bundle; docs diagram names the route |
| Body | `{flow_id, payment_mandate: {payment_mandate_contents, user_authorization}}` | bundle, walletPay.js |
| `payment_mandate_id`, `payment_response.request_id` | the cart's `payment_request_id` | bundle; docs ID table |
| `payment_details_id` (ID5) | 14-digit timestamp + 6 random digits (we use UTC; the bundle uses local time) | bundle |
| `payment_details_total` | `{label, amount: {value, currency}, pending}` copied from the cart's `details.total` | bundle (walletPay.js rounds the value with `toFixed(2)`; we do not) |
| `payment_response.method_name` | the chosen `method_data` row's `supported_methods` | bundle |
| `payment_response.details` | `{payment_payload}` only; `shipping_address`, `shipping_option`, `payer_name`, `payer_email`, `payer_phone` = `null` | bundle, walletPay.js |
| `payment_payload` | standard base64 of the compact JSON of an x402 v2 PaymentPayload: `{x402Version: 2, payload: {signature, authorization}, accepted: {scheme: "exact", network: "eip155:<chainId>", asset, amount (base units), payTo, maxTimeoutSeconds: 3600, extra: {protocol: "eip3009", name, version}}, resource: {url, description, mimeType}}` | bundle (`btoa(JSON.stringify(...))`); test-fullpay.js puts the method URL in `extra.protocol`, the bundle and walletPay.js put `eip3009` |
| `authorization` | EIP-3009 `{from, to, value, validAfter: now, validBefore: now + 300, nonce: 0x + 32 random bytes}` | bundle (test-fullpay.js uses `validAfter: 0`) |
| `resource` | the paid URL and resource string | ours: the bundle sends a fixed example resource, so the gateway is unlikely to check it |
| `merchant_agent` | `"HashkeyMerchant"` | bundle, walletPay.js |
| `timestamp` | `new Date().toISOString()` form, with milliseconds | bundle |
| `user_authorization` | HS256 JWT, header `{"alg":"HS256"}`, key = UTF-8 bytes of the payer address as `iss`; claims `{aud: "HashkeyMerchant", nonce: 0x + 32 random bytes, iat, exp: cart_expiry, iss: payer address, transaction_data}` | bundle (jose `SignJWT`), walletPay.js |
| `transaction_data` | `[sha256(canonical(cart contents)), sha256(canonical(payment_mandate_contents))]`, hex | bundle and walletPay.js hash the contents; see the conflict below |

Read routes used by the payer, from the bundle: `GET /api/v1/payment/cart-mandate/flow/{flow_id}` (the order as the
checkout reads it: `cartmandate`, `multi_pay`; we fall back to the Go SDK's `GET /api/v1/checkout/orders/{flow_id}`)
and `GET /api/v1/payment/flow/{flow_id}?payment_details_id=...` (status). The bundle also maps gateway error codes
30001 (invalid authorization), 30043 (quote updated, retry), 10001 and 10005; the payer reports them by name.

**Conflict: what `transaction_data[0]` hashes.** The checkout bundle hashes the cart's `contents` (its `Vge` is called
with `order_detail.cartmandate.contents`), and so does walletPay.js. That value equals the `cart_hash` in the
merchant's JWT. test-fullpay.js, and a comment in the same project's `agent.js`, say the hash must cover the full
cart mandate `{contents, merchant_authorization}`, which its code then does not do. The docs say nothing. We send the
contents hash by default and keep the other as a switch (`--cart-hash-scope cart_mandate`); the mock gateway can
require either (`cart_hash_scope`), and a test shows that a mismatch is refused (30001) and a match pays.

**Docs vs prior art.** No field above contradicts the docs; the docs are silent on all of them. The JWT audience
`HashkeyMerchant` is the docs' spelling for `merchant_authorization`, and the checkout uses the same for
`user_authorization`. The docs' example `payment_url` is `https://pay.hashkey.com/flow/xxx`; the prior art saw QA
URLs ending in `/checkout/<uuid>`. We take the flow id from the last path segment, which covers both; if the QA
checkout is on another host than the API, the payer needs `--checkout-host <host>`.

**Security note.** The HS256 key is the payer's public address, so `user_authorization` binds the two hashes but
authenticates nobody. The wallet's EIP-3009 signature is what authorizes the transfer.

## What waits on HashKey

1. **Credentials.** `app_key` / `app_secret` from the merchant console, for the organization registered with our
   public key (`hsp_merchant_public_key.pem`), on hashkey-testnet and base-sepolia USDC. Requested, not issued yet.
2. **Network access to QA.** `merchant-qa.hashkeymerchant.com` and `merchant-stg.hashkeymerchant.com` answer our
   network with a Cloudflare block page ("Sorry, you have been blocked"), from a plain HTTP client and from a regular
   browser alike. We need our address allowlisted, or another route to the QA gateway.
3. **Confirmation of the payer wire format.** It is built from the checkout's client code, not from documentation, and
   has only run against our mock. One live payment on QA would settle it, including the conflict above.

## Open questions for HashKey operations

1. Is the checkout's `POST /api/v1/payment/pay-mandate` (no auth, `{flow_id, payment_mandate}`) the supported way for
   a payment agent (not a browser) to submit a Payment Mandate, or is an agent-facing route planned?
2. `user_authorization.transaction_data[0]`: the hash of the cart's `contents`, or of the whole cart mandate including
   `merchant_authorization`? (The checkout code hashes `contents`.)
3. Is the HS256 `user_authorization` keyed by the payer address the intended format, or will it become a signature
   by the wallet (AP2 describes "a verifiable presentation ... over the cart_mandate and payment_mandate hashes")?
4. JWT `aud`: the docs and the Go SDK say `HashkeyMerchant`; the Python and Node SDKs send `HashKeyMerchant`. Which
   one does the gateway check? (We send the documented one; `JWT_AUDIENCE` changes it.)
5. Canonical JSON: is the body hash computed over the raw request bytes (as the SDKs do) or over a re-canonicalized
   body (as the docs say)? We send canonical bytes, so both readings give the same hash, but which one is it? Note that
   Go's `json.Marshal` escapes `<`, `>` and `&`, unlike the other SDKs; we keep those characters out of signed fields.
6. For USD-denominated carts paid in USDC, which rate applies (`usd_amount_rate`), and are network or service fees
   added to `pay_amount` paid by the payer? (Our payer accepts the gateway's amount only up to a configured fee
   ceiling, `--max-fee-usd`, default 0.)
7. Is a per-order `webhook_url` in the create-order body supported (the Python SDK sends one), or only the
   per-application setting in the console?
8. Is there a refund or cancellation API for paid orders? None is documented. Until there is one, a paid order whose
   data cannot be delivered stays re-servable (the receiving agent never charges twice and never drops a paid order).
9. Base Sepolia USDC's EIP-712 name on-chain is `USDC`, not `USD Coin` as in the SDK constants. The checkout takes the
   name from `/api/v1/payment/chain-config`; does that config say `USDC` for Base Sepolia?
10. Is there a sandbox faucet for hashkey-testnet USDC (`0x8FE3cB71...`), or should we bridge testnet USDC ourselves?

## How to run the live test once credentials arrive

```sh
# 1. fill APP_KEY, APP_SECRET, BASE_URL=https://merchant-qa.hashkeymerchant.com in secrets/hsp/hsp.env (mode 0600)
python3 server/hsp_gateway.py status                                   # HSP on; merchant key matches the public PEM
python3 server/hsp_gateway.py create-order --usd 0.01 --network hashkey-testnet
python3 server/hsp_gateway.py query --payment-request-id <id from the line above>
python3 server/hsp_gateway.py checkout --flow-id <flow id>             # the public checkout read
python3 server/hsp_gateway.py create-order --usd 0.01 --network base-sepolia --reusable
# 2. receiving agent + payment agent (payer key: a testnet wallet with test USDC)
python3 server/hsp_merchant_agent.py --port 8403 --verify-onchain
python3 agent/hsp_payer.py "http://127.0.0.1:8403/v1/hsk/address-profile?address=0x78C8476772BC209C3df09Da5e129d0879cd57F8c" \
  --key-file payer.env --pin-merchant-key hsp_merchant_public_key.pem \
  --expect-payee 0xA8A49A6Ca872D54a8FD5FB121139De9bff62a48D --max-usd 0.05 --budget-usd 0.20 \
  [--sign-only]                                                        # first run: sign and store, submit nothing
  [--cart-hash-scope cart_mandate]                                     # only if the gateway answers 30001
```

If the submission is refused, the same order can still be paid from a testnet wallet at the hosted `payment_url`
checkout; the receiving agent verifies that payment the same way (merchant API, webhook, chain) and serves it to
`GET <url>` with `X-Payer: <that wallet>`, `X-HSP-Flow: <flow id>` and `X-HSP-Proof` (that wallet's EIP-191
signature over the delivery message given in the 402).
