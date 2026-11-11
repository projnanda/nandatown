# FactoryBridge: Town mailbox integration

FactoryBridge coordinates printing, PCB, purchased components, inspection,
assembly and shipping for hardware orders. This example connects the project
from a separate checkout to Nanda Town's SQLite-backed mailbox. A requester
places a synthetic manufacturing order; a separate FactoryBridge worker runs
the actual planner, marketplace, simulated suppliers, event loop and checker;
the requester receives and checks the result through Town's mailbox.

The planner uses a **scripted plan reviewer**, not a live language model.
Suppliers, prices, inspections, schedules and delivery are simulated. The
example does not run FactoryOS CAD/slicing, operate equipment, buy anything or
demonstrate the deployed Internet of Agents.

## Tested versions and setup

- Nanda Town: `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244` (current main at test time).
- FactoryBridge: `55fd8d81d52cf554745b91e250e5fa74842fd49e`.
- The FactoryBridge repository is private. A reviewer needs authorized access
  to its source at that commit; no project code is copied to this repository.
- The observed run used Python 3.14.4, FastAPI 0.115.6, httpx 0.28.1 and
  Pydantic 2.13.3 on Windows. Nanda Town CI targets Python 3.11 and 3.12;
  this example has not yet been verified on either.

From a checkout containing this example and an authorized FactoryBridge checkout:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python examples/factorybridge/run_check.py \
  --factory-root /absolute/path/to/ai_studio \
  --out /tmp/factorybridge-town-evidence
```

This uses FactoryBridge's synthetic fixture and standard Python imports, not
the optional CAD/slicer or model-service dependencies. The two applications
are run as separate processes; the test driver launches the worker from this
example with FactoryBridge on its Python path. For full FactoryBridge setup,
consult its own README and lock files. Do not put API keys in the output.

## Expected and observed

The test creates a Town run, registers buyer and seller identities, and passes
two correlated request/response pairs through the actual TownDB mailbox,
including claims and fenced acknowledgements. Town's quote-shaped built-in
profile is used as required metadata; the manufacturing order is the message
payload, and the stock quote evaluator is **not** used. The example performs
its own checks and saves `results.json` and `town-events.json`.

| Case | Expected | Observed |
| --- | --- | --- |
| Print supplier delayed two days | Planner replans shipping, completes all quantities by day 8 within $100 simulated budget | Delivered day 8, $42.50 simulated total, 25 marketplace calls; a delay and shipping cancellation/rebooking were recorded |
| Printed-part quantity zero | Reject input before the simulation starts | Rejected; simulation_started=false |

Observed evidence was generated from the versions above using the command in
this document. Output includes FactoryBridge's tool trace and TownDB's events.
Tests are synthetic, not benchmark results or an independently verified
real-world manufacturing outcome.

## Compatibility and limits

This exercises Town's durable message acceptance, participant directory, token
authentication primitives, claims, acknowledgements and event recording with
the project's simulated multi-supplier workflow. The test driver calls TownDB
directly; **the HTTP coordinator, network transport, A2A, MCP, independent
agent processes for the buyer, portable identities and deployed NANDA Index
are not tested**. A worker process runs FactoryBridge, but the test driver
acts as requester and mailbox bridge. Town's own evaluator/evidence-bundle
verification is not used. Crash recovery, duplicate delivery, concurrent
requests and live-model behavior remain untested; do not deploy this test
adapter as an exactly-once order execution service.

MIT AI Studio Fall 2026, HW8.

