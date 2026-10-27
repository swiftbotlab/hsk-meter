# HSP gateway integration: status and open questions

Status as of 2026-10-27. HSK Meter's HashKey Merchant (HSP) gateway rail is built from the official API documentation
([hashkey-merchant.github.io](https://hashkey-merchant.github.io/en/index.html): onboarding, authentication,
api-reference, cart-mandate, webhook, appendix) and checked against the official SDKs
([hashkey-merchant/sdks](https://github.com/hashkey-merchant/sdks), commit `4ea5f61`) where the docs are silent.
It switches on as soon as merchant credentials exist.

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
| Payment agent: verifies the receiving agent's conditions, signs the Payment Mandate (EIP-3009 `TransferWithAuthorization`, EIP-712), submits, polls, then fetches the paid resource | `agent/hsp_payer.py` | tampered carts, swapped payees, unpinned merchant keys, unpinned gateways, over-limit amounts, budget, mainnet and wrong-resource offers are all refused before anything is signed |
| Mock gateway (tests only) | `server/hsp_mockgw.py` | `python3 server/hsp_mockgw.py demo` runs the whole flow offline |

Tests: `python3 server/test_hsp_gateway.py` (37 tests, offline). Six guards were mutation-checked: removing any of them
makes the suite fail.

Supported for payment today: USDC (EIP-3009) on `hashkey-testnet` (133) and `base-sepolia` (84532). The token
EIP-712 domains were read on-chain and checked against `DOMAIN_SEPARATOR()`: hashkey-testnet USDC is
`("USD Coin", "2")`, base-sepolia USDC is `("USDC", "2")`, and HashKey Chain mainnet USDC is `("Bridged USDC", "2")`.

## What waits on HashKey

1. **Credentials.** `app_key` / `app_secret` from the merchant console, for the organization registered with our
   public key (`hsp_merchant_public_key.pem`), on hashkey-testnet and base-sepolia USDC.
2. **Network access to QA.** `merchant-qa.hashkeymerchant.com` and `merchant-stg.hashkeymerchant.com` answer our
   network with a Cloudflare block page ("Sorry, you have been blocked"), from a plain HTTP client and from a regular
   browser alike. We need our address allowlisted, or another route to the QA gateway.
3. **The payer-side submission API (the main gap).** The docs publish the merchant API only. The checkout's own calls
   appear only in the architecture diagram (`/api/v1/payment/ pay-mandate (submit)` and `flow/:id (status)`), with
   no request body, response or authentication. The official SDKs add the checkout read
   `GET /api/v1/checkout/orders/{flow_id}` and the types `AP2PaymentMandate`, `SubmitPaymentMandateResponse`,
   `GetPaymentByFlowIDResponse`, but no submission route. Our payment agent builds the `AP2PaymentMandate` in the
   SDK's shape and is ready to submit it, but against a real gateway it only signs and stores the mandate until the
   route is confirmed (`--submit-path`).

## Open questions for HashKey operations

1. Is there an agent-facing (non-browser) endpoint for submitting a Payment Mandate? If yes: its path (is it
   `POST /api/v1/payment/pay-mandate`?), authentication (none, a flow token, or HMAC?), and request body
   (`{flow_id, payment_mandate}`?).
2. Inside `payment_mandate_contents.payment_response.details`, where do the EIP-3009 authorization and its signature
   go, and in which encoding (x402 v2 `PaymentPayload`?). Is `authorization.to` the cart's `pay_to`, and is
   `nonce` chosen by the payer?
3. What does the gateway expect in `user_authorization`, if anything (AP2 describes "a verifiable presentation ...
   over the cart_mandate and payment_mandate hashes")? Ours is an EIP-712 signature by the payer over both hashes
   and the flow id, base64url-encoded; we can change it or leave it out.
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
9. Base Sepolia USDC's EIP-712 name on-chain is `USDC`, not `USD Coin` as in the SDK constants. Does the checkout
   read the name from the chain? (A wallet signing with the SDK constant would produce an invalid authorization.)
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
  [--submit-path /api/v1/payment/pay-mandate]                          # only once HashKey confirms the route
```

Until the submission route is confirmed, the same order can be paid from a testnet wallet at the hosted
`payment_url` checkout; the receiving agent verifies that payment the same way (merchant API, webhook, chain) and
serves it to `GET <url>` with `X-Payer: <that wallet>`, `X-HSP-Flow: <flow id>` and `X-HSP-Proof` (that wallet's
EIP-191 signature over the delivery message given in the 402).
