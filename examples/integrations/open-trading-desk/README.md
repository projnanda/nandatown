# Open Trading Desk in Nanda Town

[Open Trading Desk](https://github.com/Idop93/open-trading-desk) is an MCP server with three shared services for trading agents: Signal Exchange (provider registry and hash-chained view log), Scorekeeper (provider track records), and Risk Gate (the only path to a simulated broker). Its code stays in its own repository.

This example runs one simulated trading day against that server from a Town Lab scenario, and tests what happens when an order confirmation is lost.

## How it connects

| Part | What it is |
| --- | --- |
| `otd_desk` | Adapter role and the only role that talks MCP. It turns Town messages into real tool calls on the server, which runs as a separate process. |
| `otd_owner` | Town script. Grants the delegation and later revokes it. |
| `otd_pm` | Town script. Finds providers, collects views, decides with the project's own rule, and sends orders. |
| `mcp_bridge.py` | Holds one live MCP stdio session for the synchronous Lab engine. |
| `otd_plugin.py` | The three roles and the validators. |

The MCP server is the only independently developed participant. Owner and PM are integration scripts. Real delegation tokens stay inside the desk; Town messages and evidence hold only handles.

Town's `test-agent --url` profiles judge price quotes and `nandatown mcp test` stops at the handshake and tool descriptions, so neither can judge a trading workflow. A Lab scenario adds Town's fault injection, attributed trace, and evidence bundle.

## Reproduce

Run from the root of this repository, with Python 3.11.

```bash
git clone https://github.com/Idop93/open-trading-desk.git ../open-trading-desk
git -C ../open-trading-desk checkout 7c9fc6b52742700a9b8c5680406779b8da0f5124
python3.11 -m venv ../open-trading-desk/.venv
../open-trading-desk/.venv/bin/python -m pip install -r examples/integrations/open-trading-desk/requirements-project.lock
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]' -r examples/integrations/open-trading-desk/requirements.lock
.venv/bin/python examples/integrations/open-trading-desk/run.py --project ../open-trading-desk --case clean --out /tmp/otd-town
.venv/bin/python examples/integrations/open-trading-desk/run.py --project ../open-trading-desk --case lost-reply --out /tmp/otd-town
.venv/bin/python examples/integrations/open-trading-desk/run.py --verify examples/integrations/open-trading-desk/evidence
.venv/bin/python -m pytest -q tests/test_example_open_trading_desk.py
```

`run.py` exits nonzero unless every required stage passed. No model key is needed: the key is removed from the environment and `claude-reasoner-01` uses its deterministic fallback. Each run uses a fresh data directory. `--verify` needs no project, and is needed because `nandatown verify` does not load scenario plugins.

Recorded evidence: Town `6f9a7c0`, project `7c9fc6b`, evaluator `lab-0.2.6`, validator `otd-3`, seed 42, adapter hash in the `desk_connected` event.

## Expected and observed

| Stage | Expected | Clean | Lost reply |
| --- | --- | --- | --- |
| `connection` | Live MCP session exposing the seven project tools, from unmodified commit `7c9fc6b` | Passed | Passed |
| `discovery_and_views` | The three expected providers found; each asked once per round up to the decision, with the reply delivered | Passed | Passed |
| `decision_and_fill` | Decision follows delivered views and the project's rule; one approved fill | Passed: NVDA, 7.2% at minute 65 | Passed |
| `risk_refusals` | Oversized, short, and revoked orders refused with the right rule | Passed | Passed |
| `scoring_and_chain` | Hash chain verifies; every provider asked once for every ticker and checkpoint (45 views); scores cover exactly the views given | Passed: 45 entries | Passed: 45 entries |
| `pm_knowledge` | PM knows only what delivered messages told it | Passed | Passed |
| `fault_and_retry` | Reply lost, same order retried and refused, one fill | Not in this case | Passed |

## The failure case

Town drops the first `order_result`. The order filled, but the PM never hears.

| Step | Observed |
| --- | --- |
| Reply deadline passes | PM records `unknown` and retries once with the same arguments |
| Risk Gate on the retry | Refuses with `DUPLICATE_POSITION` |
| PM after the refusal | Stays `unknown`: the rule says a position exists in this ticker, not that this order filled |
| `close_day` order history | One approved NVDA fill and one refused retry; the PM records `filled` |

**What this shows:** no double fill occurred, and the PM did not claim knowledge it never received.

**The limit it exposes:** `place_order` has no client order id and the server has no order-status tool, so the PM cannot learn the outcome until the end of the day. MCP has no standard idempotency key, although tool arguments could carry one.

If the project folder is missing, `run.py` stops with exit code 2 before any run. If a scenario runs but cannot connect to the project, every judged stage is `not_enough_evidence` and the run does not pass. `tests/test_example_open_trading_desk.py` checks both without the project, verifies the recorded bundles, and confirms that damaged evidence stops the relevant stage from passing. Each stage reads only what the role named in the scenario recorded and received. If the PM workflow raises an error, the run records `pm_halted`, still writes a bundle, and does not pass.

## Limits

- The market is a seeded random walk and the broker is simulated.
- No model is called.
- The PM expects one reply per request. A reply delayed past the retry window is recorded as late and ignored, so a delay fault is not covered. Duplicated control messages are also not covered.
- Scores do not show skill. Hit rates were 73% to 92% on a random walk because the server's default `first_touch` rule is lenient on AVOID views and expired views leave the denominator. An opt-in `direction` rule exists in the project and is not active here.
- Delegation can be issued by any MCP client in this demo, so the separation between owner and PM is choreography, not security.
- Town's fixtures are scripts. This is not independent adoption or full MCP conformance.
- The validators read what the desk recorded. They are not independent proof of what happened inside the server.
- Live runs are not byte-identical, because timestamps and hashes change. The checked assertions repeat.

Not tested: live models, markets, or brokers; outside provider agents; A2A; server crash and restart; concurrent runs; retry safety beyond the case above.
