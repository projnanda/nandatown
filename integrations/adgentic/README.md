# Adgentic on the Town Track

[Adgentic](https://github.com/meganshi-418/adgentic) is an exchange for shopping
agents. A buyer agent posts what its owner needs (`post_intent`). The exchange routes
the request to seller agents, which return signed, structured offers. Offer Review
checks each offer against the seller's registered records and strips prompt-injection
text. The buyer agent then ranks the reviewed offers and calls `accept_offer`. Adgentic
runs as an MCP server outside this repository.

This integration puts the whole Adgentic exchange into a Town Track run as the
**buyer-side participant**. It also registers Town's mailbox seller as an
**external seller on the Adgentic exchange**, so a Town agent competes for an
Adgentic buyer's order through Town's coordinator. This folder holds only the bridge,
the tests and the evidence.

```
Adgentic buyer agent ──MCP──> Adgentic exchange ──routes intent──> Adgentic's own sellers (s1..s5)
(scripted or Claude Code)       │  signs/reviews/accepts
                                └─ town_seller_connector ──Town HTTP mailbox──> Town Track seller
                                   (joins the run as "buyer")   quote_request / quote_response,
                                                                leases, fences, redelivery, faults
```

## What is tested

The interface under test is the Town Track HTTP mailbox: join, participant discovery
by capability (`quote.read`), message send, claim under lease, and fenced
acknowledgement. The questions are:

1. Can a Town agent's quote enter Adgentic as a signed offer, pass Offer Review and be
   accepted by an Adgentic buyer agent?
2. Does the Adgentic workflow stay correct under Town's injected faults: seller crash
   and restart, lost journal, dropped wake-up, duplicate delivery, lost ack, and two
   distinct responses? "Correct" means exactly one intent, one Town request, at most
   one acceptance, and valid signatures.
3. Where does it stop working? The boundary case is Adgentic's seller deadline set
   shorter than Town's crash recovery.

## Files

| File | Role |
|---|---|
| `adgentic_buyer.py` | Town `cmd:` buyer harness. Starts the Adgentic MCP server and acts as the Adgentic buyer agent (`--buyer scripted` or `--buyer claude`) |
| `town_seller_connector.py` | Adgentic plugin (`ADGENTIC_PLUGINS`). Joins Town, discovers the seller, turns Town quotes into signed Adgentic offers, and claims and acknowledges responses |
| `run_matrix.py` | Runs every case, then checks each one with `nandatown verify` plus the Adgentic log (signatures re-verified) |
| `test_adgentic_town.py` | Opt-in pytest. Skipped unless `ADGENTIC_DIR` is set |
| `results/` | `results.json` with the checks for all 33 matrix runs. `runs/<case>_r1/` keeps one full bundle per case (five Town records, manifest, attestation, `report.md`) with its Adgentic logs in `adgentic/`; `claude_buyer/` keeps the two real-agent runs. No `state/` or keys. `run_matrix.py` regenerates all of them |

## Reproduce

Versions used: Town `projnanda/nandatown@6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`
(nandatown 0.2.0, Track evaluator 0.5.0). Adgentic
`meganshi-418/adgentic@8e1a07458b1b5546e78dddb96ca1600bc02a4d1b` (branch
`hw8-nandatown`). Python 3.13; the integration uses only features available in 3.11+.

```bash
# Adgentic, checked out next to this repository
git clone https://github.com/meganshi-418/adgentic ../adgentic
git -C ../adgentic checkout 8e1a07458b1b5546e78dddb96ca1600bc02a4d1b
python3 -m venv ../adgentic/.venv && ../adgentic/.venv/bin/pip install -r ../adgentic/requirements.txt

# Town
python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'
export NANDATOWN_HOME=$(mktemp -d) ADGENTIC_DIR=$(cd ../adgentic && pwd)

# One run: Town's crash-restart profile, Adgentic exchange as the buyer side
nandatown run quote-crash-restart \
  --agent buyer=cmd:"$ADGENTIC_DIR/.venv/bin/python integrations/adgentic/adgentic_buyer.py --adgentic-dir $ADGENTIC_DIR"

# Full matrix (11 cases x 3 repeats, about 2 minutes, no model calls)
python integrations/adgentic/run_matrix.py --adgentic-dir "$ADGENTIC_DIR" --repeats 3

# Opt-in tests
ADGENTIC_DIR="$ADGENTIC_DIR" python -m pytest -q integrations/adgentic

# Optional: Claude Code as the Adgentic buyer agent (needs a logged-in `claude` CLI)
ADGENTIC_TOWN_MARKET=mixed nandatown run quote-crash-restart \
  --agent buyer=cmd:"$ADGENTIC_DIR/.venv/bin/python integrations/adgentic/adgentic_buyer.py --adgentic-dir $ADGENTIC_DIR --buyer claude"
```

Settings: `ADGENTIC_TOWN_TIMEOUT` sets the seller deadline in seconds (default 10).
`ADGENTIC_TOWN_MARKET` is `solo` by default, where only the Town seller carries
widgets; with `mixed`, Adgentic's five sellers also list them. `ADGENTIC_TOWN_DRAIN`
sets how many seconds the exchange keeps listening after the purchase (default 0).

How the Town task maps to an Adgentic intent: `sku` becomes `category`, `quantity`
stays `quantity`, `expected_total_cents` becomes the budget `max_total_price`, and the
priority is `["price"]`. The connector sends `unit_price_cents` from the Town seller's
registered listing (1995).

## Results

Expected values were written into `run_matrix.py` before the matrix ran. Each case
ran 3 times, with identical outcomes in every repeat.

| Case | Town profile | Expected | Observed (3/3 runs) |
|---|---|---|---|
| clean | quote-clean | Town passed; Town quote becomes an approved offer; Adgentic accepts it | Town passed; 1 offer, approved, signature valid; accepted $39.90 from `town-seller` |
| crash-restart | quote-crash-restart | Same; crash absorbed without a second intent or request | Same; 1 intent, 1 Town request; quote arrived after redelivery (~2.7 s) |
| amnesia-restart | quote-amnesia-restart | Same | Same |
| drop-wakeup | quote-drop-wakeup | Same | Same |
| duplicate-delivery | quote-duplicate-delivery | Same | Same |
| lost-ack | quote-lost-ack | Same | Same |
| amnesia-fresh-ids | quote-amnesia-fresh-ids | Town **fails on purpose** (two distinct seller responses); Adgentic accepts once | Town failed `response`, `correct`, `amnesia_survived`; Adgentic saw **1** response (see finding 2) and accepted once |
| amnesia-fresh-ids-drain | same, 6 s drain | Town fails; Adgentic sees 2 distinct responses, keeps 1 offer, accepts once | Town failed as above; Adgentic got 2 distinct responses, 1 offer, 1 acceptance |
| crash-restart-timeout-1s | quote-crash-restart, 1 s deadline | **Boundary:** Adgentic skips the recovering seller and buys nothing; Town still passes | Town passed every stage; Adgentic logged `town_quote_timeout`, created 0 offers, accepted nothing |
| mixed-clean | quote-clean, mixed market | Town quote competes; cheapest honest Adgentic seller wins; deceptive seller flagged | Town passed; 3 offers (Town $39.90, s1 $33.92, s5 flagged `POLICY_MISMATCH`, `INJECTION_STRIPPED`); accepted s1 $33.92 |
| mixed-crash-restart | quote-crash-restart, mixed market | Same under seller crash | Same |

All 33 runs matched expectations, and `nandatown verify` passed on all 33 bundles.
All 42 Adgentic offer signatures in the matrix re-verified against the registered
public keys. The Town wall time per run was 2.3 to 8.4 s, and the
scripted buyer makes no model calls.

**Real agent.** With Claude Code (`claude-sonnet-5-5`) as the Adgentic buyer and the
mixed market, `quote-clean` and `quote-crash-restart` both passed in Town. In both,
Claude saw the Town quote next to Adgentic's sellers, avoided the flagged offer and
accepted s1 at $33.92 (cost $0.17 and $0.05; 24 s and 18 s). Bundles are in
`results/claude_buyer/`, and both pass `nandatown verify`.

## What this shows, and what it does not

**Compatible.** Town's mailbox contract carried an Adgentic intent to a Town seller and
the answer back into Adgentic's signed-offer and review pipeline. Town's
at-least-once delivery did not cause duplicate Adgentic intents, requests, offers or
acceptances under any of the six seller faults. The exchange claims and acknowledges
every response it receives before it exits. The one exception is a response that
arrives after it has exited (finding 2).

**Findings:**

1. **Town can pass while the project outcome fails (boundary case).** With a 1 s
   Adgentic seller deadline, Town recovered the crashed seller and every Town stage
   passed. Adgentic had already given up on that seller, so the buyer bought nothing.
   The Town verdict covers delivery and correctness of the quote. It does not cover
   whether the quote arrived in time to matter to the caller. The default 10 s
   deadline covers Town's 1.5 s lease and restart, but an integrator has to choose a
   deadline above the counterparty's recovery time.
2. **Late responses can land after the caller has left.** In `amnesia-fresh-ids`, the
   second response arrived after the exchange finished and exited, so the duplicate
   path was never exercised. Adding a drain window showed that Adgentic keeps the
   first response and records the second without a second offer. The first matrix run
   also had a checker bug: it compared Town verdicts case-sensitively and reported
   0/30. That was fixed before the results above.
3. **Schema gap.** A Town quote carries only `total_cents`. Adgentic offers need
   delivery and return terms, so the bridge fills them from the Town seller's
   registration (3 days, 0-day returns), not from the seller. A Town seller cannot
   misstate those terms, so Offer Review's `POLICY_MISMATCH` check cannot fire for it.
4. **Identity gap.** Town authenticates the seller through its run session. The
   Adgentic offer signature comes from the bridge's per-run key, so it shows that the
   bridge committed to the offer, not that the Town seller signed it.

**Not tested:** an independently developed seller agent (the counterparty here is
Town's own scripted seller, so this is not independent adoption), `--identity`
portable grants, Lab scenarios and Path/A2A, more than one Town seller, LLM-driven
Town participants (`quote-llm*`), a remote coordinator, payment or settlement after
acceptance, and Town sellers returning malformed totals (the connector rejects
non-integer totals, but no profile produces one).

Course context: MIT AI Studio Fall 2026, HW8.
