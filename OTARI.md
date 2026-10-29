# HSK Meter + Otari: a paid AI endpoint that refunds itself

**An agent pays 0.03 USDC.e on HashKey Chain for one question. [Otari](https://otari.ai) routes the answer across four
open-weight models and enforces that buyer's AI budget. When Otari says no, the payment comes back on-chain,
automatically.**

HSK Meter is our pay-per-call API for AI agents: each request is paid in USDC.e and settled on-chain per request
through the [`HskPayPerRequest`](https://hsk.blockscout.com/address/0x78C8476772BC209C3df09Da5e129d0879cd57F8c)
contract, and a payment the seller cannot deliver on is refunded on-chain. This branch (`otari-gateway`) adds
`GET /v1/ai/ask?q=<question>`, endpoint 4 on that contract, with every model call going through the hosted Otari
gateway (`https://otari.ai/api/v1`, OpenAI-compatible).

| | |
|---|---|
| Branch | [`otari-gateway`](https://github.com/swiftbotlab/hsk-meter/tree/otari-gateway) (main is the HashKey hackathon entry and stays untouched) |
| First commit where Otari came in | [`e99914c`](https://github.com/swiftbotlab/hsk-meter/commit/e99914c6d95676de05f4794fa9a433c7b1d4fecb) Otari gateway client |
| Code | [`server/otari.py`](server/otari.py) (client, key ring), [`server/ai_ask.py`](server/ai_ask.py) (the endpoint), hooks in [`server/seller.py`](server/seller.py) |
| Otari configuration | [`otari/config.json`](otari/config.json): keys, budgets, ceilings, routing policies, MCP server, web search |
| Live proof | [`otari/live_runs.json`](otari/live_runs.json): 10 paid calls on mainnet (3 refunded) and 3 refusals before payment, with transactions |
| Demo video | [`otari/otari-demo.mp4`](otari/otari-demo.mp4) (4:24, captions and voice-over), script in [`otari/VIDEO_SCRIPT.md`](otari/VIDEO_SCRIPT.md) |
| Tests | [`server/test_ai.py`](server/test_ai.py): 28 offline tests (scripted gateway + loopback HTTP client), in CI |
| Feedback | [`OTARI_FEEDBACK.md`](OTARI_FEEDBACK.md) |

## What happens to one paid question

```
agent (buyer.py / MCP)            HSK Meter seller                         Otari (otari.ai)
  GET /v1/ai/ask?q=...  ───────▶  screen before quoting  ── screening key ──▶ hsk-guard   Qwen3Guard-Gen 8B
                                                          ── screening key ──▶ hsk-route   Ministral 3 3B
  ◀── 402 + price (or 422/429: refused, nothing charged)
  signed EIP-3009 authorization ▶ settle 0.03 USDC.e on HashKey Chain
                                  read the chain itself (address, tx, network constants)
                                  answer                   ── buyer's key ────▶ hsk-answer  gpt-oss-120b
                                                              + Blockscout MCP, web search / code execution if routed
                                  check                    ── buyer's key ────▶ hsk-verify  Ministral 3 8B
  ◀── 200 answer + Otari receipt (models, providers, fallback, cost per call)
  ◀── or: on-chain refund when Otari refuses (budget_exceeded), the chain of providers fails, or the check fails
```

1. **Before any payment** the seller's screening key asks `hsk-guard` (Qwen3Guard-Gen 8B, falling back to 0.6B) whether
   the question is safe and `hsk-route` (Ministral 3 3B, structured output) whether it is in scope and which paid tools
   it needs. A flagged or off-topic question gets a `422` and is never quoted: nothing is signed or charged.
2. The buyer signs the quoted authorization and the seller settles it on-chain (unchanged HSK Meter flow).
3. The seller reads what it can read itself from its own HashKey Chain RPC: addresses and transactions named in the
   question, plus network constants it knows (chain id, official RPC, explorer, USDC.e/USDT/WHSK contracts).
4. `hsk-answer` (gpt-oss-120b) answers with those facts and the workspace's Blockscout MCP server for anything else
   on-chain. Otari web search (max one search) and code execution are declared only when the router asked for them,
   because each costs about $0.01.
5. `hsk-verify` (Ministral 3 8B) checks the draft against the facts, fixes units and decimals, and fails answers that
   contradict the chain.
6. Paid calls (steps 4-5) run on **the buyer's own Otari API key**, whose spend ceiling is that buyer's AI budget, or on
   a shared pool key for buyers without one. If Otari refuses (`403 budget_exceeded`), every provider in the routing
   chain fails, or the verifier fails the answer, the work raises `Undeliverable` and the seller **refunds the payment
   on-chain**. The buyer is then refused at the quote for the rest of the UTC day, so it is never charged and refunded
   in a loop.
7. The on-chain request id is the Otari `Idempotency-Key`, so a re-serve of a paid request (HSK Meter re-serves a
   payment up to 3 times in an hour) returns the stored answer without calling or billing a provider again.

The answer comes with a receipt the buying agent can check (run `m4_drill`, the fault drill):

```json
"otari": {"tier": "dedicated",
  "screen": {"guard": {"verdict": "safe", "categories": "None"}, "route": {"in_scope": true, "needs_web": false, "needs_math": false}},
  "calls": [
    {"role": "guard",  "policy": "hsk-guard",        "servedModel": "Qwen3Guard-Gen-8B",  "provider": "ovhcloud", "costUsd": 0},
    {"role": "route",  "policy": "hsk-route",        "servedModel": "ministral-3b-2512",  "provider": "mistral",  "costUsd": 0.000017},
    {"role": "answer", "policy": "hsk-answer-drill", "servedModel": "openai/gpt-oss-120b", "provider": "deepinfra",
     "fallback": true, "attempts": 2, "costUsd": 0.000419},
    {"role": "verify", "policy": "hsk-verify",       "servedModel": "ministral-8b-2512",  "provider": "mistral",  "costUsd": 0.000184}],
  "buyerCostUsd": 0.000603}
```

## Open-weight models, one role each

| Role | Otari policy | Model (open weights) | Fallback | Paid by | Cost seen |
|---|---|---|---|---|---|
| Safety screen | `hsk-guard` | Qwen3Guard-Gen 8B (Alibaba, Apache 2.0) | Qwen3Guard-Gen 0.6B | screening key | $0 |
| Scope + tool routing | `hsk-route` | Ministral 3 3B (Mistral, Apache 2.0) | Ministral 3 3B on Bedrock | screening key | ~$0.00002 |
| Answer | `hsk-answer` | gpt-oss-120b (OpenAI, Apache 2.0); pool buyers: gpt-oss-20b | gpt-oss-120b on Nebius, then OVHcloud | buyer's key | $0.0001-0.0004 (+$0.01 per search or sandbox call) |
| Verify | `hsk-verify` | Ministral 3 8B (Mistral, Apache 2.0) | Ministral 3 8B on Bedrock, then Qwen3.5 9B | buyer's key | ~$0.0001 |

A typical paid question costs the seller $0.0002-0.0006 in model calls against a 0.03 USDC.e price.

## Otari features used (rubric map)

| Feature | How the app uses it | Where | Live evidence |
|---|---|---|---|
| Model routing and fallbacks | Every role is a routing policy with an `on_failure` chain across providers; `hsk-answer` also routes by key (`when: key_id` = pool key -> gpt-oss-20b). The fault drill `hsk-answer-drill` has a broken primary to show the failover | [`otari/config.json`](otari/config.json) `routingPolicies`, `--ai-answer-policy` | real outages: video takes 1 and A (DeepInfra failed, Nebius served, 2 attempts); fault drill `m4_drill` (primary absorbed with a 404) |
| Budgets and spending limits | Spend ceilings on every key, a daily workspace ceiling ($0.40) and a monthly organization cap ($1.50) | `otari/config.json` `spendCeilings` | ceilings enforced during these runs; total spend $0.085 |
| Per-user budgets | Each buyer wallet with a dedicated Otari key gets its own ceiling; `budget_exceeded` on that key becomes an on-chain refund and a same-day quote refusal | `server/otari.py` `KeyRing.for_buyer`, `server/ai_ask.py` `work` | runs `b1`-`b3`: served, then refunded ([refund tx](https://hsk.blockscout.com/tx/0xcac45800160fe190e1aa41fef0196be14a20601721a6b134bda6518265ffbd44)), then refused at the quote |
| Multiple models serving distinct roles | guard, router, answerer, verifier: four open-weight models from three families | `server/ai_ask.py` | every served run lists all four calls |
| MCP integrations | Blockscout MCP server configured in the Otari workspace with an allow-list of 7 tools, passed by `mcp_server_ids` | `--otari-mcp-server-id`, `otari/config.json` `mcpServers` | Otari usage rows show `__unlock_blockchain_analysis__` and `get_transaction_info` calls |
| Web search or web fetch | `otari_web_search` (max_uses 1) when the router says the question needs off-chain facts; workspace domain allow-list | `server/ai_ask.py` `_work` | run `m3` (Otari bills `web_search_calls: 1`) |
| Code execution | `otari_code_execution` when the router says the question needs a calculation | `server/ai_ask.py` `_work` | run `m5_code` (Otari bills `code_execution: 1`) |
| Usage visibility | Response headers (`Otari-Provider`, `Otari-Model`, `Otari-Fallback`, `Otari-Attempt-Count`) and `usage.cost_usd` go into the buyer's receipt; every call carries `metadata` tags (`buyer`, `rid`, `role`, `tier`) for Otari's usage records | `server/otari.py` `chat` | receipts in `otari/live_runs.json` |
| Per-key governance | `allowed_models` on every key: the screening key can only reach the guard and router models, buyer keys only the answer and verify models (a buyer key asking for `hsk-guard` gets 403) | `otari/config.json` `keys` | verified by a direct call |
| Guardrails | Not used as an Otari guardrail: on otari.ai an organization cannot define one without a vendor key. The screen runs as the `hsk-guard` policy instead | | see [feedback](OTARI_FEEDBACK.md) |

## Live runs on mainnet (2026-10-29)

Endpoint 4 was registered in [`0x112bcfb8…`](https://hsk.blockscout.com/tx/0x112bcfb88f3abecf6b1d5e7723dfaa01a90ccc1f3c33c3f237c18c39b234c67c)
(0.03 USDC.e, payee = the HSK Meter payee). Full records: [`otari/live_runs.json`](otari/live_runs.json).

| Run | What happened | Transactions |
|---|---|---|
| paid1 | balances + "how many questions can it still pay for": served | [settle](https://hsk.blockscout.com/tx/0x6caf020d88f1cc9cda42f94e3175ffed0e768bf7df9aa6c20f9aea85028394fc) |
| b1 | Agent B's buyer (budget: 2 Otari requests a day): served | [settle](https://hsk.blockscout.com/tx/0x2ce1e031d114af3a01409c2debd5757f17f54311493b812a9d924a52ac52097e) |
| b2 | same buyer: Otari `budget_exceeded`, refunded | [settle](https://hsk.blockscout.com/tx/0x17b38f7b832196be2dc003086e34bbd94852d7dd5a030f3b5eeee2b491816b88), [refund](https://hsk.blockscout.com/tx/0xcac45800160fe190e1aa41fef0196be14a20601721a6b134bda6518265ffbd44) |
| b3 | same buyer: `429 ai_budget_exhausted` at the quote, nothing charged | none |
| m2 | three web searches pushed a $0.02 key ceiling to $0.031, the verify call was refused, refunded | [settle](https://hsk.blockscout.com/tx/0xcb3b4fb531ca14d8287561dfdd58f3010801bfe3cbb538fd2e6f308b791bedd8), [refund](https://hsk.blockscout.com/tx/0x395d08669135aed90e0e559a9b8baf4f9ceac45d8fd336655a3fc3e191f6a049) |
| m3 | web search question, served but wrong (RPC URL, USDC.e): fixed in `bcf27b0` by grounding the network constants | [settle](https://hsk.blockscout.com/tx/0x110544755b83fa737ff2c3a51d2da66dcf8e300abbd64e85017087ffa515ee29) |
| m4_drill | fault drill: broken primary, Otari fell over to gpt-oss-120b, Blockscout MCP used | [settle](https://hsk.blockscout.com/tx/0xa43e3a2f1680b68dd6ad57d9f37d9a87d3737d989e8f08adbc8c95b078b797dc) |
| m5_code | quantitative question, Otari code execution ran | [settle](https://hsk.blockscout.com/tx/0x7c9c9d8e93a6b00e4b081b332e0621e0c880202c59f87a6b74e3a248e0c34913) |
| video take 1 | served, DeepInfra failed and Otari fell over to Nebius; the answer read the balance one block before the payment (fixed in `28f7c2d`) | [settle](https://hsk.blockscout.com/tx/0x785fbe686cb817d37020d12b49d64c83a200fffbd9f8e94b732f0783b6d32b94) |
| video take A | served, again DeepInfra -> Nebius (2 attempts), balance already includes the payment | [settle](https://hsk.blockscout.com/tx/0x5bfd43cd6dd77ce94ed77513802d66e4f15b7b23ffc4282446c007e4d2788912) |
| video take B | Agent B over its budget: Otari `budget_exceeded`, refunded | [settle](https://hsk.blockscout.com/tx/0x18d0aa9e3f1fbed6dba8d3e93dea7d4a1ef9e43f9e269134ba6b6efa4c174abc), [refund](https://hsk.blockscout.com/tx/0xb3ed625ab63437071cbc2d546c3a0f836fabb21415171dfc00f2ca5a96ad9be8) |
| video takes C, D | over-budget buyer refused at the quote (429); harmful question refused by the guard (422); nothing charged | none |

## Run it

```bash
# the seller, sandboxed (bwrap), with the AI endpoint on
HSK_METER_KEY_DIR=/path/to/seller-keys HSK_METER_VENV=/path/to/venv \
HSK_METER_OTARI_KEY_DIR=/path/to/otari-keys \
  deploy/run_sandboxed.sh seller --port 8402 --otari-mcp-server-id <workspace MCP server id>
#   /path/to/otari-keys: screen.env, pool.env, buyers/0x<address>.env, each holding one OTARI_API_KEY line, mode 0600
#   --ai-answer-policy hsk-answer-drill runs the fault drill (broken primary model)

# a paying agent
HSK_METER_BUYER_KEY_FILE=... HSK_METER_PROVIDER=0x30CCc6d816854d1a66fBfecE15b1515a3E941160 \
HSK_METER_PAYEE=0xA8A49A6Ca872D54a8FD5FB121139De9bff62a48D \
  python agent/buyer.py "http://127.0.0.1:8402/v1/ai/ask?q=how+much+usdc.e+does+0x5270fe01fd3d6148b687da0b5693b0425cfca361+hold%3F" \
  --max-price 0.05 --time-budget 90

# tests (offline: no keys, no network)
python tools/ci_offline.py server/test_ai.py
```

Questions are URL-decoded and lowercased, 8-240 characters of letters, digits, spaces and `. , ? ! : ; ' " ( ) / % # @ & + - _ = $`.
The paid resource is the raw query string, so the request id binds the exact question.

## Security notes

- One Otari key per purpose, each with its own ceiling and `allowed_models`; keys are read once from 0600
  one-variable files (no symlinks), never logged, printed or put in an error. The sandbox binds the Otari key
  directory read-only, and the in-process egress guard allows only the RPC host and `otari.ai:443`.
- The client allows only `https://otari.ai` and `https://api.otari.ai`, refuses redirects and caps bodies.
- The question, tool output, web pages and token or contract names are data for the models, never instructions for
  the seller; the models have no keys, files or wallet. Answers are cleaned of markdown and invisible or bidi
  characters before they are sold.
- Screening runs before the quote on the seller's own key, so a spam of refused questions costs the buyer nothing and
  the seller only the free guard model plus a cent-fraction router call, under the screening key's ceiling.

## What is not done

- Per-buyer keys are created in the Otari dashboard: an otari.ai organization cannot create service keys (which would
  make every buyer an end user with its own budget automatically). Buyers without a key share the pool key.
- No Otari guardrail (see above). No streaming. The seller runs on loopback for these runs, as the main HSK Meter
  seller does for its demos.
