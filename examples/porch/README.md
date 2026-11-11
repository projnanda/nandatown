# Porch in NANDA Town

Porch is a permissioned gateway between AI agents and a connected home.
Agents connect to it over MCP, each with its own key. Porch checks every
request against that agent's scopes and the home's safety rules, dry-runs
it, sends it through its Device Hub (LG ThinQ, Matter, Google Home, or
simulated copies), reads the device back, and writes the decision to an
audit log. Risky actions wait for the household owner.

Porch runs outside Town. This folder holds only what Town needs to test it:

| File | What it is |
|---|---|
| `porch_town.py` | Lab plugin: the adapter (a Town agent that forwards signed Town messages to Porch over MCP), the scripted agents, and the `porch_home` validator |
| `porch_home.yaml` | The end-to-end scenario |
| `porch_home_no_dedup.yaml` | Negative control: fails on purpose |
| `evidence/` | The bundle recorded from the live run reported below |
| `../../tests/test_porch_integration.py` | Tests: without Porch, against a stand-in, and against a live Porch |

## What is connected

```
energy-agent, concierge-agent, stranger, impersonator     (Town agents)
   | signed Town messages: town index, auth, transport with a duplicate fault
   v
porch          the adapter, a Town agent with a card in the town index
   | MCP over streamable HTTP, one Porch key per Town agent
   v
Porch gateway  scopes, safety rules, approvals, dry run, verify, audit log
   | HTTP
   v
Porch Device Hub -> devices (simulated by the hub in this test)

owner    -> Porch owner API: who is home, approvals, the audit log
auditor  -> Device Hub: reads device state directly, not Porch's answer
```

The interface under test is Porch's agent interface (the MCP tools
`describe_home` and `request_action`) used by several Town agents at once,
plus Porch's owner API. Town supplies discovery, sender identity, delivery
with a fault, and the evidence bundle.

## Run it

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) (Porch's
start script uses it).

**1. Start Porch** from a checkout of
<https://github.com/yooncham/porch-device-hub-yc> (a private repository;
ask the team for read access):

```bash
porch_gateway/start.sh        # hub on :8100, gateway on :8000, devices simulated
```

In a second terminal, put the home in its starting state:

```bash
porch_gateway/.venv/bin/python porch_gateway/owner.py reset
```

**2. Run the scenario** from this repository:

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'
export PORCH_CONFIG=/path/to/porch-device-hub-yc/porch_gateway/porch_config.json
nandatown run examples/porch/porch_home.yaml
```

Expect `Verdict: PASSED` and exit code 0. Then, with the bundle directory
the run printed:

```bash
python examples/porch/porch_town.py table runs/<run id>     # expected and observed, per step
python examples/porch/porch_town.py verify runs/<run id>    # hashes, then replays the validator
```

`PORCH_CONFIG` is Porch's own config file. The adapter reads the agent keys
and the owner key from it and keeps them in memory; none is written to a
scenario, a message, an event or the bundle. `PORCH_MCP_URL` and
`PORCH_HUB_URL` override the addresses.

**3. Run the failure cases:**

```bash
nandatown run examples/porch/porch_home_no_dedup.yaml               # FAILS on purpose
nandatown run examples/porch/porch_home.yaml --layer auth=plain.v1  # FAILS on purpose
PORCH_MCP_URL=http://127.0.0.1:9/mcp PORCH_HUB_URL=http://127.0.0.1:9 \
  nandatown run examples/porch/porch_home.yaml                      # Porch unreachable: FAILS
```

Run `owner.py reset` again after the second one; it leaves the lamp on.

**4. Run the tests:**

```bash
python -m pytest -q tests/test_porch_integration.py
```

Seven tests run without Porch. Two more run when `PORCH_CONFIG` is set and
Porch is up.

## The workflow

| Agent | What it does | What must happen |
|---|---|---|
| all three home agents | look up `home.act` in the town index | they find the adapter's verified card |
| energy-agent | `describe_home` | 8 devices on 3 platforms |
| energy-agent | lamp off, dryer delay 6 h | allowed, executed, verified by Porch; hub shows the new state |
| energy-agent | dryer delay 20 h | denied (household limit is 12 h); dryer still at 6 |
| energy-agent | unlock the front door | denied (outside its scope); door locked |
| concierge-agent | heater on, nobody home | denied (safety rule); heater off |
| concierge-agent | unlock the front door | held for the owner; door locked |
| owner | sees the held request in Porch, approves it | approval recorded |
| concierge-agent | retries with the approval id | allowed, executed, verified; door unlocked |
| concierge-agent | uses the same approval again | denied (an approval works once) |
| concierge-agent | lock the door | allowed, verified; door locked |
| stranger (no Porch key) | `describe_home`, lamp on | Porch refuses both; lamp unchanged |
| impersonator | lamp on, claiming to be energy-agent, signed with its own key | Town refuses delivery; Porch never sees it; lamp unchanged |
| Town transport | delivers energy-agent's first request twice | Porch's audit log holds one entry for it |
| owner | reads Porch's audit log at the end | every adapter decision has exactly one matching entry |

The validator reads only the recorded events. Decisions come from the
adapter's record of Porch's answers, device state from the auditor's reads
of the hub, and the count of executions from Porch's own audit log.

## Result of the recorded run

| | |
|---|---|
| Town | `main` at `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`, nandatown 0.2.0, evaluator `lab-0.2.6`, Python 3.12.3 |
| Porch | `yooncham/porch-device-hub-yc` at `834f441a2eba6dce2c8f1b7c6b25cb9001afa0c6` (gateway and Device Hub 0.1.0 in one tree), fastmcp 4.1.4, simulated devices. Its `main`, `64a9a4ab84d484f1e5e0523b3120d9e352c88b76`, is the merge of that commit and has the same tree |
| Adapter | `porch-town-adapter/0.1.0` (this folder) |
| Run | `sim-a5abadb5758a`, 2026-11-11 00:31 UTC, 178 events, 65 intents |
| Bundle | `evidence/sim-a5abadb5758a`, fingerprint `sha256:e3b272a089821f838ccfabf663489444e76059267bee1f2b43075c3020a58f37` |
| Verdict | PASSED |
| Repeated | 2026-11-11, from fresh clones of this branch and of Porch `main`, on macOS with Python 3.13.15 and 3.11.15: PASSED, with the same step outcomes, 178 events and 65 intents |

| Agent | Step | Expected | Observed | Hub state expected | Hub state observed |
|---|---|---|---|---|---|
| energy-agent | describe | described | described | | |
| energy-agent | lamp_off | allowed | allowed | OnOff false | OnOff false |
| energy-agent | dryer_delay_6h | allowed | allowed | delay 6 | delay 6 |
| energy-agent | dryer_delay_20h | denied | denied | delay 6 | delay 6 |
| energy-agent | unlock_door | denied | denied | Locked | Locked |
| concierge-agent | describe | described | described | | |
| concierge-agent | heater_on | denied | denied | OnOff false | OnOff false |
| concierge-agent | unlock_door | held_for_owner_approval | held_for_owner_approval | Locked | Locked |
| concierge-agent | unlock_door_approved | allowed | allowed | Unlocked | Unlocked |
| concierge-agent | unlock_door_again | denied | denied | Unlocked | Unlocked |
| concierge-agent | lock_door | allowed | allowed | Locked | Locked |
| stranger | describe | rejected | rejected | | |
| stranger | lamp_on | rejected | rejected | OnOff false | OnOff false |
| impersonator | forged_lamp_on | not_delivered | not_delivered | OnOff false | OnOff false |

| Stage | Status | Note from the run |
|---|---|---|
| gateway_reachable | passed | Porch answered 13 MCP calls |
| discovery | passed | 3 agents found the gateway's verified card |
| home_described | passed | describe_home matched the expected home |
| decisions_match_policy | passed | 9 of 9 steps got the expected decision |
| hub_state_confirmed | passed | 11 of 11 hub reads matched the expected state |
| owner_approval | passed | held, approved by the owner through Porch, retry executed and verified |
| unknown_agent_rejected | passed | Porch refused both calls from the agent with no key |
| impersonation_blocked | passed | failed Town's signature check, never reached Porch, device unchanged |
| duplicate_applied_once | passed | delivered twice by Town, one entry in Porch's audit log |
| audit_complete | passed | 9 Porch audit entries match 9 adapter decisions |
| ledger_conserved | passed | no money moved |
| privacy | not tested | the scenario declares no redacted fields |

Attribute names are shortened in the step table; `porch_town.py table`
prints them in full. Porch reported 61 ms for each of the three Matter
commands and 1,153 ms for the LG ThinQ dryer command. The devices are
simulated, so these are not hardware measurements.

Check the recorded bundle without running Porch:

```bash
python examples/porch/porch_town.py verify examples/porch/evidence/sim-a5abadb5758a
python examples/porch/porch_town.py table examples/porch/evidence/sim-a5abadb5758a
```

## Failure and boundary cases

All four were run against the same live Porch.

| Case | Command | Expected | Observed |
|---|---|---|---|
| Town delivers a request twice | `porch_home.yaml` (the `duplicate` fault) | the device is commanded once | passed: one audit entry for the duplicated request |
| The same, with the adapter's duplicate guard off | `porch_home_no_dedup.yaml` | fails | FAILED at `duplicate_applied_once`: "request m-3 was delivered twice and Porch's audit log holds 2 entries for it". Every other stage passed |
| Forged sender, Town auth swapped for `plain.v1` | `porch_home.yaml --layer auth=plain.v1` | fails | FAILED at `impersonation_blocked` and `hub_state_confirmed`: the forged request ran under energy-agent's key and the hub showed the lamp on |
| Porch not running | `PORCH_MCP_URL=http://127.0.0.1:9/mcp ...` | never a pass, nothing retried | FAILED at `gateway_reachable` ("ConnectError: [Errno 111] Connection refused"); the eight Porch stages inconclusive; each agent stopped after one call and sent no action request |

## What this shows

- Porch's agent interface works for a population of Town agents, not only
  for one MCP client: per-agent scopes, household limits, the safety rule,
  owner approval and single-use approvals all held when the requests
  arrived as Town messages.
- Town's index is enough for an agent to find the gateway, and Town's auth
  layer is what tells the adapter who is asking. The adapter holds the
  per-agent Porch keys, so Porch's scopes are only as strong as that
  signature check. The `plain.v1` run shows it: without the check, one
  agent acted with another agent's scope.
- Porch's `request_action` has no request id. Town delivers at least once,
  so the same request can arrive twice, and Porch then executes it twice.
  The adapter absorbs duplicates by Town message id; the control shows
  what happens without that. A request id in Porch itself is the proper
  fix; it is not in the version tested here.
- An unknown key is refused per tool call, not at connection: `initialize`
  and `tools/list` still answer it with the tool names.
- `nandatown verify` cannot replay a validator that lives in a plugin file,
  because it does not load plugin code. It reports "evaluator replay
  mismatch" for these bundles. `porch_town.py verify` loads the plugin and
  runs the same check.

## What is not tested

- Physical devices and the vendor clouds. The hub simulated all 8 devices.
- A model-driven agent. The Town agents here are scripted. Porch's own
  demos drive it from Hermes agents running Claude, directly over MCP;
  that path does not go through Town.
- Track and Path. Track's task is fixed to the quote, and Porch is not an
  A2A agent, so neither mode fits without a new task type.
- Porch's water-leak emergency rule, approval expiry (10 minutes), and the
  owner declining a request against a live Porch (covered against the
  stand-in only).
- Two agents acting at the same moment; the agents here take turns.
- An adapter restart. Its duplicate guard is in memory.
- The hub failing while the gateway is up.
- Repeatability of Porch's side. Town's side is seeded; Porch's approval
  ids, audit ids and latencies differ from run to run.
- Payments, trust, negotiation and memory layers, and the NANDA Index.

The stand-in used by the tests (`FakePorch`) is a Town fixture. Its passes
show that the adapter and validator behave as written. Only the live run
is evidence about Porch.
