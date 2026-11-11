# GridPact: A2A adapter and Path-mode check

GridPact is a peer-to-peer energy-trading protocol (MIT MAS.665 course project). Agents trade over an MCP
server with a locked stake, a hash-chained ledger and an auditor. It lives in its own repository,
<https://github.com/nh13120/gridpact>, and runs there. This folder is the only Town-side code: a small A2A
adapter, a script that runs the check, evidence bundles, and a test.

## What is tested

Town's Path mode (`nandatown test-agent --url ...`) against GridPact, through the adapter. For each Town quote
request the adapter makes a real GridPact trade over MCP (`propose`, then `respond accept`: stake locked,
commitment on the ledger) and answers with what GridPact recorded.

| Town order | GridPact |
|---|---|
| `quantity` widgets | that many MWh over steps 68-79 (12 steps) |
| `unit_price_cents` | cents per MWh |
| `request_id` | `propose(request_id=...)`, GridPact's idempotency key |
| `total_cents` | value of the commitment GridPact created |
| `commitment_id` | GridPact's id, so a second booking shows up as a different quote |

Profiles: `a2a-capability-fulfillment@0.3` (one quote, then Town repeats the same order) and
`a2a-order-conflict@0.1` (same order id re-sent with `quantity: 5`, which must be refused).

## Reproduce

Town needs Python 3.11+; GridPact was run on 3.14 (its `.venv`). No data files and no API key (GridPact runs on its
synthetic fixture, and no LLM agent takes part).

```bash
# 1. GridPact at the tested tag
git clone --branch hw8-v1 https://github.com/nh13120/gridpact.git
python3 -m venv gridpact/.venv && gridpact/.venv/bin/pip install -r gridpact/requirements.txt

# 2. this checkout
python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'

# 3. run the check (starts a fresh GridPact server and the adapter, then stops both)
GRIDPACT_DIR=$PWD/../gridpact GRIDPACT_PYTHON=$PWD/../gridpact/.venv/bin/python \
  examples/gridpact/run_check.sh runs/gridpact-check

# 4. the same as a test, plus unit tests that need no GridPact
GRIDPACT_DIR=$PWD/../gridpact GRIDPACT_PYTHON=$PWD/../gridpact/.venv/bin/python \
  python -m pytest -q tests/test_gridpact_adapter.py
```

Versions tested: Town `6f9a7c0` (main on 2026-11-11); GridPact tag `hw8-v1` (commit `cb29b2b`); Python 3.14.
To see the failure, point `GRIDPACT_DIR` at tag `v2` (GridPact before the fix) and set
`GRIDPACT_DATA_DIR=<hw8-v1 checkout>/fixtures/synthetic`, since `v2` has no fixture.

## Expected and observed

| | expected | GridPact `v2` (before) | GridPact `hw8-v1` (after) |
|---|---|---|---|
| quote | total 3990 cents | 3990 | 3990 |
| repeated order | no second fulfillment | **Failed**: second, different quote | Passed |
| same order id, `quantity: 5` | `rejected`, first quote stands | **Failed**: re-quoted 9975 | Passed |
| what GridPact recorded (one run each) | 1 commitment per order id | 4 commitments for 2 order ids, $660 stake | 2 commitments, $240 stake |

Before the fix, GridPact's `propose` had no request id. A retried proposal opened a second offer and
accepting both double-booked the seller. Town's duplicate and conflicting-retry stages found this from the
outside. The fix adds an optional `request_id` to `propose` (same id and terms returns the original offer;
same id, different terms is refused), and the same two profiles then pass. Bundles for both runs are in
`evidence/before-v2/` and `evidence/after-hw8-v1/`; check one with `nandatown verify <bundle>`.

## Limits

- The adapter is scripted. It does not use GridPact's LLM agents, the auditor, challenges or settlement;
  Town's quote profiles do not exercise them. Over-declared capacity (GridPact's main claim) is untested here.
- The adapter and GridPact are by the same team, so this is not an independent-agent result. It shows
  that GridPact's trade interface survives Town's repeat and conflict checks, not general A2A conformance.
- One buyer, one seller, one fixed order. Discovery, identity and payment layers are not exercised.
- The adapter needs a fresh GridPact server (agent tokens are issued once), and `descriptor_consistency` is
  Not tested (no pinned card digest).
