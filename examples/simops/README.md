# SimOps AI in Nanda Town

SimOps AI is a simulation environment and coordination layer for construction AI agents. Each trade's
agent negotiates one shared schedule. A neutral referee checks every move against site rules (zones, the
crane, crew limits, trade order) and commits only what every affected trade signed. SimOps runs as its own
service with an MCP endpoint. Agents join a seat with a bearer token, and the token decides the seat.

This folder plugs SimOps into the Lab. Five Town trade agents find a SimOps desk in Town's registry and
play Building 01, Week 9 of a data center job through it. They read the board and their private briefs,
propose work, get refused, agree a swap over Town messages, sign it, and the week closes. SimOps's own
audit is recorded at the end. Then we break things on purpose. Calls arrive twice. An offer arrives after
the week closed. A rogue speaks for Electrical. The auth layer gets swapped for one that checks nothing.
SimOps goes down.

Everything here is Town-side: a plugin with roles and a validator, a small MCP client, scenarios, a
check script, recorded evidence and one test. SimOps itself stays in its own (private) repository and
runs elsewhere.

## How it connects

```
 Town Lab (this folder)                                             SimOps AI (runs elsewhere)
 ┌───────────────────────────────────────────────────────┐          ┌──────────────────────────────┐
 │ civil  concrete  steel  mechanical  electrical  rogue │          │ /mcp      seat tools         │
 │    │       │       │        │           │        │    │          │ referee   rules, alternatives│
 │    └───────┴── Town messages (registry, auth, ────┘   │          │ board     threads, signatures│
 │                transport faults, redaction)           │          │ event log replayable         │
 │                         │                             │   MCP    │ verify    audit, I1-I5       │
 │                   simops-desk ────────────────────────┼─────────▶│                              │
 │        card: construction.schedule.referee            │  bearer  │ /api/playground  fresh run,  │
 │        records every answer as a Town event           │  seat    │   close week, read audit     │
 └───────────────────────────────────────────────────────┘  token   └──────────────────────────────┘
```

- **Discovery.** The desk publishes a signed card with capability `construction.schedule.referee`. Each
  trade looks it up in Town's registry before it calls anything.
- **Calls.** A trade sends `call.<tool>` to the desk as a Town message, with its own seat token in the
  body. The desk calls SimOps over MCP with that token and replies `simops_result`. So every SimOps call
  goes through Town's auth, transport and privacy layers.
- **Seats.** Before the run, the construction manager's side (`simops_link.Site`) opens a fresh SimOps
  run and hands each trade its own token out of band. This is how SimOps invites outside agents. The
  desk never holds the tokens, except in the control case that is built to fail.
- **Swaps between trades** are Town messages (`swap_offer`, `swap_accepted`, `swap_signed`). The
  thread, the signatures and the commit live in SimOps.
- **Evidence.** The desk records every SimOps answer as an attributed `simops_call` event: outcome,
  reason, rules, thread, signatures and board version. It also records `week_closed`, `week_board` and
  `simops_verified` (SimOps's audit and log digest). The validator judges the run from these events
  alone. `seat_token` and `brief` are declared private, so tokens and brief bodies never leave the run.

The interfaces under test are Town messages between trade agents and the desk, and the desk's MCP calls
to SimOps with each trade's bearer token. The playground REST routes are the harness: they open a run,
close the week and read the audit.

The trade agents follow the same rules as SimOps's own rule bot. The desk, the trades and the rogue were
written by us for this test. They are not independent agents adopting SimOps.

## Reproduce

SimOps is hosted for this check at `https://simops-production.up.railway.app`. It plays rule bots only,
holds no model key, and its playground gives every run fresh seat tokens.

```bash
# 1. Town at this branch
git clone https://github.com/iggyops/nandatown -b integration/simops-week9 && cd nandatown
python3 -m venv .venv && source .venv/bin/activate && python -m pip install -e '.[dev]'

# 2. Point at SimOps
export SIMOPS_URL=https://simops-production.up.railway.app

# 3. The check: every case against a fresh SimOps run, both logs cross-checked, evidence rewritten
python examples/simops/run_check.py --out /tmp/simops-evidence

# 4. One case by hand, then Town's own tools on its bundle
nandatown run examples/simops/simops_week9.yaml --out /tmp/town-lab
nandatown replay /tmp/town-lab/<run id> --kind simops_call
nandatown visualize /tmp/town-lab/<run id>
nandatown compare examples/simops/simops_week9_forged.yaml --swap auth=plain.v1

# 5. The test, and offline verification of the recorded evidence (no SimOps, no network)
python -m pytest -q tests/test_simops_integration.py
python examples/simops/run_check.py --verify examples/simops/evidence
```

`nandatown compare` and `nandatown run` need `SIMOPS_URL` too. The comparison swaps Town's auth for `plain.v1` and changes no stage verdict: with Town's check gone, SimOps still refuses the forged calls. The optional `live_seat` case runs only
when `OPENAI_API_KEY` is set. It costs well under a cent.

Versions in the recorded evidence (`evidence/summary.json` and each `check.json`):

| | |
|---|---|
| Town | `6f9a7c0` (projnanda/nandatown main), nandatown 0.2.0, Lab evaluator `lab-0.2.6`, Python 3.12 |
| SimOps | 0.1.0, build `hw8-town@26aa2b8`, image `sha256:c7671aaf8b3632b267d09502ecbe5ad118309bba93ff707eb8c8e7e1f6ae1599` |
| Model (live_seat only) | OpenAI `gpt-6-luna`, a mutable dependency |

The clean case is deterministic end to end. With the same SimOps build, a rerun logs the same SimOps calls
with the same results, so the SimOps log digest matches the recorded one
(`sha256:d54f73994b73…`). The live test checks this.

## Cases and results

`run_check.py` writes the expectation for each case before it runs, and compares it with what Town and
SimOps recorded.

| Case | Town setting | Expected | Observed |
|---|---|---|---|
| `clean` | hmac auth, no faults | Every stage passes. Electrical's Hall 2 clash is refused, the swap with Mechanical is signed by both, nothing unscheduled, SimOps audit clean | **passed**. 28 SimOps calls, 1 refusal (`zone_exclusive`), 1 commit signed by both, I1-I5 hold |
| `duplicate` | transport duplicates `accept_swap` and both `commit_week` calls | Each changes SimOps once. Repeats are refused, with no second commit | **passed**. 3 repeated deliveries, refused as `not_open`, `already_signed`, `not_agreed`. 1 commit |
| `late_accept` | `swap_offer` delayed 10 s, past the week's close at t=8 | The late accept is refused and the board does not change. The run reports the rough-in unscheduled instead of claiming success | **failed, as expected**. Accept refused `not_open` (thread expired), `week_committed` failed with `ELE-roughin` unscheduled, `stale_calls_refused` passed, SimOps audit clean |
| `forged` | a rogue sends calls as Electrical with its own key, plus a forged card; hmac auth | Town stops the forged sender before the desk | **passed**. 3 of 3 forged calls `delivery_failed: bad signature`. Forged card unverified |
| `forged_weak_auth` | the same, with `auth: plain.v1` | Town lets it through. SimOps refuses: the rogue has no token for Electrical | **passed**. 3 of 3 reached SimOps, 3 of 3 refused `unauthorized` at the door, board unchanged |
| `trusting_desk` | weak auth, and a desk that picks the token by sender name | **Fails on purpose**: the rogue moves Electrical's task | **failed, as expected**. `forged_seat_refused` failed. `ELE-roughin` moved to days 43-45, after Electrical's crew leaves |
| `simops_down` | `SIMOPS_URL` points at a closed port | Nothing that needs SimOps is credited | **incomplete, as expected**. Discovery passed, every SimOps stage `not_enough_evidence` |
| `chain_C4` | SimOps case C4: a predecessor chain across the yard and Hall 2 | Out-of-order placements refused, alternatives taken, all placed | **passed**. 3 refusals (`zone_exclusive`, `precedence`), 3 commits |
| `crane_C3` | SimOps case C3: crane and crew limit | Refused, alternative taken, all placed | **passed**. 1 refusal (`crew_limit`), 2 commits |
| `live_seat` | Electrical decided by `gpt-6-luna`, the rest by rules | The week commits through the same desk | **passed**. The model proposed, was refused, opened the swap and signed. 10 model calls, 36k tokens in, about $0.004 |

In every case that reached SimOps, the cross-check agrees. Every call the desk recorded is in SimOps's own
log of that run, in the same order with the same outcome. Calls refused at the door (no valid token) are
not in its log. The audit digest Town recorded is the one SimOps reports. See `evidence/SUMMARY.md` and
`evidence/<case>/check.json`.

## What this shows

- SimOps works as a shared service for agents it did not create. The agents reach it through Town's
  registry and message layers, the desk adds no rules of its own, and the referee's refusals and
  alternatives are enough for rule agents and a model agent to finish the week.
- SimOps is safe under at-least-once delivery for the calls that change state. A repeated accept or
  commit is refused, and nothing applies twice.
- A slow agent cannot change a closed week. The run says the week did not commit. It does not claim
  success.
- Two layers guard the same seat. Town's hmac auth stops a forged sender. With Town's auth switched off,
  SimOps still refuses, because the seat comes from the token and never from a name. The control shows
  the limit: an adapter that maps names to tokens throws that away, and SimOps's own audit still passes
  that run, because every call it saw carried a valid token. So the token has to travel with each
  request, end to end.
- Town never credits a run it could not check. With SimOps down, the stages that need it come back
  inconclusive, not passed.

## Limitations and what is untested

- **`nandatown verify` cannot replay this validator.** It does not load plugin files, so on these
  bundles it reports an evaluator replay mismatch. `run_check.py --verify` loads the plugin, then runs
  Town's own `verify_bundle`. We left Town's core alone. A `verify --plugin` flag would close the gap.
- **The Lab, not the Track.** Track profiles are a two-party quote (buyer and seller), and this
  workflow needs five trades and a referee. Real processes, crashes, leases and fencing are untested here,
  and so are Path/A2A.
- **Our own agents.** The trades, the desk and the rogue are fixtures we wrote. No independent outside
  agent has joined through Town yet.
- **Out-of-band seats.** Tokens are handed to each trade in the same Python process before the run.
  SimOps's real invite, qualifier and approval flow for outside agents is not exercised. Neither are
  Town's portable identity grants.
- **One week.** The graded runs are one-week slices (Week 9, C3, C4). SimOps's 46-week job with weekly
  freezes and construction-manager decisions is not played through Town.
- **Sync calls in a simulated clock.** The desk calls SimOps synchronously inside the Lab's logical time.
  Real network latency is not modelled, so a timeout between Town and SimOps is untested.
- **The model seat** is one run of one model. Its moves can differ between runs.
- **The hosted SimOps** is a small sandbox: runs live in memory, it is rate-limited, and old runs are
  pruned. It is for this check, not production.

## Files

| File | What it is |
|---|---|
| `simops_plugin.py` | Roles `simops-desk`, `simops-trade`, `simops-llm-trade`, `simops-rogue`, and validator `simops_week9` |
| `simops_link.py` | MCP over plain httpx, the playground routes, the per-run `Site`, an OpenAI Responses client for the live seat |
| `simops_week9*.yaml`, `simops_chain.yaml`, `simops_crane.yaml` | The scenarios, one per case |
| `run_check.py` | Plays the cases, cross-checks both logs, writes and verifies `evidence/` |
| `evidence/` | Recorded bundles (`town/`), SimOps's log of each run without result bodies (`simops_log.json`), `check.json`, `summary.json`, `SUMMARY.md` |
| `../../tests/test_simops_integration.py` | Offline evidence check. The live clean run is skipped unless `SIMOPS_URL` is set |

## Bring your own agent (optional)

The hosted playground can seat any agent, not only Town's:

```bash
curl -s -X POST $SIMOPS_URL/api/playground -H 'content-type: application/json' -d '{}'
# -> {"run": ..., "mcp_url": ".../mcp", "seats": {"civil": "sk-simops-...", ...}, "key": "sk-playground-..."}
```

Connect any MCP client to `mcp_url` with `Authorization: Bearer <one seat token>`. Use the `key` with
`GET /api/playground/<run>` for the run's log and SimOps's audit. To put your agent behind Town instead,
give it the `simops-trade` role's job: look up `construction.schedule.referee` and send `call.<tool>`
messages to the desk with your token.
