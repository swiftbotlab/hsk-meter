# HSP Agentic Payment client (official QA MCP)

HashKey published the **HSP Agentic Payment** QA server for hackathon teams on 2026-10-30
(`https://mcp-qa.hashkeymerchant.com/`, MCP over streamable HTTP). This folder is our payer for it, written from the
published guide: `get_chain_config` -> `create_order` (Cart Mandate) -> `prepare_payment` -> EIP-712 signature ->
`finalize_payment` -> `submit_payment` -> `query_payment` until `payment-safe` / `payment-finalized`, with
`attribution_verified` checked under one fixed team name.

**Status (2026-10-30):** built and tested against a local mock of the documented tools (84 offline tests, also in CI).
QA access is IP-allowlisted by HSP: our access request was filed on 2026-10-30 and we are waiting for HSP's confirmation.
**No payment has run on the live QA server yet**; receipts will be added here when it has.

## What it adds: a signing firewall
The guide has a person look at the recipient, payer and amount before the wallet signs whatever EIP-712 data the server
returns. `firewall.py` does that check in code, against our own order and the token from `get_chain_config`, and refuses
to sign when any of these differ: primary type, chain id, token contract, payer, recipient (or Permit2 spender), amount
(`token_total_raw`, the order amount and a per-payment cap), validity window, or the x402 payment payload. The tests
include 33 tampered payloads that must be refused, each with a positive control, and 14 guards were mutation-checked
(removing any one of them makes a test fail). Spend caps: 1 USD per payment, 5 USD in total, counted from `receipts.jsonl`.

## Run
```bash
python3 -m pip install -r requirements.txt -c constraints.txt   # eth-account, from the repository root
hsp_qa/run_tests.sh                                              # offline, mock server on 127.0.0.1 only
python3 hsp_qa/pay.py pay --mock --yes                           # full flow against the mock
python3 hsp_qa/pay.py health                                     # is our IP allowlisted yet?
python3 hsp_qa/pay.py pay --dry                                  # live: stops before signing
```
Keys for the QA payer and recipient are generated locally into `$HSP_QA_SECRETS_DIR` (default `~/.hsk-meter/hsp_qa`,
mode 600) and are testnet-only.
