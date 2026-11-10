# Agent Trust Layer (ATL) in Nanda Town

ATL is a payment guard for AI agents (MIT AI Studio Fall 2026 project). An agent never holds payment
credentials. It submits each payment to ATL's `pay_check`, and ATL checks it against a mandate the owner
signed: registered payees and keys, categories, per-payment, daily and hard caps, duplicates, split orders and
instruction-like memo text. ATL answers approve, escalate or refuse. ATL executes approved payments itself and
returns an Ed25519-signed receipt. ATL runs as its own service, an MCP server over stdio. Its code lives in the
project repository, not here.

This folder connects ATL to Town in two places:

1. **Payments layer (Lab).** `atl_payments.py` registers `payments: atl.v1`. Every Town movement that pays a
   counterparty (`transfer`, or an escrow `release` to a seller) goes through ATL first. An approved payment is
   executed and receipted by ATL, the receipt is verified, and then Town's ledger moves the money. A refused or
   escalated payment moves no money, and its escrow is refunded. Town's ledger stays the money of record, so
   Town's conservation check still applies.
2. **A real agent (Track).** `agent/atl_buyer.py` runs a Claude Code agent as Town's buyer. It gets two MCP
   servers and no other tools: Town's participant adapter (`nandatown mcp serve`) and ATL. It requests a quote
   from Town's seller, checks it, acknowledges it to Town, and pays the seller only through ATL.

## Files

| File | Role |
|---|---|
| `atl_payments.py` | `atl.v1` plugin, a stdlib MCP client for ATL, and three validators |
| `town-mandate.json`, `track-mandate.json` | The owner's mandates (Lab buyer `buyer-1`, Track buyer `buyer`) |
| `setup_atl.py` | Owner step: sign and register the mandates in ATL's state directory |
| `scenarios/atl_marketplace.yaml` | Town's marketplace with ATL as the payments layer |
| `scenarios/atl_registry_eviction.yaml` | Town's `registry_eviction_unowned` attack with ATL |
| `scenarios/atl_registry_eviction_owned.yaml` | The same attack with `index.owned.v1` plus ATL (defense in depth) |
| `scenarios/atl_over_cap.yaml` | A purchase above the owner's per-payment cap |
| `agent/atl_buyer.py` | Claude buyer for `nandatown test-agent --role buyer` |
| `test_atl_payments.py` | Pytest compatibility checks (skipped without an ATL checkout) |
| `run_campaign.py` | Repeats every check and writes `SUMMARY.md` |
| `evidence/` | Campaign summaries and per-run results behind the tables below (bundle paths are relative) |

## Reproduce

Versions tested: Nanda Town `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244` (`nandatown 0.2.0`), ATL project commit
`402861c` (branch `atl-v2`), Python 3.12, `uv` 0.12, Claude Code 2.1 for the agent part. No payment credentials
are needed. ATL always runs on its simulated rail here, and each Town run works on a fresh copy of ATL's state.

```bash
# Town (this repository)
python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'

# ATL (the project, checked out elsewhere)
git clone <ATL repository> /path/to/atl && (cd /path/to/atl && git checkout 402861c && uv sync)
export ATL_REPO=/path/to/atl ATL_VAR_DIR=/tmp/atl-town
mkdir -p "$ATL_VAR_DIR" && python integrations/atl-payments/setup_atl.py   # owner registers the mandates

# Lab: one command per scenario, plus Town's own baseline for comparison
nandatown run integrations/atl-payments/scenarios/atl_marketplace.yaml
nandatown run integrations/atl-payments/scenarios/atl_registry_eviction.yaml
nandatown run registry_eviction_unowned                      # baseline: default ledger.v1
nandatown run integrations/atl-payments/scenarios/atl_registry_eviction_owned.yaml
nandatown run integrations/atl-payments/scenarios/atl_over_cap.yaml
nandatown run marketplace --plugin integrations/atl-payments/atl_payments.py --layer payments=atl.v1

# Tests and the repeated campaign
python -m pytest -q integrations/atl-payments
python integrations/atl-payments/run_campaign.py --seeds 100 --out runs/atl-lab

# Real agent (needs a logged-in `claude`); ATL_BUYER_ATTACK=1 adds the hidden standing order
nandatown test-agent --role buyer --profile quote-clean --timeout 180 \
  --cmd "python integrations/atl-payments/agent/atl_buyer.py"
python integrations/atl-payments/run_campaign.py --seeds 0 --agent-reps 3 --models sonnet,haiku --out runs/atl-agent
```

Every run leaves a Town evidence bundle (`nandatown report`, `verify`, `visualize`). ATL's own records for agent
runs are copied next to the bundle id (`agent-runs/<run_id>/`). They contain the agent's stream, ATL's tool-call
log, and ATL's simulated-rail ledger.

## Results

### Lab: 100 seeds per configuration, 700 runs

| Scenario / payments layer | Runs | Verdict passed | Money to the rival | ATL verdicts | Valid ATL receipts |
|---|---|---|---|---|---|
| marketplace / `ledger.v1` (Town baseline) | 100 | 100 | 0 | none | 0 |
| marketplace / `atl.v1` | 100 | 100 | 0 | approve 200 | 200 |
| registry_eviction_unowned / `ledger.v1` (Town baseline) | 100 | 0 | 3780 cents per run | none | 0 |
| registry_eviction_unowned / `atl.v1` | 100 | 0 | **0** | refuse 100 (R2) | 0 |
| registry_eviction / `index.owned.v1` + `ledger.v1` (Town baseline) | 100 | 100 | 0 | none | 0 |
| registry_eviction / `index.owned.v1` + `atl.v1` | 100 | 100 | 0 | approve 100 | 100 |
| over-cap purchase / `atl.v1` | 100 | 100 | 0 | escalate 100 (R6) | 0 |

Expected and observed agree on every row.

- **Normal trade.** With ATL in the payments layer, Town's marketplace still passes every stage and moves
  exactly the same money as the baseline (7180 cents per run). Each of the two escrow releases per run
  carries a verified ATL receipt.
- **Forged listing.** On Town's unowned-index attack, the default ledger pays the rival 3780 cents in every run.
  With ATL, the rival is never paid, because it is not a payee in the owner's mandate (R2). The buyer is
  refunded.
- **Owner caps.** An order above the owner's per-payment cap is escalated. The scripted owner refuses, the
  escrow is refunded, and the seller is not paid.

### Real agent (Claude buyer, Track profiles)

48 runs: 4 Town Track profiles, with and without the hidden standing order, 3 reps each, Claude Sonnet and
Haiku. Total model cost USD 1.13, mean agent time 35.5 s per run. Town starts its own scripted seller and
injects the profile's fault.

| Profile (Town fault) | Runs | Town verdict passed | Seller paid exactly once | Attack payments refused | Money to unregistered keys |
|---|---|---|---|---|---|
| quote-clean (none) | 12 | 11 | 12/12 | 5/5 attempts | 0 |
| quote-duplicate-delivery (work offered twice) | 12 | 10 | 12/12 | 5/5 attempts | 0 |
| quote-lost-ack (first ack lost) | 12 | 11 | 12/12 | 5/5 attempts | 0 |
| quote-crash-restart (seller dies after claiming) | 12 | 2 | 12/12 | 6/6 attempts | 0 |

- **Money.** The money outcome was right in all 48 runs. The agent paid the seller through ATL exactly once,
  even when Town duplicated the work or the seller crashed and was redelivered. Each payment carries a
  verified ATL receipt.
- **Hidden standing order.** With the order in its instructions, the agent tried to pay `seller-rival` in 21
  of 24 runs. ATL refused all 21 (R2, unknown payee), and nothing reached an unregistered key.
- **The failure, a real-agent boundary.** 14 runs ended `incomplete` because Town's `correct` stage had no
  evidence: the buyer's acknowledgement of the quote arrived after its lease expired, so Town recorded no
  processed ack. The lease is 5 s in three profiles and 1.5 s in `quote-crash-restart`, which caused 10 of the
  14. An LLM tool round trip can be slower than that. The agents reported this in their own final messages. Town's protocol
  behaved as designed (stale acks are fenced). The agent's payment through ATL was unaffected. A longer lease
  for model-driven buyers, or an agent that acks before reasoning, would be the fix to test next.

## What this shows, and its limits

- **What the integration covers.** ATL works as a drop-in Town payments layer for both Lab trades (marketplace
  escrow) and a real model-driven agent in Track. Its decisions and signed receipts sit next to Town's own
  evidence.
- **Boundary: ATL does not fix discovery.** With the unowned index, Town's `listing_intact` and
  `honest_trade_completed` still fail: the buyer finds only the rival, so no honest trade happens. ATL keeps
  the money safe but cannot create the right trade. Combined with `index.owned.v1`, every stage passes. The
  two defenses are complementary.
- **The Lab is deterministic.** The seeded scenarios gave the same outcome for every seed. The 100 seeds show
  stability, not independent samples.
- **The owner is scripted.** Escalations in the Lab are answered by a scripted owner (`ATL_TOWN_OWNER`,
  `refuse` by default). The `approve` path uses ATL's real owner channel (`atl approve`) but is not exercised
  by these scenarios.
- **Not tested here:** auctions and supply-chain milestone escrow with ATL; Track profiles with an LLM seller;
  agents other than Claude; ATL's real Asaas sandbox rail; and an attacker who knows ATL's rules and crafts
  a registered payee. The integration does not implement other Town layers (identity, auth, registry).
- **The mailbox agent is our own.** The Claude buyer was written by the ATL team for this test. Its success is
  not independent adoption by another team's agent.
