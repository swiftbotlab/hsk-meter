# Agent-to-agent (A2A) demo: Agent B pays Agent A

One end-buyer request to **Agent B** (a risk-brief seller) makes Agent B **buy** the raw data from **Agent A**
over x402, so a single call produces **two** on-chain settlements between **distinct** wallets. This is the
"agents discover, collaborate, and transact" flow, settled on HashKey Chain mainnet (chain 177).

```
end-buyer ──0.08 USDC.e (ep3)──▶ Agent B ──0.03 USDC.e (ep1)──▶ Agent A
  (0x5270…A361)                 (payee 0xD680…4A83,           (payee 0xA8A4…a48D,
                                 buyer  0x7A03…4130)           settler 0x30CC…1160)
```

Agent B (`agent/agent_b.py`) reuses `server/seller.py`'s seller, settlement, refund and x402/EIP-3009 code
unchanged — it only advertises endpoint 3 (`/v1/hsk/risk-brief`) and pays Agent A through `agent/buyer.py`.

## Prerequisites (one-time)

- Agent A is this repo's seller with endpoints 1–2 registered (see the main README "Seller" section).
- Agent B needs its own on-chain endpoint 3 and two funded wallets:
  - a **provider/payee** wallet (settles Agent B's sales, pays gas) with a little HSK, and
  - a **buyer** wallet (pays Agent A) with a little USDC.e (no HSK needed — Agent A pays the settlement gas).
- Register endpoint 3 from Agent B's provider key, with Agent B's provider as its own payee (so no separate
  payee consent is needed): `registerEndpoint(80000, <AgentB provider>, "GET /v1/hsk/risk-brief")`
  (80000 = 0.08 USDC.e). On this deployment it is tx
  [`0x5c8f458d…`](https://hsk.blockscout.com/tx/0x5c8f458da639d2852c5ee0a0ef5c246005df8c80945ed061b9787a69e212bcfc).

## Run it (three terminals)

1. **Agent A** (port 8402):
   ```sh
   HSK_METER_KEY_DIR=<agent-a keys> HSK_METER_STATE_DIR=<repo>/run \
     python3 server/seller.py --host 127.0.0.1 --port 8402
   ```
2. **Agent B** (port 8403): it serves endpoint 3 and pays Agent A for each answer.
   ```sh
   python3 agent/agent_b.py --host 127.0.0.1 --port 8403 \
     --key-dir <agent-b seller keys: provider.env/adapter.env/token.env, 0600> \
     --agent-a-url http://127.0.0.1:8402 \
     --a-provider 0x30CCc6d816854d1a66fBfecE15b1515a3E941160 \
     --a-payee 0xA8A49A6Ca872D54a8FD5FB121139De9bff62a48D \
     --b-buyer-key-file <agent-b buyer key: one HKPW_BUYER_PK=0x… line, 0600> \
     --upstream-max-price 0.05 --upstream-cap 5.00
   ```
   On mainnet the egress guard allows only the chain RPC and Agent A's host; the buyer key is never printed.
3. **End-buyer** calls Agent B (pins Agent B's provider/payee):
   ```sh
   HSK_METER_BUYER_KEY_FILE=<end-buyer key file> \
     python3 agent/buyer.py "http://127.0.0.1:8403/v1/hsk/risk-brief?address=0x<any address>" \
       --expect-provider <AgentB provider> --expect-payee <AgentB provider> \
       --max-price 0.10 --session-cap 0.40
   ```

The response is a risk brief whose `upstreamPayments[]` carries the tx hash of Agent B's own payment to Agent A —
the A2A hop, verifiable on-chain. The end-buyer's payment to Agent B is a second settlement on endpoint 3.

## Verified mainnet evidence (captured 2026-10-26)

| run | risk brief on | end-buyer → Agent B (ep3) | Agent B → Agent A (ep1) |
|---|---|---|---|
| 1 | the settlement contract | `0x7771c755…` | `0xf8df895c…` |
| 2 | an EOA | `0x29b0c35a…` | `0x65572b77…` |
| 3 | the APRO USDC/USD feed | `0xa69e680a…` | `0x0e050d7e…` |

**Composed refund** (start Agent A with `--smoke-refund-address 0x…dEaD`, then request that address): Agent A
refunds Agent B, so Agent B cannot deliver and refunds the end-buyer. Both wallets ended exactly where they
started. ep3 paid `0xee9845cb…` → refunded `0xf407cf85…`; ep1 paid `0xddedc886…` → refunded `0xf8aa291d…`.
(Open each on `https://hsk.blockscout.com/tx/<hash>`.)
