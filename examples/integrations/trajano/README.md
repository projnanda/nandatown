# Trajano in Nanda Town

[Trajano](https://github.com/augustoal/trajano) is an agent-native B2B procurement
marketplace. Buyer and seller agents trade fasteners only through its MCP tools,
using typed messages (`rfq`, `offer`, `counter`, `accept`, `withdraw`, order
updates). The server enforces the rules: two negotiation rounds, price floors,
offer hashes and expiry, stock, idempotency keys, and the buyer's signed
spending mandate, which is checked at `accept`.

This example runs one complete purchase on Trajano inside a Town Lab scenario.
It also runs a failure case and a boundary case against the same live server.

## What runs where

| Participant | Developed by | How it takes part |
| --- | --- | --- |
| Trajano server (FastAPI, two MCP servers, SQLite) | Trajano | Separate process, launched from its own checkout and virtual environment |
| Five rule-based seller agents | Trajano | MCP clients of Trajano's seller server, in the same process as the server |
| `market` (`trajano_market`) | this example | The adapter, and the only Town role that talks to Trajano. It turns each Town message into one real MCP call on Trajano's buyer server |
| `buyer` (`trajano_buyer`) | this example | Town-scripted procurement agent. It reaches Trajano only through Town messages, so Town faults apply to it |
| Registry, transport, ledger, privacy, evidence | Town | Shared infrastructure |

The interface under test is MCP over streamable HTTP. The buyer side uses the
tools `search_listings`, `request_quote`, `counter_offer`, `accept_offer`,
`withdraw`, `get_thread`, `get_inbox` and `get_order`, called through the
official `mcp` SDK (`trajano_mcp.py`).

Trajano is pinned in `project_version.json` (commit `26d78f6`). The adapter
starts it with `python -m scripts.town_world`. That script builds a fresh
database with the clock frozen at the task's morning, seeds the catalog, and
provisions one buyer account and its signed mandate. It then starts the
server on a free port and runs the five sellers. When the buyer finishes, the
adapter stops the world. Trajano then writes an **audit**: the buyer's orders
and threads as its database records them, scored against the answer key of
Trajano's solver. The validators compare the buyer's beliefs with that audit.

The buyer's API key and signed mandate stay inside the adapter. The buyer refers
to the mandate by the handle `mandate-1`, and `redact_fields` keeps both values
out of the evidence. The `privacy` stage checks this.

## The workflow

The task is `t01` from Trajano's eval set: 500 × M8×30 ISO 4017 steel 8.8 hex
bolts, shipped to Ohio within 18 days. The mandate caps the unit price at
`0.1502`, the total at `98.84` and the quantity at `1000`.

1. **Discovery.** The market publishes `procurement.fasteners` in Town's
   registry, and the buyer looks it up.
2. **Search.** The buyer calls `search_listings` with the spec. Trajano ranks
   the candidates by landed cost: goods, freight, tax and pack rounding.
3. **Quotes.** The buyer sends `request_quote` to the two best listings. The
   rfq never carries the buyer's budget. The sellers answer asynchronously;
   `await_inbox` polls `get_inbox` until both threads await the buyer.
4. **Counter.** The buyer counters the cheapest offer at 5% below it. The
   seller answers with a final offer, and a second counter would be refused
   with `ROUND_LIMIT`.
5. **Accept.** The buyer accepts with the offer's hash, the mandate handle and
   the idempotency key `accept-<offer_id>`, then withdraws the other thread.
6. **Fulfilment.** `await_order` polls `get_order` while the seller agent
   confirms, ships and delivers the order.
7. **Settlement.** The buyer pays the market the order total through Town's
   ledger, and Trajano's database is audited.

## Reproduce

Run from the root of this checkout. You need Town at base commit `6f9a7c0`,
Trajano at `26d78f6c1f02c888e38ae48fe898e448f4aefcaa`, Python 3.12, and
[uv](https://docs.astral.sh/uv/) for Trajano's environment. No model key is
needed.

```bash
git clone https://github.com/augustoal/trajano.git ../trajano
git -C ../trajano checkout 26d78f6c1f02c888e38ae48fe898e448f4aefcaa
(cd ../trajano && uv sync)

python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]' -r examples/integrations/trajano/requirements.lock

.venv/bin/python examples/integrations/trajano/run.py --project ../trajano --case deal --out /tmp/trajano-town
.venv/bin/python examples/integrations/trajano/run.py --project ../trajano --case lost-accept --out /tmp/trajano-town
.venv/bin/python examples/integrations/trajano/run.py --project ../trajano --case over-mandate --out /tmp/trajano-town
.venv/bin/python examples/integrations/trajano/run.py --verify examples/integrations/trajano/evidence
.venv/bin/python -m pytest -q tests/test_example_trajano.py
```

`run.py` exits with 0 only when every stage passed, with 1 otherwise, and with 2
when the project checkout or its `.venv` is missing. Each case takes about four
seconds. Use `--verify` rather than plain `nandatown verify`, because
`nandatown verify` does not load the scenario's plugin, and the plugin holds
the validators the replay needs.

## Results

These are the recorded bundles in `evidence/`, with Town evaluator `lab-0.2.6`
and seed 42.

| Case | Expected | Observed | Bundle fingerprint |
| --- | --- | --- | --- |
| `deal` | Market found in the registry, two offers, one counter answered with a final offer, one order delivered, the other thread withdrawn, one payment, database agrees | **passed**, 11/11 stages. Counter at `0.1097`, accepted at once. Order 1 at `76.00` against a list optimum of `79.07` | `sha256:24a9989a264e74cf1290bbf7b52c563b752d9366ad32c30bd2b9a710c7e4857f` |
| `lost-accept` | Accept reply lost; buyer retries once with the same key; Trajano replays the first answer; exactly one order and one payment | **passed**, 9/9 stages. Both calls used `accept-3` and got the identical result, order 1. The database holds one order | `sha256:1c572d21bd08fa4b7253d33fa4c09806cd729489f19b8656b32151c773761a59` |
| `over-mandate` | A buyer that skips its own budget check accepts an offer above its cap; Trajano refuses; no order, no payment | **passed**, 9/9 stages. `MANDATE_INVALID` ("mandate caps the unit price at 0.1000"), `retryable: false`, a `refused` message on the thread, both threads withdrawn, no order | `sha256:be27ffcbc4fd77fa724dd95636d0dc6fb96f6d3d9f3315d3f5b8728859aef8dc` |
| No project | `run.py` refuses to start | Exits with code 2, no run (tested) | not applicable |
| Project present but not running | Nothing is judged as passing | **incomplete**: every judged stage `not_enough_evidence` (tested) | not applicable |

### Failure case: a lost accept reply

Town drops the market's first `accept_offer_result`. The order exists in
Trajano, but the buyer never hears about it. The buyer records `unknown` rather
than guessing. After 2 logical seconds it sends the same `accept_offer` again,
with the same offer, hash and idempotency key. Trajano stores the response to
every write under its key, so it returns the original result instead of
running the accept a second time.

The buyer then learns order 1, and the audit confirms one order, one delivery
and one payment. The validator requires that both accept calls used one key
and got identical results. The tests check the opposite as well: if the retry
had produced a different order, the run fails.

### Boundary case: a careless buyer over its mandate

The mandate in this run caps the unit price at `0.1000`, below every seller's
price. The buyer is configured with `check_mandate: false`, so it shortlists,
negotiates and accepts as if it had no cap. Trajano's check at `accept` is the
only guard left. It refuses with a structured, non-retryable error and writes
a system `refused` message on the thread. The buyer reads both, does not
retry, and withdraws. The audit shows no order and Town's ledger shows no
payment.

## What this shows

- Trajano works end to end as a subject in Town. The run covers discovery
  through Town's registry, eight buyer tools over a real MCP session,
  asynchronous replies from Trajano's own seller agents, the 2-round negotiation
  protocol, a full order lifecycle, and settlement in Town's ledger.
- What the buyer believed about its order matched Trajano's database in all
  three runs.
- Retrying with an idempotency key made a lost reply safe: there was no double
  order, and the buyer recovered the order it had placed.
- The spending limit holds against a buyer that ignores it, because the server
  enforces it rather than the agent.

## Limits it exposed

- **Time.** Town's clock does not move while the adapter calls Trajano, but
  Trajano's sellers answer in real seconds. So `await_inbox` and `await_order`
  poll in wall-clock time, for at most 20 seconds per request, and record the
  number of polls. In these runs that was 2 polls for each wait for offers and
  4 for each order. Trajano's own clock is frozen at the task's morning, so
  offer expiry is not exercised.
- **`get_order` has no amount.** `OrderView` has no total, so the buyer pays
  the total of the offer it accepted, which Trajano's audit then confirms. An
  agent that lost its own state could not settle from `get_order` alone.
- **A refusal does not close the thread.** After `MANDATE_INVALID` the thread
  stays `OFFERED`, with the refusal as its last message. A buyer that reads only
  the thread status would not see that its accept was refused.

## Limitations and what remains untested

- The buyer is a Town script, and the sellers are Trajano's own deterministic
  rule agents. Neither is an independently developed agent, and no language
  model is called. Trajano's LLM buyer (`agents/llm_agent.py`) is not wired into
  Town.
- Only Trajano's buyer server is reached from Town. The seller server is used
  by Trajano's own seller agents inside the launched world, so a Town-scripted
  seller is untested.
- The audit is written by Trajano from its own database, and the validators read
  what the adapter recorded. Neither is independent proof of what happened
  inside the server.
- Live runs are not byte-identical: run ids, poll counts and timestamps change.
  The checked assertions repeat.
- Untested: A2A, discovery outside Town's registry, server crash and restart,
  concurrent buyers, duplicated or delayed requests (only a dropped reply is
  tested), a retry with a different key, offer expiry, stock races, and real
  payment rails.
