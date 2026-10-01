# LLM market: model-driven buyers through hash-locked escrow

Two buyers each buy one dataset from three sellers that list it. A language model makes every buying decision by tool call. The buyers are the participants under test, defined in `agents.py` and loaded by the scenario through `plugin_files`. The sellers are the town's `data-seller` role.

```bash
ollama pull llama3.1:8b
LLM_MARKET_MODEL=llama3.1:8b nandatown run examples/llm_market/llm_market.yaml
LLM_MARKET_MODEL=llama3.1:8b nandatown run examples/llm_market/llm_market_no_hashlock.yaml
nandatown verify runs/<id>
```

`TOWN_MODEL_URL` points at another OpenAI-compatible endpoint, and `TOWN_MODEL_KEY` adds a bearer token. `LLM_MARKET_MODEL=scripted:v1` runs a fixed policy with no inference (buy the cheapest listing, shop again after a failure); the tests use it.

## The market

| Seller | Price | Behaviour |
|---|---|---|
| seller-a | 1500 | honest |
| seller-b | 1100 | the cheapest, but it seals other bytes under the real listing |
| seller-c | 1400 | honest |

- **The deadline fault:** the second `escrow_locked` notice is held past the 5.0 s escrow deadline, so that seller's key claim arrives late.
- **Two buyers:** buyer-2 starts after buyer-1 has finished. It sees the town's reputation scores, including buyer-1's "bad" ratings of sellers whose orders failed.

## What the model decides, and what it does not

The model is asked at two points:
- **which listed seller to buy from**, seeing price and reputation;
- **what to do after an order fails:** shop again, or give up.

Code checks every call before acting: a tool offered at that point, a seller that is listed, and a price within the principal's cap. A refused call is recorded and asked again, at most twice. Each call is recorded as a `model_decision` event with the model name, tool, arguments, and whether it was accepted.

The sealed-box protocol is code, never the model's choice: verify the box, pay into hash-locked escrow, open the data with the key the ledger releases.

## Negative control

`llm_market_no_hashlock` is the same file on `deadline.v1`. Sellers hand keys to buyers themselves, and a key that arrives after its refund still opens the data. The scenario then fails `atomic_exchange` with "goods opened without payment".

## What happened (llama3.1:8b, 2026-09-30)

These are 5 runs of each scenario, with the model served by Ollama 0.30.7 on an RTX 6000 Ada.

| | `llm_market` (hashlock.v1) | `llm_market_no_hashlock` (deadline.v1) |
|---|---|---|
| verdict | passed 5/5 | failed 5/5: `order-buyer-1-2 goods opened without payment` |
| `verify` | 10/10 bundles verify | |
| guardrail rejections | 0 | 0 |

The model made the same choices in all five runs:

1. buyer-1 bought from seller-b, the cheapest. The ledger refused seller-b's claim because the content did not match the listing, and buyer-1 was refunded.
2. It shopped again at seller-a. Seller-a's key claim was the delayed one, so the claim was refused and buyer-1 was refunded again.
3. It bought from seller-c and opened the data.
4. buyer-2 then saw reputations of seller-b −1, seller-a −1 and seller-c +1. It **still chose seller-b**, calling its reputation "acceptable". The hashlock refused the junk and refunded buyer-2, which then bought from seller-c.

In the control, the same choices end with buyer-1 opening seller-a's data after its refund.

Two observations:
- **The protocol protected a buyer that ignored a negative trust signal** in favour of price.
- **Reputation built from failed orders cannot tell a cheat from a late seller.** seller-a was honest but delayed, and was rated down like seller-b.

## What this does not prove

- **Model runs are not reproducible from the seed.** The model is a mutable dependency. `temperature: 0` and a fixed request seed narrow the variation but do not remove it. `verify` replays the judgment over the recorded events; it does not replay the model.
- **The stages depend on the model's choices.** If a model never buys from seller-b, the wrong-content stage has no evidence, and the run reports that instead of passing.
- **This is Lab, not Track.** The buyers run in process through `TownAPI`, not as separate processes over the mailbox HTTP contract.
- **`nandatown campaign` takes only bundled scenario names**, so repeated runs of this file need a shell loop.
