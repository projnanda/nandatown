# FlexGrid on Town: a demand-flex market day, every node an agent

FlexGrid is a demand-flexibility market for a community of homes and buildings.
Homes and buildings are seller nodes, a coordinator pools what they offer into
one sale, two buyers order demand cuts, and every message is a signed Beckn
envelope. FlexGrid's transport is Town's HTTP mailbox, and it ran live on a
self-hosted `nandatown` 0.2.0 before this integration existed.

This example puts the whole system on a local Town run from current `main`, with
every one of its 11 nodes an agent: 8 homes picked to cover the 200 of the live
community, and 3 buildings. It plays 8 days, 1 normal and 7 unusual, each twice:
once with every node an agent, and once with every node on code's rules, the
control. FlexGrid's code is not here: `FLEXGRID_HOME` points at a separate
checkout, and this folder holds only the adapter, the check and the recorded
evidence.

```
                      Town coordinator (mailbox, leases, fences, event log)
       ^            ^              ^                ^                 ^
       |            |              |                |                 |
    utility   grid-operator   coordinator   8 homes, 3 buildings   statement-reader
    (buyer)      (buyer)      (pools)       (seller agents)        (Town's stock buyer)
```

Each name is its own Town participant with its own session and its own Run
Grant. There are six processes, as on the live system: Town, the coordinator,
each buyer, the homes, the buildings.

## Every node an agent

A model decides for every home and every building at every point of the day:

| Point | What the agent decides | What code's rule would do, shown to the agent |
|---|---|---|
| The offer, the day before | how much of its safe offer to give, and which devices to leave out | offer the planner's safe offer in full |
| A second round, if the pool is short | whether to give more | the same safe offer |
| The commitment | accept or decline the contract | accept: it is inside the offer and safe |
| A building's pre-cool, in the morning | how far to pre-cool, or not at all | the playbook's depth |
| The sale's opening | which devices open, by letter | "If you change nothing, code opens A, C" |
| Every exception in the window | the same, when anything changes: a shortfall, a call, a note | code's own plan for that moment |
| An owner's or manager's note | what the person needs kept | nothing: a rule can't read a note |
| The report | two plain sentences on what happened | none |

Code's rules are one input the agent sees and may take, change or drop. Code
then sizes every kilowatt, and a guard checks every device call against the
person's limits; the guard is a check, not the decider. A rule decides a point
only when the model fails to answer, and each such fallback is recorded and
counted. The agent can only lower code's safe offer, never raise it. The agent picks
the devices, and code sizes each one's kW. The guard holds the safety and money
limits: every device limit, the person's wishes, the owner's minimum price, and no
commitment above the node's own offer. The coordinator and the two buyers are not
agents in these runs: they run the market. The coordinator's own agent exists in
FlexGrid's code but is off here.

An answer that gives less than the rule's amount needs a reason. With none, the
model is asked once more, and if there is still none, the rule's amount goes out
and the receipt says "no reason given". A reason that cites no fact stands, and is
flagged.

FlexGrid's design asks four things of a decision: the coordinator hears only the
amount, every decision leaves a receipt with its reason, no decision goes without a
reason, and the person is told in plain words when a node gives less than asked.
This recording meets them in part:

- **Receipts.** Every decision leaves a receipt in the node's own decision log:
  what was asked, the rule's plan, what the agent chose and, where it gave one, its
  reason. Only an answer below the rule's amount must have a reason. 22 of the 690
  receipts have none: 21 pre-cools at the playbook's full depth and the office's
  reading of its manager's note, whose tools have no reason field; the office's
  offer after that note carries the reason and the note. `facts` holds a fact only
  where the model cited one or a note lowered a sale: 14 of the 690. The raw logs
  hold 26 of 779, because they also keep the network failure day's other play and
  the note read before the reset.
- **The coordinator.** Of its decision the coordinator hears only the amount:
  `offer.agent` carries who decided and which decision, and the agent's reason and
  the person's words stay in the receipt. It still gets code's offer detail (each
  device's kW, battery and car figures, forecasts), one of 12 fixed cause labels
  with each shortfall, and after the sale a report with the model's summary and
  what the person was told. One offer, the office's on the unusual people day, also
  says why it left cooling out.
- **The person.** A message goes to the owner, or a building's manager, only when an
  agent's own answer is below the rule's amount: an offer cut or a commitment
  declined, a running call ended, a plan leaving more uncovered than code's by over
  2% of what the sale owes (at least 0.05 kW), a note's cost, or a shallower pre-cool.
  It says in plain words why its agent gave less than the rule's amount, on their own
  channel and in the day's report. That report goes to the coordinator's store, and
  nothing yet shows it to the person.
- **Less than asked.** A node that simply can't cover what it sold, where code's plan
  leaves the same gap, keeps its reason on the receipt and tells no one. On 53 of 88
  node-days (network failure once) a node measured more than 2% under what it sold,
  and on 49 of them nobody was told. The day's report gives the kWh missed in most
  cases and almost never why.

## The clock waits for the agents

FlexGrid's services each keep a replay clock. Here every clock holds at each
15-minute step until every agent has answered and nothing the market waits on is
still coming: offers, the buyers' contract, commitments, a call's answers,
readings, settlement. Then all of them move one step together. No decision is
skipped because the model was slow, and a day's deadlines are kept by
construction. A held step that moves nothing for 10 minutes goes on, and that is
on the record. `run_check.py` checks that it never happened.

This is a test and demo device. A real evening doesn't wait for anyone.

What happens in a day, an owner's note or a grid emergency, is posted through each
service's own what-if door while every clock waits one step before it, so it lands
on its own step, as in the offline day.

## The workflow under test

The interface is Town's Track mailbox contract: `join` (with a grant),
`messages`, `inbox/notify`, `inbox/claim`, `inbox/ack`, then the run's `events`
and `intents`.

| Step | Who | Town message kind |
|---|---|---|
| Order with deposit | each buyer publishes its signed order when asked | `beckn.discover`, `beckn.on_discover` |
| Call for offers | coordinator to every node | `beckn.discover` |
| Offers | each node's agent to the coordinator: the amount, with code's offer detail (above) | `beckn.on_discover` |
| Clearing and contract with each buyer | coordinator, buyers | `beckn.select`, `init`, `confirm` and their `on_` answers |
| Contracts | coordinator to node, its agent answers | `beckn.confirm`, `beckn.on_confirm` |
| Delivery | nodes run their agents' plans, and say when they fall short | `beckn.rt_capacity` |
| Gaps and calls | the coordinator's desk asks, nodes answer, the desk dispatches | `beckn.rt_ask`, `rt_answer`, `rt_dispatch`; a buyer's call is `beckn.rt_call` |
| Meter readings | node to coordinator and to both buyers, one signed batch each | `beckn.on_status` |
| Settlement | "readings are in", then each buyer's settlement | `beckn.update`, `beckn.on_status` |
| Payment | coordinator to each node | `beckn.on_status` |
| Closing statement | Town's stock buyer asks the coordinator | `quote_request`, `quote_response` |

Control traffic does not go through Town and is not market traffic:
`run_check.py` starts the clock (`/demo/start`), sets the steps it posts at
(`/demo/conductor`), enters each buyer's order (`/needs`), posts the day's events
through the what-if doors, and reads status APIs. The model is called by each
node process directly, not through Town. On a later play `run_check.py` sends
`/demo/start` twice, because the waiting clock keeps stepping after a play ends and
a step landing during the start would put clocks back. That second call came after
the recording, whose second plays used one; it changes no FlexGrid code.

## Who judged what

| Judge | What it judged |
|---|---|
| Town's stage evaluator 0.5.0 | Each market run: one exchange, the closing statement (`accepted`, `claimed`, `received`, `processed`, `response`, `correct`), and `portable_identity`. It does not know what a kilowatt-hour is |
| Town's `verify`, `receipt`, `proof` | That the bundle's hashes, evaluator replay and attestation hold. A signature proves key commitment, not truth |
| `run_check.py` | Sold, delivered, readings in, settled, paid, each cross-check of Town's records against FlexGrid's ledgers, and on an agent's day each node's offer, receipt and reason against Town's record. This is the example's judgment, not Town's |
| FlexGrid | Its own signatures and settlement, which the cross-checks test |

Town's `run_town` starts one buyer and one seller, and `PROFILES` is a closed
table whose task must be a quote, so a day with 15 participants cannot be a stock
Track profile. `run_check.py` therefore drives the same pieces itself:
`nandatown.coordinator`, `POST /runs` with a `TestProfile` naming every
participant, then `evaluate`, `write_bundle` and `attest_bundle`. The bundle is
an ordinary Track bundle: `nandatown verify`, `report`, `receipt`,
`verify-receipt`, `proof` and `visualize` all work on it.

The closing statement is how a market day gets a verdict from Town at all. Before
anything starts, `run_check.py` runs FlexGrid's offline simulator on the same
community, orders and events, every node on rules: one process, an in-memory bus,
a virtual clock, no Town. On a rules day, what that day pays its sellers is frozen
in the Town profile's task, and after the money on the Town run is final, Town's
stock buyer, unchanged, asks the coordinator what it paid, and compares the answer
with the frozen total to the cent. An agent's day can't be held to that money:
what its agents offer is theirs. It is held to the count of nodes whose day closed
on their own word: each one answered the call, and each one under contract had
every half hour's reading reach the coordinator and was settled on them. That
count doesn't hang on what an agent chose, and a reading Town refused still shows.
The badge from `proof` covers that exchange and nothing else about the market.

## The days

Every day is played with every node an agent (the case's name) and with every node
on rules (`NAME_rules`), on the same Town run setup. The window is 18:00 to 21:00 in
summer, at $2.04 a kWh, with the live days' orders of 2,726.52 and 1,635.91 kW,
more than 11 nodes can fill.

| Case | Date | What's different | How it reaches the nodes on Town |
|---|---|---|---|
| `normal` | Mon 29 Jun 2026 | The 22nd of the 45 sale evenings by grid demand. Weather as measured, steady supply, usual behaviour | nothing |
| `extreme_heat` | Thu 2 Jul 2026 | The summer's hottest evening (35.4 C), full sun, the highest grid demand of the 45 | the data |
| `snow_day` | Tue 27 Jan 2026 | A real Boston school closure: families and 60% of the others stay home, cars stay home, heat pumps work hard. Winter test evening 17:00 to 19:00 at $2.04. Orders sized from its rules day by the benchmark's rule: 95% of its lowest half hour of true relief, split 62.5 / 37.5 | the data; the sizing runs offline first |
| `no_sunlight` | 29 Jun | Heavy overcast and rain all day: sunlight 20% of normal, not forecast, on homes and buildings | the what-if weather, from 00:00 for 24 hours |
| `special_schedules` | 29 Jun | The university closes unexpectedly at noon, and the shopping center holds an evening event from 17:30 in a quarter of the space it already offered for sale | a building's closure and zone event, at 12:00 and 17:30 |
| `unusual_people` | 29 Jun | Three owners write "Going away tonight, need a full battery" at 17:00, one asks for the car charged now at 19:00, and the office manager's note about chiller 2 is on file | notes and "car now" from the owners' phones; the manager's note in the office's policy file before the day |
| `grid_emergency` | 29 Jun | The grid operator calls 2,000 kW at 18:45 for 19:00 to 20:00 | the operator's `/alert`, as FlexGrid's live script sends it |
| `network_failure` | 29 Jun | The same day played twice on one Town run, so Town would hand a message out again under an id its sender reused | a second `/demo/start` |
| `network_failure_before` | 29 Jun | The same, on the code of 6 Nov with every node thinking: the failure as it was | as above |

The 8 homes are `hh-001`, `hh-002`, `hh-039`, `hh-071`, `hh-084`, `hh-090`,
`hh-139` and `hh-197`. Together they cover all 5 towns and all 6 household types
of the 200, gas and heat pumps (3 heat pump homes, for the snow day), all 7 battery
makes, 1 to 5 people, small to large solar and floor area, and a hot tub
(`hh-071`; none of the 8 has a pool).

## Files

| File | What |
|---|---|
| `flexgrid_node.py` | One FlexGrid process on a run. An adapter around FlexGrid's own entry points: a join through a Run Grant, the coordinator's answer to `quote_request`, the offline reference, and a reader for each node's decision log: the model's answers, the receipts, what each person was told and each guard verdict |
| `run_check.py` | The one command: starts everything, runs each case, posts the day's events, cross-checks, writes `summary.json` and the bundles |
| `stand_in_model.py` | A stand-in for the model that answers at once with the rule's plan at every point: a dry run of the agent days, and a yardstick for what the agents' own device path changes |
| `evidence/` | Recorded run. `summary.json`, four bundles as plain files, and every other bundle, receipt and decision log in `extras.tar.gz` |
| `../../tests/test_flexgrid_integration.py` | The same as tests, skipped unless `FLEXGRID_HOME` is set or the evidence is there |

## Reproduce

Python 3.11 or newer. No GPU, no Jetson, no tunnel, no live config. The rules days
need no model. The agent days need an Ollama-style model, and repeating them
needs that model: they ask at temperature 0 with a fixed seed, so most answers
repeat, but not all.

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]' numpy scipy
nandatown run quote-clean                     # Town's own control

git clone FLEXGRID_REPO_URL flexgrid          # FlexGrid, with its data pack
git -C flexgrid worktree add ../flexgrid-6nov hw8-baseline-6nov
export FLEXGRID_HOME=$PWD/flexgrid FLEXGRID_BASELINE_HOME=$PWD/flexgrid-6nov

python examples/flexgrid/run_check.py --cases normal_rules        # one rules day, about 7 minutes
python examples/flexgrid/run_check.py --jobs 4                     # every rules day, without --model
```

The agent days, with the model the recording used (14 GB, Apache 2.0, the same
digest as the local tag recorded):

```bash
ollama pull hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q3_K_XL
python examples/flexgrid/run_check.py --jobs 4 \
  --model http://127.0.0.1:11434 \
  --model-name hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q3_K_XL
FLEXGRID_HOME=$PWD/flexgrid python -m pytest -q tests/test_flexgrid_integration.py
```

The agent days take turns on the model, one after the other, and the rules days
run beside them. `--record` replaces `evidence/`, and needs every case,
`FLEXGRID_BASELINE_HOME` and `--model`. Each run stages a copy of the code in a temp
folder, with its own config folder and `NANDATOWN_HOME`, so nothing is written to
the checkout.

The recorded evidence needs no FlexGrid. Four bundles are there as they stand: the
normal day both ways, and the failure case before and after. The archive gives back
all the others, each case's receipt and report, and each node's decision log, byte
for byte:

```bash
nandatown verify examples/flexgrid/evidence/normal
nandatown report examples/flexgrid/evidence/network_failure_before
tar xzf examples/flexgrid/evidence/extras.tar.gz -C examples/flexgrid/evidence
nandatown verify examples/flexgrid/evidence/grid_emergency
nandatown verify-receipt examples/flexgrid/evidence/normal/receipt.json \
  --bundle examples/flexgrid/evidence/normal
```

`proof` also needs the evidence to be under 30 days old, so it stops rendering on
the recorded bundles after that, as it should.

Options: `--homes` (names, the 8 above by default), `--step S` (the shortest wall
time of a step, 2 s), `--model URL --model-name NAME`, and `--model-step S` for
the 6 Nov code's model day, which has no waiting clock and runs at 25 s a step.
POSIX only: the runner signals process groups.

## Results

Recorded with `nandatown` 0.2.0 at main `6f9a7c0`, evaluator 0.5.0, Python 3.12,
macOS. FlexGrid `flexgrid-core` 0.2.0: every current case ran commit `8273a76` of
FlexGrid's public repo (code digest `c3c1bfc3d52c`), and `network_failure_before`
ran the code of 6 Nov, tag `hw8-baseline-6nov` (`3ac5644`, digest `a965a1848f27`).
The model is `qwen3.8-27b-q3:latest` (digest `da1cab12ae5c`) through a local
Ollama, at temperature 0 with seed 7. 8 homes, 3 buildings, 2 buyers, the clock
waiting for the agents with at least 2 s a step. `evidence/summary.json` holds
every stage's expected and observed values, every check, each node's receipts and
what its person was told, each model answer, and each bundle's fingerprint.

### Every case

| Case | Every node | Readings at each buyer | Under contract | Settled | Measured kWh | Buyers pay | Sellers get | Town's verdict |
|---|---|---|---|---|---|---|---|---|
| `normal` | an agent | 66 and 66 | 11 of 11 | 11 of 11 | 14,172.7 | $21,041.22 | $16,832.98 | passed, TOWN-TESTED |
| `extreme_heat` | an agent | 66 and 66 | 11 of 11 | 11 of 11 | 14,355.0 | $21,291.11 | $17,032.89 | passed, TOWN-TESTED |
| `snow_day` | an agent | 44 and 44 | 11 of 11 | 11 of 11 | 3,866.6 | $5,751.63 | $4,601.31 | passed, TOWN-TESTED |
| `no_sunlight` | an agent | 66 and 66 | 11 of 11 | 11 of 11 | 14,194.5 | $21,046.54 | $16,837.24 | passed, TOWN-TESTED |
| `special_schedules` | an agent | 66 and 66 | 11 of 11 | 10 of 11 | 2,686.4 | $4,052.68 | $3,242.15 | passed, TOWN-TESTED |
| `unusual_people` | an agent | 66 and 66 | 11 of 11 | 11 of 11 | 13,116.3 | $19,440.51 | $15,552.41 | passed, TOWN-TESTED |
| `grid_emergency` | an agent | 66 and 66 | 11 of 11 | 11 of 11 | 13,714.3 | $20,358.63 | $16,286.91 | passed, TOWN-TESTED |
| `network_failure`, play 1 | an agent | 66 and 66 | 11 of 11 | 11 of 11 | 14,172.7 | $21,041.22 | $16,832.98 | (asked at the end) |
| `network_failure`, play 2 | an agent | 66 and 66 | 11 of 11 | 11 of 11 | 14,172.7 | $21,041.22 | $16,832.98 | passed, TOWN-TESTED |
| `normal_rules` | on rules | 66 and 66 | 11 of 11 | 11 of 11 | 14,059.4 | $20,874.27 | $16,699.42 | passed, TOWN-TESTED |
| `extreme_heat_rules` | on rules | 66 and 66 | 11 of 11 | 11 of 11 | 13,948.4 | $20,699.80 | $16,559.84 | passed, TOWN-TESTED |
| `snow_day_rules` | on rules | 44 and 44 | 11 of 11 | 11 of 11 | 4,001.9 | $5,951.19 | $4,760.95 | passed, TOWN-TESTED |
| `no_sunlight_rules` | on rules | 66 and 66 | 11 of 11 | 11 of 11 | 14,099.1 | $20,906.07 | $16,724.85 | passed, TOWN-TESTED |
| `special_schedules_rules` | on rules | 66 and 66 | 11 of 11 | 10 of 11 | 2,781.2 | $4,195.79 | $3,356.63 | passed, TOWN-TESTED |
| `unusual_people_rules` | on rules | 66 and 66 | 11 of 11 | 11 of 11 | 14,041.7 | $20,845.80 | $16,676.64 | passed, TOWN-TESTED |
| `grid_emergency_rules` | on rules | 66 and 66 | 11 of 11 | 11 of 11 | 13,628.8 | $20,233.55 | $16,186.84 | passed, TOWN-TESTED |
| `network_failure_rules`, play 1 | on rules | 66 and 66 | 11 of 11 | 11 of 11 | 14,059.4 | $20,874.27 | $16,699.42 | (asked at the end) |
| `network_failure_rules`, play 2 | on rules | 66 and 66 | 11 of 11 | 11 of 11 | 14,059.4 | $20,874.27 | $16,699.42 | passed, TOWN-TESTED |
| `network_failure_before`, play 1 | thinking, 6 Nov code | 66 and 66 | 11 of 11 | 11 of 11 | 7,177.4 | $14,641.88 | $8,534.50 | (asked at the end) |
| `network_failure_before`, play 2 | thinking, 6 Nov code | 0 and 0 | 11 of 11 | 0 of 11 | 0.0 | $0.00 | $0.00 | failed |

Every case came out as expected, stage by stage. In every case each offer, each
node's answer and each reading a buyer counted is on Town's record with the same
value, each buyer's settlement and each payment notice on Town's record equals the
ledgers, and every signature on the record verifies. On every agent day each node's
offer on Town's record is the amount on its agent's receipt, `offer.agent` carries
no reason or person's words, every answer below the rule's amount has a reason its
owner or manager was told, and the clock never went on without an answer it waited
for. The recorded check "the coordinator heard only the amount" tests only the keys
of `offer.agent`, so it passes with the office's `kept_out` beside it on the unusual
people day; the check and `summary.json` stay as recorded. On the special schedules day the university closed at noon after its contract
was made: its readings came in and didn't count, so 10 of 11 were paid, on rules as
offline. The gap between what the buyers pay and what the sellers get is the
coordinator's keep: buyers pay $2.04 per kWh, and sellers are paid at lower rates
by class.

### What the agents changed

Each day with every node an agent beside the same day with every node on rules,
both on Town:

| Day | Measured kWh, agents | on rules | Change | Buyers pay, agents | on rules | Sellers get, agents | on rules |
|---|---|---|---|---|---|---|---|
| normal | 14,172.7 | 14,059.4 | +0.8% | $21,041.22 | $20,874.27 | $16,832.98 | $16,699.42 |
| extreme heat | 14,355.0 | 13,948.4 | +2.9% | $21,291.11 | $20,699.80 | $17,032.89 | $16,559.84 |
| snow day | 3,866.6 | 4,001.9 | -3.4% | $5,751.63 | $5,951.19 | $4,601.31 | $4,760.95 |
| no sunlight | 14,194.5 | 14,099.1 | +0.7% | $21,046.54 | $20,906.07 | $16,837.24 | $16,724.85 |
| special schedules | 2,686.4 | 2,781.2 | -3.4% | $4,052.68 | $4,195.79 | $3,242.15 | $3,356.63 |
| unusual people | 13,116.3 | 14,041.7 | -6.6% | $19,440.51 | $20,845.80 | $15,552.41 | $16,676.64 |
| grid emergency | 13,714.3 | 13,628.8 | +0.6% | $20,358.63 | $20,233.55 | $16,286.91 | $16,186.84 |
| network failure | 14,172.7 | 14,059.4 | +0.8% | $21,041.22 | $20,874.27 | $16,832.98 | $16,699.42 |

The agents mostly took the rule's plan, and every decision left a receipt:

| Day | Decisions with a receipt | Left the rule | Of them, less than the rule | Messages told to people | Flags | Fallbacks to a rule |
|---|---|---|---|---|---|---|
| normal | 88 | 2 | 0 | 0 | none | 0 |
| extreme heat | 86 | 5 | 1 | 1 | none | 0 |
| snow day | 57 | 1 | 0 | 0 | none | 0 |
| no sunlight | 88 | 2 | 0 | 0 | none | 0 |
| special schedules | 92 | 3 | 0 | 0 | none | 0 |
| unusual people | 98 | 7 | 4 | 4 | "less, for the person's note" 4 | 0 |
| grid emergency | 93 | 2 | 0 | 0 | none | 0 |
| network failure, each play | 88 | 2 | 0 | 0 | none | 0 |

`summary.json` has each receipt: the rule's plan and what the agent chose, each by
device, with its reason where it gave one.

- **Most of what left the rule opened more.** Of the 24 decisions that left it, 18
  opened more than the rule did: a small device, such as a water heater or a
  dishwasher beside the car, or the mall's air handlers, which the rule's 2% margin
  would have left out.
- **The owners' notes were kept, and they heard what it cost.** On the unusual
  people day the three owners' "Going away tonight, need a full battery" became
  `keep_battery_full`. Each heard, for example, "Got it: keeping the battery full,
  as you asked, so I'm giving 5.1 kW tonight instead of 14.8.", and those homes
  delivered 62.8 kWh less than on rules, where nothing can read a note. The office
  manager's note on file ("no cooling to give up tonight") became `keep_house_as_is`,
  and they heard "Got it: leaving the heating and cooling as they are, as you asked,
  so I'm giving 102 kW tomorrow night instead of 320."; the guard held the office's
  cooling, pre-cool included, and the office delivered 48.6 kWh against 1,073.6 on
  rules: more than the whole day's 925 kWh drop. A rule would have sold the cooling
  the manager said wasn't there. Each such receipt measures against the amount
  without the note, is flagged "less, for the person's note", and cites the note as
  its fact. The figure in the message is an estimate at the moment of the note, from
  the parts of the offer left: `hh-139` was told 5.5 kW and averaged 1.1 kW on the
  meter.
- **One opening came up short by itself.** On the hot day the mall's agent opened
  the same letters as the rule, the guard lowered the calls, and 7 kW stayed
  uncovered; its manager was told.
- **The same day gave the same answers.** The two plays of the network failure day
  and the normal day are three plays of 29 Jun with every node an agent, and they
  came out the same, decision for decision and cent for cent: at temperature 0 with
  a fixed seed, the same questions got the same answers.

On the three days measured, most of the gap isn't the model's. A node with an agent runs its devices through
the agent's own path, its plans, the guard and the device calls, and that path
doesn't give the rules controller's day even when every answer is the rule's. A
stand-in model that takes the rule's plan at every point
(`examples/flexgrid/stand_in_model.py`, not recorded here) measured 137.0 kWh more
than the rules day on the normal day (+0.97%), 122.7 kWh more on the emergency day
(+0.90%) and 135.4 kWh less on the snow day (-3.38%), on the recorded code, against
+0.81%, +0.63% and -3.38% with the model. So the snow day's -3.4% is all path: the
model's day and the stand-in's are the same, kWh for kWh, and in the buildings the
mall (-82.7 kWh) and the office (-51.6 kWh) are the ones short. On the special
schedules day the mall's agent took the rule's whole plan and the mall still
delivered 91.6 kWh less than on rules.

**Both arms, the same limits, with one difference.** Every device limit is code
both arms run, but one rule is checked only on the agent path: the guard lets an
air handler slow only once its zones are eased, so its boxes don't starve. The
rules controller doesn't check it: it eases the zones and slows the fans in the
same step. On the agent path code eases the zones first by itself whenever a plan
opens an air handler, so the rule only orders the calls: in this recording it
blocked 0 of the 25 air-handler calls on the 8 agent days. Making the rules
controller check it too would change every code-only node and the offline day each
rules day is held to, so it stays as it is, and this is the one place the two arms
differ.

### The agents, the model and the clock

| Day | Model answers | Answer time, s | Clock held, s | Wall, min | Decisions | Below the rule, with a reason told | Fallbacks to a rule | Flags |
|---|---|---|---|---|---|---|---|---|
| normal | 122 | 1,508 | 1,102 | 23.7 | 88 | 0 | 0 | 0 |
| extreme heat | 117 | 1,461 | 1,071 | 23.2 | 86 | 1 | 0 | 0 |
| snow day | 83 | 1,080 | 777 | 18.0 | 57 | 0 | 0 | 0 |
| no sunlight | 122 | 1,513 | 1,104 | 23.7 | 88 | 0 | 0 | 0 |
| special schedules | 128 | 1,552 | 1,149 | 24.5 | 92 | 0 | 0 | 0 |
| unusual people | 138 | 1,783 | 1,271 | 26.5 | 98 | 4 | 0 | 4 |
| grid emergency | 137 | 1,964 | 1,348 | 27.8 | 93 | 0 | 0 | 0 |
| network failure, two plays | 244 | 3,039 | 1,082 | 46.7 | 88 per play | 0 | 0 | 0 |

1,091 answers over the 8 agent days, a median of 11.0 s each and at most 38.6 s, as
each node saw them; one model served both node processes one request at a time. No
answer failed, so no point fell back to a rule. The clock held each day for 13 to 22
minutes in all beyond its 2 s steps, mostly at the offers the day before, at each
half hour of the sale, and at the reports. A rules day takes 5.5 minutes. The only
flags are the 4 "less, for the person's note"; no receipt is "reason cites no fact"
or "no reason given". These flags apply to cuts only, and only the agent's own
choice is tested for a fact: the mall's hot-day cut, short only after its calls ran,
cites none and has no flag. 22 of the 690 receipts have an empty reason (21 pre-cools
at the playbook's depth, one reading of a note). The guard passed 34 to 66 device calls a day as asked, and
changed two in all: it trimmed the mall's zone cooling to +1.125 C after the event on
the special schedules day, and it held the office's pre-cool on the unusual people
day, which the manager's note had ruled out.

A speed-up, measured separately, not in this recording and on the code just before
its last fixes: FlexGrid's `llm_short_answers` ends every question asking for each
reason in at most 20 words. On the normal day it cut the model's time from 1,108 to
879 s (Ollama's own count),
the median answer's from 8.5 to 6.3 s, and the day from 23.2 to 19.9 minutes, with
96 kWh (0.7%) less measured. One run each, so read it as a size.

### Rules days against the offline day

With every node on rules, 7 of the 8 days on Town are the offline day number for
number: the same kWh sold, measured and true, and each node paid the same. On the 6
played once, Town's stock buyer checked the sellers' total to the cent, and on the
3 with no event Town also carried the offline day's messages, kind by kind.
`network_failure_rules` is the offline day on both plays.

`grid_emergency_rules` is held to its network only. Offline, each node answers the
grid operator's call before its own step; on Town, a moment after it, from a state
one step on. So the call's answers differ a little: the sellers get $16,186.84
against $16,186.81 offline, and the true kWh differ by 11. The snow day's orders,
1,330.82 and 798.49 kW, are 95% of its rules day's lowest half hour of true relief,
2,241.4 kW, split 62.5 / 37.5.

### The failure case: a repeated message id

This is a failure from a live day, 6 Nov 2026, reproduced on a real Town run.

- **Baseline.** On the 6 Nov code, every node thinking, the first play of the day
  pays every seller: 66 readings at each buyer, 11 settled.
- **Failure.** The same day played again on the same Town run. That code built a
  batch's id from the dataset time, so the second play repeats it with a different
  body, and Town refuses it: `identity_reuse_rejected` 43 times in Town's log. The
  code took the refusal as "already sent". The buyers hold 0 readings and pay $0.00,
  and all 11 sellers go unpaid. Town's stock buyer asks how many nodes' days closed
  and Town reports: `correct` failed, "buyer observed total 0 against expected 11".
- **Change.** A message's id now says which message it is: the same through any
  retry within a play, new at each play, so the same id only ever carries the same
  body, which is Town's rule (`ids.py`, `town.Sent`). A refused send is counted and
  logged (`town.TownSession.send`).
- **Rerun.** The same two plays on the current code with every node an agent:
  nothing refused, all 66 readings in at every receiver, all 11 settled on both
  plays, and Town passes. With the clock waiting, the second play is the first one
  again, to the cent.

The 6 Nov code has no waiting clock, so its model day runs at 25 s a step, the
quiet hours 8 times faster, as its live model days did: 191 answers and 53 minutes
for the two plays. Its first play isn't comparable with today's: that code sold
less of the same orders.

### What this phase found and changed in FlexGrid

Six faults in FlexGrid came out of these runs, each fixed in FlexGrid and recorded
again:

- The coordinator's desk handed a gap to the first answers that covered it, so
  which building covered it depended on which answer came through Town first. On a
  rules day that moved 35.6 kWh from the university to the office and the day no
  longer matched offline. With the clock waiting, the desk now takes every answer
  first, as the offline day does.
- A building's pre-cool was judged by what the guard applied, not what the agent
  asked. When the manager's note made the guard hold the office's cooling, the
  receipt said "less than the rule, no reason given", and this check failed the
  unusual people day. It now keeps both, and a pre-cool shallower than the
  playbook's needs a reason like any other cut.
- A plan's agent may test its letters first. The test's answer began "TEST ONLY,
  nothing was sent", and three plans copied it as their reason and opened nothing.
  It now says "A test only: nothing has opened yet" and ends "Nothing opens until
  you call set_plan".
- A person's note was never "less than the rule". Each receipt compared the agent
  with code's plan under the wish, so when an owner wrote the battery out of a sale
  nobody was told what it cost. A rule can't read a note, so a note is now measured
  against the amount without it, flagged, its words are the fact, and the person
  hears what it costs ("Got it: ..."). The office manager's note on file is handled
  the same way at the offer.
- A building's list showed its air handlers as if they could simply open. Asked
  alone, the guard blocks one until its zones are eased, so the agent's test said
  "blocked", and on 7 of 8 days in an earlier recording the office's agent left them
  out of the opening, 76 to 204 kW short. Code's own plan never hit that block: when
  a plan opens an air handler, code eases its zones first. The list now says so ("It
  needs E first, which eases its zones"), the test runs calls the way the plan
  carries them out, and "Nothing is uncovered" shows only when nothing is: under the
  plan's 2% margin the mall's car ports and plugs read so with 391 kW uncovered.
- A car that gives power didn't hold its charger on the agent path. One charger
  can't draw and give at once, and the rules controller holds it through the sale;
  with every node an agent `hh-090`'s car charged every other quarter hour while it
  gave, and the home lost $28.66 every day against rules. Giving now holds the
  charger.

## What this shows, and what stays untested

Shown: FlexGrid's coordinator, two buyers and eleven nodes, every node an agent,
as separate Town participants with Run Grants, complete whole market days over
Town's mailbox on current `main`, on 8 kinds of day. Each node's offer on Town's
record is the amount its agent chose, of its decision the coordinator heard only
that amount (what else it got is listed at the top), each decision left a receipt, and every answer below the rule's amount
carried a reason its owner or manager heard. With the clock waiting, no decision
was skipped and no answer fell back to a rule. With every node on rules, a day on
Town is the offline day to the cent, and Town's stock buyer can judge that money,
except on the emergency day, explained above. The replayed id that cost every
seller its pay on 6 Nov is refused by Town on that code and not on this one.

Not shown, and worth saying plainly:

- Every load is simulated. Homes and buildings replay a dataset, and no real device
  or meter is involved. Homes are synthetic. The wholesale prices in the data pack
  are a flat placeholder.
- The waiting clock exists only in a replay. A real evening won't pause for the
  agents, so these days show what the agents decide with all the time they need, not
  what they decide against a deadline. At this size a day takes 18 to 28 minutes
  with the clock held, against 5.5 on rules.
- One model and one run per day. At temperature 0 with a fixed seed the same day gave
  the same answers three times, but another model, or another day, would answer
  differently, so each gap between an agent's day and its rules day is one sample,
  not an estimate. Nothing here says how often agents do better.
- Part of each gap isn't the model's, as above: the agent's own device path doesn't
  give the rules controller's day even when every answer is the rule's.
- A reason is the model's own sentence, except on a note: where a note lowers a
  sale, the reason opens with code's fixed words for the need the model recorded (for
  the three owners, that is the whole reason). Code checks that a cut names a fact and
  flags one that doesn't, not that the sentence reads true. The kW in a note's message
  is an estimate at the moment of the note, not what the meter will read.
- The data pack is two stretches, 1 to 28 Jan and 1 Jun to 3 Jul 2026, with simple
  temperature forecasts, so the trained forecasts aren't exercised, and the three
  buildings earn most of the pay.
- A small community: 8 homes and 3 buildings, against 200 homes at full size. One
  machine, loopback, no tunnel, no TLS, so the slow links and drops of the live days
  aren't reproduced.
- Town's evaluator judged one exchange per market run. Sold, delivered, readings,
  settled and paid are `run_check.py`'s judgment, on records Town kept. Town's
  `TOWN-TESTED` badge on a market bundle covers that exchange only.
- The offline reference is FlexGrid's own code. Agreement shows Town changed
  nothing, not that the settlement rules are right.
- Town's stock buyer is Town's own fixture. Apart from it, no independent agent took
  part, and nobody else has reproduced this yet. Town's Lab, A2A and MCP are not
  used.

The recorded `TOWN-TESTED` lines age out after 30 days by design. Bundles hold
FlexGrid's signed messages and each agent day's decision logs, with each question
in full, and no keys or tokens; `state/` is left out.
