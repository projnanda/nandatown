# AgentTab on the Track: buy on credit, then quote

[AgentTab](https://github.com/KevinChunye/AgentTab) is a credit line for AI
agents that earn. An agent signs an IOU for a purchase, the seller
countersigns it, and AgentTab's engine approves or refuses the charge against
the agent's limit and its owner's policy. It is a separate service with its
own repository and deployment. None of its code is in this repository.

This integration puts an AgentTab-funded agent in Town's seller role and asks
one question: **Town delivers work at least once; does AgentTab charge for it
exactly once?**

## What is connected

| Piece | Where it runs | Role |
|---|---|---|
| `examples/agenttab_seller.py` | a Town `--cmd` participant | Town's seller. For each quote it buys one 40-cent price lookup on its AgentTab line, then answers |
| AgentTab service | elsewhere (local process or the hosted deployment) | registers the owner, agent and supplier; decides, holds and captures the charge |
| Town buyer, coordinator, evaluator | Town | request the quote, inject the fault, judge the stages |
| `examples/agenttab_check.py` | after the run | compares Town's evidence with AgentTab's ledger |

Per quote the adapter makes AgentTab's whole purchase path over HTTP:
`register_owner`, `verify_email`, `register_agent` (with an Ed25519 key and a
deposit), `set_policy`, `register_seller` once; then `check_credit`, a signed
and countersigned IOU to `charge`, and `capture`. The IOU nonce is a hash of
the Town run and message id, so a redelivered request re-presents the same
IOU. The charge's ids travel in the seller's acknowledgement note and so into
Town's event log.

## Reproduce

Town at `6f9a7c0`. AgentTab at `4f3c58c`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Offline, against a contract stub of AgentTab's API (a Town fixture, not
AgentTab):

```bash
python -m pytest -q tests/test_agenttab_integration.py
```

Against a real AgentTab service. Use a local one (`python start.py` in an
AgentTab checkout serves port 8000) or the hosted deployment:

```bash
export AGENTTAB_URL=https://e0435c42-a98f-438c-9a06-8e127e5db0be.mtm.sh
```

```bash
python -m pytest -q tests/test_agenttab_integration.py -k live
```

One profile by hand, then the cross-check on the bundle it prints:

```bash
nandatown test-agent --role seller --profile quote-amnesia-restart --cmd "python examples/agenttab_seller.py"
```

```bash
python examples/agenttab_check.py runs/run-XXXX
```

## Expected and observed

Expected in every row: the Town verdict is PASSED, and AgentTab holds exactly
one 40-cent charge for the one quote. "Charged" is expected / observed
lookups from `agenttab_check.py`.

| Town profile | What breaks | AgentTab `4f3c58c` (local) | Hosted deployment (older build) |
|---|---|---|---|
| `quote-clean` | nothing | PASSED, 1 / 1 | PASSED, 1 / 1 |
| `quote-crash-restart` | seller stalls past its lease and restarts | PASSED, 1 / 1 | PASSED, 1 / 1 |
| `quote-amnesia-restart` | seller pays, answers, forgets, dies before the ack | PASSED, 1 / 1 | **INCOMPLETE, 0 / 1** |
| `quote-duplicate-delivery` | the work is offered twice | PASSED, 1 / 1 | PASSED, 1 / 1 |
| `quote-lost-ack` | the first acknowledgement is lost | PASSED, 1 / 1 | PASSED, 1 / 1 |
| `quote-drop-wakeup` | the wake-up hint is lost | PASSED, 1 / 1 | PASSED, 1 / 1 |

Observed on 2026-11-10 with Town 0.2.0 (evaluator 0.5.0) on Python 3.12.

**The one failure is real.** On the hosted build, a seller that restarts
without its journal re-presents its nonce and AgentTab refuses it as
`iou_replay`, with no way to look the first answer up. The seller has been
charged once and must decline work it already paid for; Town stops at the
`received` stage and the check reports 0 expected, 1 observed. AgentTab fixed
this in `14d5403` (the same nonce returns the stored reply, and
`lookup_charge` finds it); the hosted service has not been redeployed since.
The acknowledgement note records it:

```json
{"reason": "retry_refused", "charge_error": "iou_replay",
 "lookup_status": 404, "lookup_error": "unknown_tool"}
```

## Boundary cases

| Case | How to run it | Town | AgentTab | Reading |
|---|---|---|---|---|
| Owner policy forbids the purchase | `AGENTTAB_BOUNDARY=policy_cap` | INCOMPLETE; seller acks `rejected`, no quote sent | 0 charged; `binding_rule: owner_policy` | A refused agent declines the work. It never quotes unpaid |
| Fresh nonce per attempt (negative control) | `AGENTTAB_NONCE=fresh`, profile `quote-amnesia-restart` | **PASSED** | **2 charged** | Town sees one correct quote and cannot see the double payment. Only the ledger check catches it |
| AgentTab unreachable | `AGENTTAB_URL=http://127.0.0.1:9` | INCOMPLETE; every ack `retryable` | nothing | The work stays redeliverable; no quote is sent |
| Purchase slower than the lease | hosted service, `quote-crash-restart` (1.5 s lease) | PASSED | 1 charged | The stale ack is fenced, Town redelivers, the seller reports the application from its journal instead of buying again |

## What this shows, and what it does not

Shown: an agent funded by AgentTab can hold a Town role over the HTTP mailbox
contract, and AgentTab's charge is applied once under five of Town's delivery
faults on both builds, and under all six on the current one. A Town pass
alone does not establish that a payment happened once; the two evidence
trails have to be compared.

Not tested:

- Town's own buyer does not pay through AgentTab. Only the seller's input
  purchase is on credit; the quote itself is free.
- Nothing after the charge: statements, autopay, settlement to the supplier,
  interest and defaults run on AgentTab's daily cycle, which no Town profile
  advances.
- AgentTab's MCP and `/chat` interfaces, and Town's Path (A2A) and Lab modes.
  AgentTab has no A2A endpoint.
- Portable identity (`--identity`). The adapter joins with the run token.
- More than one quote per run, concurrent sellers, or a limit reached by
  volume.
- The adapter chooses to act on `FAULT` for the two crash profiles, as Town's
  stock seller does. An agent joined with `--wait` would not be told to crash.
- The offline tests use `tests/fixtures/agenttab_contract_stub.py`. It checks
  both IOU signatures but its credit decision is a fixed limit. Passing it
  says the adapter follows the contract, not that AgentTab is correct.

The bundle's `state/seller/` holds the test agent's AgentTab keys. Do not
share it.
