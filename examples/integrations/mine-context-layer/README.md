# Mine Context Layer in Nanda Town

[Mine Context Layer](https://github.com/jsanti00/mine-context-layer) is an MCP server that holds the shared context of one underground-mine shift: plan, fleet, operating rules, events with provenance (source, validity, confidence, link to the original record), proposals, and a human decision log. Agents read and write it through eight MCP tools, with permissions by role: source agents (radio, CMMS) only publish events from their own system, function agents (maintenance, planning) read everything and propose, and only a human approves. Its code and synthetic mine data stay in its own repository.

This example runs one shift incident against that server from Town Lab scenarios: scoop LH-07 loses hydraulic pressure at stope TP-1310-04, and the system must propose which loader to move. The answer key is the project's own (incident INC-01: move LH-11 from TP-1250-02).

## How it connects

| Part | What it is |
| --- | --- |
| `mcl_layer` | Adapter role and the only role that talks MCP. It opens one MCP stdio session per Town participant, each with that participant's layer identity and role, and turns Town messages into real tool calls. Town agents without a layer identity are refused. |
| `mcl_radio`, `mcl_cmms` | Town scripts with source identities. Radio publishes the failure as spoken ("scoop 7", "1310-04"); CMMS adds the repair history, or nothing if the CMMS is down. |
| `mcl_maintenance`, `mcl_planning` | Town scripts with function identities. Maintenance estimates the repair (and tries once to publish an event, which must be denied); planning applies the site's dispatch rules and proposes. |
| `mcl_supervisor` | The human. Approves through the project's own non-MCP command (`python -m mcl.session decide`), because no MCP tool can approve. |
| `mcl_intruder` | A Town agent with no layer identity that tries to read the shift plan. |
| `mcl_agent` | Speaks for one of the project's four real agents (headless Claude Code, connected only to the layer over MCP) in the `agents` case. |
| `mcp_bridge.py` | One live MCP stdio session for the synchronous Lab engine, adapted from the Open Trading Desk example (#322). |
| `mcl_plugin.py` | The roles and the validators. |

Turn-taking ("the failure is published", "the assessment is ready") travels as Town messages between roles. All shared state lives in the layer. Validators count only records written by the adapter: real MCP results, the decision write, and a final audit read back through MCP.

`nandatown mcp test` stops at the handshake and tool list, and `test-agent` profiles judge price quotes, so neither can judge this workflow. A Lab scenario adds attributed traces, a non-participant, and an evidence bundle.

## Reproduce

Run from the root of this repository, with Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/jsanti00/mine-context-layer.git ../mine-context-layer
git -C ../mine-context-layer checkout 4b3c9e017f1e85db55e251a21efd65f090847af5
(cd ../mine-context-layer && uv sync)
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]' -r examples/integrations/mine-context-layer/requirements.lock
.venv/bin/python examples/integrations/mine-context-layer/run.py --project ../mine-context-layer --case workflow  --out /tmp/mcl-town
.venv/bin/python examples/integrations/mine-context-layer/run.py --project ../mine-context-layer --case cmms-down --out /tmp/mcl-town
.venv/bin/python examples/integrations/mine-context-layer/run.py --verify examples/integrations/mine-context-layer/evidence
.venv/bin/python -m pytest -q tests/test_example_mine_context_layer.py
```

The `workflow` and `cmms-down` cases need no model and are deterministic. The `agents` case runs the project's real agents and needs [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged in:

```bash
.venv/bin/python examples/integrations/mine-context-layer/run.py --project ../mine-context-layer --case agents --out /tmp/mcl-town
```

`run.py` exits nonzero unless every stage passed. Each run uses a fresh shift database. `--verify` needs no project, and is needed because `nandatown verify` does not load scenario plugins.

Recorded evidence: Town `6f9a7c0`, project `4b3c9e0`, evaluator `lab-0.2.6`, seed 42, adapter hash in the `layer_connected` event.

## Expected and observed

| Stage | Expected | `workflow` | `cmms_down` | `agents` |
| --- | --- | --- | --- | --- |
| `connection` | Live MCP sessions exposing the eight project tools, from unmodified commit `4b3c9e0` | Passed | Passed | Passed |
| `id_resolution` | Spoken "scoop 7" / "1310-04" resolved by the layer to LH-07 at TP-1310-04 | Passed | Passed | Passed |
| `provenance` | The CMMS record links to its work orders (`cmms://OT-...`) and the assessment cites it | Passed | Not in this case | Not in this case |
| `permission_boundary` | A function agent's publish is denied and logged by the layer; a Town agent without a layer identity is refused | Passed | Not in this case | Not in this case |
| `degraded_source` | CMMS down: no CMMS record appears; the estimate names what is missing, with a wider range and confidence below 0.7 | Not in this case | Passed: 2 to 8 h, confidence 0.5 | Not in this case |
| `action_matches_key` | Move LH-11 from TP-1250-02 to TP-1310-04, citing the failure event and the assessment | Passed | Passed, with missing info carried over | Passed |
| `real_agents` | All four real agents finish, call only layer tools, and get no permission denials | Not in this case | Not in this case | Passed: 13 layer tool calls, 64 s, about USD 0.16 of model use |
| `human_decision` | The supervisor's approval is in the layer's decision log, written outside MCP | Passed | Passed | Passed |

## Failure and boundary cases

| Case | What happens | Observed |
| --- | --- | --- |
| Function agent writes as a source | Maintenance calls `publish_event` | The layer returns `permission_denied`, logs the call as failed, and no event by that agent exists |
| Agent with no layer identity | `visitor` asks for `get_plan_slice` | The adapter refuses it (`not_a_layer_participant`); the layer is never called |
| A source system is down | The CMMS is marked down before the incident | The CMMS connector publishes nothing; maintenance widens its range and lowers confidence; planning still proposes the right move and carries the missing information to the human |

The tests also damage the recorded evidence (wrong loader, an allowed publish, a missing refusal, a record written by a script role, a decision by someone other than the supervisor, a modified project, a confident estimate without CMMS, a real agent using other tools) and check that the affected stage stops passing.

## What this shows, and what it does not

- **Shown:** the layer works as a shared state service for agents it did not ship with. Town participants with different identities read and write the same shift through the MCP standard, permissions hold per identity, provenance survives the round trip, and the human approval path stays outside MCP.
- **Shown, agents case:** the project's own four agents run the same incident with Town carrying their turn-taking, and reach the answer key.
- **Not shown:** adoption by independently developed agents. Radio, CMMS, maintenance and planning are integration scripts in the scripted cases; the planning script applies the site rules mechanically.
- **Not shown:** robustness to message loss between roles, more than one incident, contradictions between sources, or several shifts. Layer identity comes from how the adapter starts each session (environment variables), so Town's signed identity maps to a layer role only through this adapter; the layer itself has no authentication.
- The `agents` case uses a model, so results can vary between runs; one recorded run is one observation.
