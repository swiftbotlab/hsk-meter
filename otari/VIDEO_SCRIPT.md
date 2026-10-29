# Demo video script (4:24)

Video: [`otari/otari-demo.mp4`](otari-demo.mp4) (1920x1080, captions + voice-over). Everything on screen is real: the
Otari dashboard of our organization, the agent console making paid calls on HashKey Chain mainnet on 2026-10-29,
and the HashKey Chain explorer. The console is a local page that runs `agent/buyer.py`'s `quote()` and `buy()`.
Counts on the slides (28 tests, 10 paid calls) are as of the recording; OTARI.md has the current ones.

| Time | On screen | Caption | Voice-over |
|---|---|---|---|
| 0:00 | title card | HSK Meter + Otari: a paid AI endpoint that refunds itself | |
| 0:04 | slide1.png | HSK Meter: agents pay per request on HashKey Chain, and every model call goes through Otari | HSK Meter lets one AI agent pay another for every API request, in USDC.e on HashKey Chain, and refunds the payment on-chain when the seller cannot deliver. For this hackathon we added a paid AI endpoint, and every model call goes through Otari. The question we wanted to answer: when an agent pays for an AI answer, who enforces its budget, and what happens to its money when the gateway says no? |
| 0:30 | slide2.png | Screened before the quote on the seller's key; answered and verified after payment on the buyer's key | Here is one paid question. Before any price is given, the seller's screening key asks two models through Otari: Qwen3Guard checks that the question is safe, and Ministral 3B checks that it is in scope and whether it needs web search or math. Only then does the agent get a 402 quote and pay on-chain. After payment, the buyer's own Otari key pays for the answer: gpt-oss-120b with a Blockscout MCP server, then Ministral 8B checks it against chain data the seller read itself. If Otari refuses, the seller refunds. |
| 1:04 | 1/3  The Otari configuration | 1/3  The Otari configuration |  |
| 1:06 | dash_routing.png | Routing: every role is an Otari policy with backups on other providers | In the Otari dashboard, every role is a routing policy with backups on other providers. hsk-answer also routes by API key: buyers on the shared pool key get the 20B model. The drill policy has a deliberately broken primary, to show the failover. |
| 1:23 | dash_keys.png | API keys: one per purpose, each with a model allow-list and its own budget | One Otari key per purpose. The screening key can only reach the guard and router models. Buyer keys can only reach the answer and verify models. Each key has its own budget. |
| 1:34 | dash_ceilings.png | Spend ceilings: per key, per workspace per day, per organization per month | Those budgets are spend ceilings: one per key, a daily cap for the workspace, and a monthly cap for the organization. A buyer key's ceiling is that buyer's AI budget. |
| 1:45 | dash_mcp.png | Tools: the Blockscout MCP server, 7 allowed tools, passed by id | The Blockscout MCP server is configured in the workspace with seven allowed tools. Web search is on with a domain allow-list, and code execution is on by default. The router decides per question whether to offer them, because each call costs about a cent. |
| 1:53 | dash_web.png | Web search with a domain allow-list; code execution on; both offered only when the router asks |  |
| 2:01 | 2/3  Live on HashKey Chain mainnet | 2/3  Live on HashKey Chain mainnet |  |
| 2:03 | takeA_paid.mp4 | A paid question: screened, quoted at 0.03 USDC.e, settled on-chain, answered through Otari | Now live, on HashKey Chain mainnet. The buyer agent asks about its own balance. The question is screened through Otari and quoted at 0.03 USDC.e. The agent signs, the seller settles the payment on-chain, and then asks Otari for the answer, on the buyer's own key. |
| 2:26 | takeA_paid.png | The receipt: DeepInfra failed, Otari fell over to Nebius (2 attempts); the answer passed verification | Here is the receipt. Look at the answer row: DeepInfra failed, and Otari fell over to Nebius on its own, in two attempts. The verifier passed the answer, and the balance already shows the payment. |
| 2:39 | takeB_refund.mp4 | Agent B: its Otari budget (2 requests a day) is already used up, and it pays anyway | Agent B has a tiny Otari budget: two requests a day, and it is already used up. It asks a question, and pays anyway. |
| 2:53 | takeB_refund.png | Otari answers budget_exceeded, so the seller refunds the 0.03 USDC.e on-chain | Otari refuses the answer call with budget exceeded, so the seller refunds the 0.03 USDC.e on-chain. The balance is back where it was. |
| 3:03 | bs_refund.png | The refund on the HashKey Chain explorer: HskPayPerRequest sends 0.03 USDC.e back | Here is that refund on the HashKey Chain explorer: the settlement contract sends 0.03 USDC.e back to Agent B. |
| 3:13 | takeC_quote_refused.mp4 | Same buyer, same day: refused at the quote, nothing signed or charged | If Agent B asks again the same day, the seller refuses at the quote. Nothing is signed or charged. |
| 3:24 | takeD_unsafe.mp4 | A harmful question: stopped by the Qwen3Guard screen before any price is given | And a harmful question is stopped by the guard model before any price is given. |
| 3:35 | dash_activity.png | Otari's activity log: policy, paying key, cost, and absorbed fallback attempts | Every call shows up in Otari's activity log: the policy, the key that paid, the cost, and the absorbed attempts when a provider fails. |
| 3:44 | 3/3  What we used, and what we learned | 3/3  What we used, and what we learned |  |
| 3:46 | slide3.png | Otari features used by the app | So the app uses routing with fallbacks, budgets and spending limits, a budget per buyer, four open-weight models in distinct roles, an MCP server, web search and code execution, all through one gateway. |
| 4:00 | slide4.png | Feedback and three issues opened on mozilla-ai/otari | Building it, we hit three real problems and opened issues on the Otari repository: tool fees can pass a spend ceiling, budget tier-down ignores ceilings, and hosted organizations cannot create service keys. The details and our feedback are on the branch. Thanks for watching. |
| 4:18 | end card | repo, branch, first Otari commit, issues, contract | |
