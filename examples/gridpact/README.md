# GridPact: A2A adapter and Path-mode check

GridPact is a peer-to-peer energy-trading protocol (MIT MAS.665 course project). Agents trade over an MCP
server with a locked stake, a hash-chained ledger, deterministic settlement and an auditor that can
challenge over-declared capacity. It lives in its own repository, <https://github.com/nh13120/gridpact>, and
runs there. This folder is the only Town-side code: a small A2A adapter, a script that runs the check,
evidence, and a test.

## What is tested

Town's Path mode (`nandatown test-agent --url ...`) against GridPact, through the adapter. For each Town quote
request the adapter plays one GridPact order through real GridPact tools over MCP:

1. `propose` (with the order's `request_id` as GridPact's idempotency key) and `respond accept`: stake locked,
   commitment on the ledger.
2. An auditor agent reads the seller's declared capacity and, if it is more than 10% above the nameplate,
   calls `challenge`. GridPact rules on it, trims the commitment to the real capacity and pays a bounty.
3. GridPact settles delivery for that commitment (`operator_settle`, a guarded operator tool).
4. The quote is read back from GridPact: `total_cents` (value of the commitment), `commitment_id`, the audit
   verdict, and ordered, delivered and shortfall kWh.

| Town order | GridPact |
|---|---|
| `quantity` widgets | that many MWh, delivered over 12 fifteen-minute steps from 17:00 (2 steps in the liar variant) |
| `unit_price_cents` | cents per MWh |
| `request_id` | `propose(request_id=...)` |
| `total_cents` | value of the commitment GridPact created |
| `commitment_id` | GridPact's own id, so a second booking shows up as a different quote |

Profiles: `a2a-capability-fulfillment@0.3` (one quote, then Town repeats the same order) and
`a2a-order-conflict@0.1` (same order id re-sent with `quantity: 5`, which must be refused). Two variants:
`honest` (seller PVB_17) and `liar` (seller PVB_37, which declares 5000 kW for a 2500 kW battery).

## Reproduce

Town needs Python 3.11+; GridPact was run on 3.14. No data files and no API key (GridPact runs on its
synthetic fixture; the auditor is a fixed rule, no LLM).

```bash
# 1. GridPact at the tested tag
git clone --branch hw8-v2 https://github.com/nh13120/gridpact.git
python3 -m venv gridpact/.venv && gridpact/.venv/bin/pip install -r gridpact/requirements.txt

# 2. this checkout
python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'

# 3. run the check: honest seller, then the over-declaring seller
export GRIDPACT_DIR=$PWD/../gridpact GRIDPACT_PYTHON=$PWD/../gridpact/.venv/bin/python
examples/gridpact/run_check.sh runs/gridpact-honest
VARIANT=liar examples/gridpact/run_check.sh runs/gridpact-liar

# 4. the same as tests (5 unit tests with a fake GridPact need nothing; the integration test needs the exports above)
python -m pytest -q tests/test_gridpact_adapter.py
```

`run_check.sh` starts a fresh GridPact server and the adapter for each profile, runs it, prints each stage and what
GridPact recorded, and stops both. Versions tested: Town `6f9a7c0` (main on 2026-11-11); GridPact tag `hw8-v2`;
Python 3.14. For the failure run, point `GRIDPACT_DIR` at tag `v2` (GridPact before the fix) and set
`GRIDPACT_DATA_DIR=<hw8-v2 checkout>/fixtures/synthetic`, since `v2` has no fixture. Output and bundles of the
three runs below are saved in `evidence/*/` (`run_check.txt` plus the Town bundles; check one with
`nandatown verify <bundle>`).

## Expected and observed

| | expected | GridPact `v2` (before) | `hw8-v2` honest | `hw8-v2` liar |
|---|---|---|---|---|
| quote | total 3990 cents | 3990 | 3990 | 3990 |
| repeated order (`duplicate_request`) | no second fulfillment | **Failed**: second, different quote | Passed | Passed |
| same id, `quantity: 5` (`conflicting_retry`) | `rejected`, first quote stands | **Failed**: re-quoted 9975 | Passed | Passed |
| commitments GridPact recorded per profile | 1 | 2 (duplicate) | 1 | 1 |
| audit | honest: none; liar: challenge valid | not run | no challenge | valid challenge, $12 bounty |
| delivery of the 2 MWh order | honest: all; liar: what its battery can give | not run | 2000 of 2000 kWh | 1250 of 2000 kWh (trimmed from 1000 to 625 kWh/step), no penalty |

**Failure found from outside.** Before the fix GridPact's `propose` had no request id. A retried proposal opened a
second offer and accepting both double-booked the seller. Town's duplicate and conflicting-retry stages caught it.
The fix (optional `request_id` on `propose`: same id and terms returns the original offer, same id with different
terms is refused) is in `hw8-v2`, and the same profiles then pass.

**Boundary case: the over-declaring seller.** Town's profiles still pass, because they check the quote and the
retry behaviour. GridPact's own ledger shows what the quote does not: the auditor challenged the lie before
settlement, GridPact trimmed the commitment to the battery's real 625 kWh/step, and the buyer received 1250 of
the 2000 kWh it ordered (the rest is released for re-sourcing; the adapter does not re-source). The quote's
`total_cents` stays at the order value. Town's evidence bundle records only `total_cents` and `request_id`, so the
under-delivery is visible in `run_check.txt` and the GridPact ledger, not in the Town verdict. A Town profile that
checks delivered quantity would catch it.

## Limits

- The agents are scripted: the Town requester sends one fixed order and the auditor is a fixed rule with the
  nameplates handed to it (as in GridPact's HW7 benchmark). GridPact's Haiku agents and their negotiation
  (counters, several sellers, the buyer re-sourcing after a challenge) are not driven through Town.
- Adapter and GridPact are by the same team, so this is not an independent-agent result and not A2A conformance.
- One buyer, one fixed order. Discovery, identity and payment layers are not exercised. `descriptor_consistency`
  is Not tested (no pinned card digest).
- Settlement goes through an operator tool that GridPact only enables when started with `GRIDPACT_OPERATOR_KEY`;
  the check sets it, and agents never have it. Without it the adapter skips settlement.
- The adapter needs a fresh GridPact server (agent tokens are issued once).
