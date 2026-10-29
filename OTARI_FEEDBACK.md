# Otari feedback from building HSK Meter's paid AI endpoint

Written while building [`OTARI.md`](OTARI.md) on hosted otari.ai (organization owner, not a deployment operator,
default workspace, the $2 starting balance), 2026-10-29. Every item below was reproduced; request ids and numbers
are from our own organization. Total Otari spend for the whole build, tests and demo: about $0.09.

## The three questions

### When were you most tempted to skip Otari and call the provider directly?

At per-buyer budgets. HSK Meter sells to anonymous agents identified by a wallet address, so the budget we need is
one per wallet. Otari has exactly that feature (service keys whose requests name end users, each on its own budget,
closed as #1843), but an otari.ai organization cannot create a service key: the member key route accepts the fields
and silently drops them (bug 3 below), and `/api/v1/end-users` answers "This endpoint requires a service key". The
fallback is one API key per buyer, created by hand in the dashboard. For a marketplace with thousands of wallets that
does not scale, and for a while a direct provider call plus our own per-wallet ledger looked simpler than the
gateway.

The second moment was a spend ceiling being overshot: a key capped at $0.02 spent $0.0304 in a single request with
three web searches (bug 1). For a product whose whole promise is "your budget is enforced or you get refunded", a cap
that one request can pass by 50% is the thing we least wanted to explain.

We stayed because of what Otari did well: routing policies with fallback chains, per-key `allowed_models`, and
response headers that say who answered and what it cost removed a lot of code we would otherwise own.

### What did you build yourself that Otari should have done?

- **Per-buyer key mapping.** A key ring that maps a wallet address to an Otari key file, with a shared pool key for
  everyone else (`server/otari.py`). With service keys on hosted this would be the `user` field.
- **"Budget is used up" memory.** After a `budget_exceeded` we refuse that buyer at the quote for the rest of the
  UTC day, so we do not settle a payment only to refund it. Otari has no way for an API key to read its own
  ceiling (`/api/v1/organizations/me/usage` and the ceilings answer `401 Invalid master key` to an API key), so we
  cannot check headroom before taking money.
- **Budget tier-down.** The dashboard offers "Tier down on budget" (`budget_used_pct`), but it reads only the user's
  own budget, never a spend ceiling (bug 2), and hosted members only have ceilings. We tier by `key_id` instead and
  would have liked "80% of this key's ceiling -> cheaper model".
- **A guardrail.** No guardrail can be defined on hosted without a vendor credential (`any_llm` is refused with
  "would run on the deployment's own credentials"), although the catalog serves Qwen3Guard-Gen 0.6B and 8B at $0.
  We run the guard model as an ordinary routing policy and parse its `Safety: Unsafe` text. A guardrail profile
  backed by a catalog guard model, metered like a normal request, would do this properly.
- **Tool cost control.** Because tool fees are settled after the request and not held (bug 1), we declare web search
  and code execution only when a router model says the question needs them, and cap search with `max_uses: 1`.
- **A receipt for the buyer.** We assemble per-call provider, model, fallback, attempts and cost from headers. Code
  execution and MCP calls are not reported on Chat Completions (documented), and there is no per-meter cost in the
  response; the usage API has exactly the right data (`billing_meters`, MCP tool calls, absorbed fallback rows) but
  is not readable with the key that made the call.

### Would you keep using Otari without the rule?

Yes, for this product, with conditions. Routing policies with fallback across providers, the open-weight catalog at
these prices (a paid question costs us $0.0002-0.0006), `Idempotency-Key` (a retry with the same key returned the same
response and request id, and the usage records hold one billed row), per-key model allow-lists and tagged usage records are things we would not want
to rebuild. What would make it a clear yes for a multi-tenant product on otari.ai: service keys for organizations,
ceilings that include tool fees, tier-down that sees ceilings, and a usage/budget read for the calling key.

## Bugs, with steps

### 1. Hosted tool fees are not held against a spend ceiling, so one request overshoots it

Steps: create a budget of $0.02 per calendar day, attach it as a spend ceiling to an API key, send one chat
completion on that key with `"tools": [{"type": "otari_web_search"}]` and a question that makes the model search
three times. Observed: the request succeeds; its usage row is `cost 0.030363` with `web_search_calls: 3`
(`unit_rate 0.01`); the ceiling shows `current_spend 0.031292` against `max_budget 0.02`; the next request on the key
is refused `403 budget_exceeded`. Reading the source, `_apply_tool_charges` folds tool fees in at settlement, after
the hold for tokens was reserved, so they never meet the gate. `web_search_max_calls` (10) bounds it at about $0.10 per
request, plus code execution. Failover is careful not to "quietly take spend past the cap"; tool calls do exactly that.

### 2. `budget_used_pct` routing never fires for a spend ceiling

Steps: a policy with `select: [{when: {budget_used_pct: {gte: 80}}, target: <cheap model>}, {default: <model>}]`; a key
with a ceiling at 82% and then 92% used. Observed: the default model serves every request. `get_budget_state` reads
`User.spend` against the budget attached to the user (`User.budget_id`) only; scoped ceilings (api_token, workspace,
org_member) are never consulted. On otari.ai a member cannot attach a user budget, so the "Tier down on budget"
strategy the hosted routing guide and the dashboard offer has no effect there.

### 3. The member key route silently drops service-key fields

Steps: `POST /api/v1/organizations/me/keys` with `{"key_name": "...", "is_service_key": true, "end_user_budget_id":
"<budget>", "end_user_budget_ids": ["<budget>"]}`. Observed: `200`, and the key comes back with
`is_service_key: false`, `end_user_budget_ids: []`. Expected either a working service key or a 4xx naming the
fields; the routing policy API, by contrast, is "closed to unknown keys, so a typo is a 400".

### 4. Quickstart base URL returns 404

The hosted Quickstart and the multi-provider routing guide send requests to `https://api.otari.ai/v1/chat/completions`.
That answers `404 {"detail": "Otari serves its API under /api/v1, not /v1. Use /api/v1/chat/completions instead."}`.
The dashboard's own snippet uses `https://otari.ai/api/v1`, which works, as does `https://api.otari.ai/api/v1`. An
OpenAI SDK configured from the docs (`base_url="https://api.otari.ai/v1"`) fails on its first call.

### 5. Docs say routing policies cannot use otari.ai's managed models; they can

The hosted routing guide says a policy entry with no provider key of the caller's behind it "is skipped" and that a
request whose entries all fail that way is refused with `managed_credential_unsupported`. With no provider keys at
all, our policies serve managed models both as the default and as `on_failure` entries (request `3c9cf272-…`: the
primary absorbed a 404, the managed fallback served it, `Otari-Fallback: true`). Good news, but the docs steer people
away from a feature that works.

### 6. Direct search refuses while in-loop search works

`POST /api/v1/search {"query": "..."}` answers `400 No search tools are configured`, while `otari_web_search` declared
in a chat completion in the same workspace runs searches and returns sources. The tool-settings text says an unnamed
direct search uses the same in-loop instance.

### 7. A domain allow-list that removes every result is invisible to the caller

With a workspace `allowed_domains` that did not include the docs domain the search actually found, the call was
reported as `status: completed, sources: []`, billed $0.010, and the model answered with a confident, wrong chain id
and a made-up documentation URL. If the model is told "0 results after the workspace domain filter", it can say it
found nothing; today the caller cannot tell an empty search from a filtered one.

## Friction

- A routing policy created through the API answered `400 invalid_model "Unknown or unsupported model"` on the first
  call, and worked about 30 s later. The refresh window is documented; the error could say the policy is not loaded yet.
- A non-streaming request to `nebius:google/gemma-3-27b-it` produced no response for 180 s (our client gave up); its
  usage row appeared later as `success`, `latency_ms 603835`, billed. No per-request upstream timeout is available,
  and a slow (not failed) candidate never triggers the fallback chain.
- A catalog-id request with MCP servers answered `429 upstream_rate_limited` with one attempt and no fallback, while
  the same catalog id without MCP fell over from deepinfra to ovhcloud a minute later. It may be the documented "a tool
  loop with assistant state cannot be replayed", but the error does not say at which point the loop failed.
- Qwen3Guard-Gen 0.6B labelled a plain balance question "Controversial: Non-violent Illegal Acts"; 8B did not. Not an
  Otari issue, but a note for anyone wiring the free guard models.

## Issues opened on mozilla-ai/otari

- [#2694](https://github.com/mozilla-ai/otari/issues/2694) Gateway-run tool fees are not held against spend ceilings, so one request can pass a key's cap (bug 1)
- [#2695](https://github.com/mozilla-ai/otari/issues/2695) `budget_used_pct` ignores spend ceilings, so "Tier down on budget" never fires on otari.ai (bug 2)
- [#2696](https://github.com/mozilla-ai/otari/issues/2696) otari.ai organizations cannot create service keys: `POST /organizations/me/keys` silently drops `is_service_key` (bug 3)
