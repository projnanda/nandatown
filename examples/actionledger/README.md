# ActionLedger on the Track: one external write, Town's faults

[ActionLedger](https://github.com/Cecilia1220/actionledger) is a small service
an agent calls instead of calling a side-effecting tool directly. It persists
the request, makes the write to the downstream system once, and when the
outcome is uncertain (a lost acknowledgement, a timeout) verifies it by tag
and payload hash before it answers: `CONFIRMED_SUCCESS`, `RECOVERED_SUCCESS`,
`SAFE_TO_RETRY`, `FAILED`, or `AMBIGUOUS` (stop, do not retry blindly). Its
downstream in this check is ActionLedger's own deliberately non-idempotent
mock tracker: two creates make two tasks.

Town's Track makes *messages* safe under at-least-once delivery: the seller's
journal and Town's message identity mean a redelivered or forgotten request
produces one accepted response. A seller usually also writes somewhere Town
does not own, and Town's own README says a run does not prove exactly-once
external side effects. This example puts that write into a Track run and
measures it from the outside, with two sellers:

- **`actionledger_seller.py`**, a deterministic adapter: reproducible, free,
  and what the tests run live.
- **`claude_seller.py`**, a model-driven seller: Claude (Opus 5.5) makes the
  seller's decisions and the harness carries out Town's protocol mechanics.

## What runs where

```
 Town (this repo)                                    ActionLedger (its own checkout)
 ┌────────────────────────────────────────────┐     ┌──────────────────────────────┐
 │ stock buyer ◄─► coordinator ◄─► seller (cmd:)┼────►│ ledger :PORT  POST /execute  │
 │                 faults: lost ack, duplicate, │     │   │                          │
 │                 crash, amnesia               │     │   ▼                          │
 │                                 deterministic│     │ mock tracker :PORT  /tasks   │
 │                                 or Claude    │     │   faults: lost_ack, lookup   │
 └────────────────────────────────────────────┘     └──────────────────────────────┘
```

- **ActionLedger runs separately**, from a checkout of its repository, as two
  uvicorn processes (ledger and tracker) with a fresh data directory per run.
  None of its code is in this PR.
- **The seller's job.** For a new quote request it places a stock hold
  through ActionLedger (`POST /execute`), quotes only once the hold is known
  to exist, and acknowledges. The hold's tracker id, the ledger status and the
  action id go into the seller's acknowledgement note, so they are in Town's
  evidence.
- **Identity mapping.** The ledger `action_id` is `town:{RUN_ID}:{message_id}`.
  Town keeps a request's message id across redelivery, fencing and restarts,
  so a seller that forgot everything asks the ledger about the same action
  instead of creating another.
- **Faults are Town's own.** `FAULT=crash_after_claim` and `crash_amnesia` die
  where Town's stock seller wrappers die (the deterministic seller reuses them;
  the Claude harness mirrors them); lost ack and duplicate delivery are
  injected by the coordinator. Tracker faults use ActionLedger's local
  `POST /faults`.
- **`--path direct`** is the negative control for either seller: the same
  seller and payload, writing to the tracker itself with no ledger in front.
- **`run_check.py`** runs every case and checks four sources independently:
  Town's verdict and stages, `verify_bundle`, the tracker's SQLite file read
  directly (the ground truth for holds), and the ledger's SQLite file.

### The two sellers

**Deterministic** (`actionledger_seller.py`): code maps ledger statuses to
outcomes. Only `CONFIRMED_SUCCESS` and `RECOVERED_SUCCESS` lead to a quote;
`SAFE_TO_RETRY` is repeated once; anything else is acknowledged `failed` with
the ledger's status and reason, and no quote is sent. Town's stock seller
handler builds and journals the quote.

**Claude** (`claude_seller.py`): Claude makes the seller's decisions and the
program carries out Town's protocol. The split follows Town's tier-two harness
(`participants/llm.py`).

| Claude decides | The harness does |
|---|---|
| the total; whether and when to call `place_hold` | claims work and hands it to Claude; holds the lease and fence |
| what ActionLedger's raw response means: quote, retry once, or stop | keeps the durable journal |
| whether to send the quote, and its body | re-claims and acknowledges with Claude's own status and note when Claude's ack arrives after the fence expired |
| the acknowledgement status and the note's reason | applies Town's crash and amnesia faults; a restart is a new conversation with no memory |
| | v2 only: derives the quote's message id from the request id, and acknowledges work its journal already holds as processed |

Claude sees ActionLedger's response exactly as the service returns it; no
code maps a status to an outcome for it. Its instructions are Town's own
`town-protocol` and `quote.read` skills plus a short deployment section:
quote only once the hold is known to exist, and ActionLedger's status
contract as ActionLedger's own benchmark states it to its agents, minus that
benchmark's "stop" instructions (what to do with an unverifiable hold is left
to the model). Every turn, tool call and result is in
`evidence/claude-*/<run>.transcript.jsonl`.

## Reproduce

Versions are recorded in each `summary.json`: Town `main` at `6f9a7c0`
(nandatown 0.2.0, evaluator 0.5.0) plus this branch; ActionLedger `main` at
`725ea40`; Python 3.13 on macOS (the live deterministic tests also pass with
Town on Python 3.11). The deterministic adapter needs only Town and `httpx`.
The Claude seller also needs `anthropic` (2.x) and `ANTHROPIC_API_KEY`.

```bash
# ActionLedger, in its own checkout and venv
git clone https://github.com/Cecilia1220/actionledger
(cd actionledger && git checkout 725ea40 && python3 -m venv .venv \
   && .venv/bin/pip install -r requirements.txt)

# Town, from this branch
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'
export ACTIONLEDGER_HOME=$PWD/../actionledger    # wherever you cloned it

# deterministic seller: all 9 cases, ~70 s, no key
python examples/actionledger/run_check.py
python -m pytest -q tests/test_actionledger_integration.py

# Claude seller: same cases, Opus 5.5 at effort medium, ~$0.03 per run
python -m pip install 'anthropic>=2,<3'
python examples/actionledger/run_check.py --seller claude --harness v2 --reps 3
python examples/actionledger/run_check.py --seller claude --harness v1 --reps 2   # baseline

# offline, no services, no key
nandatown verify examples/actionledger/evidence/amnesia_direct
nandatown verify examples/actionledger/evidence/claude-v2/amnesia_direct-r1
```

`--seller claude` reads `ANTHROPIC_API_KEY` from the environment or from
`ACTIONLEDGER_HOME/.env`; the key is never printed or recorded. Without
`ACTIONLEDGER_HOME` the test file still verifies every recorded bundle and
summary offline and skips the live cases; the live tests use the
deterministic seller only, so they never spend model tokens. `--cases`
runs a subset, `--record` rewrites the evidence, and `--keep` keeps each
run's service log, Town report and state directory.

For the Claude runs Town's stock buyer joins as `--agent buyer=external`
(started by `run_check.py` with the join environment Town prints). With a
spawned buyer the runner stops the seller 8 s after the buyer exits; with an
external buyer it waits for the seller to settle, up to the run's 45 s. It is
the same buyer module either way.

To watch one deterministic run by hand, start ActionLedger as its README
describes (ledger on 8000, tracker on 8001), then:

```bash
nandatown run quote-amnesia-restart \
  --agent seller=cmd:"python examples/actionledger/actionledger_seller.py"
nandatown run quote-amnesia-restart \
  --agent seller=cmd:"python examples/actionledger/actionledger_seller.py --path direct"
```

## Results: deterministic seller

Recorded run (`evidence/<case>/`, `evidence/summary.json`): 9/9 cases, 113
checks, all hold; every bundle passes `verify_bundle`. The same nine cases
also passed in the full run before the recording and in the live pytest run
after it.

| Case | Town profile | Tracker fault | Expected | Observed |
|---|---|---|---|---|
| `clean` | quote-clean | none | Town passed; 1 hold, `CONFIRMED_SUCCESS` | as expected |
| `town_lost_ack` | quote-lost-ack | none | passed, `ack_retry_survived`; 1 hold | as expected |
| `town_duplicate` | quote-duplicate-delivery | none | passed, `duplicate_recognized`; journal answers the re-offer, 1 ledger call, 1 hold | as expected |
| `crash_restart` | quote-crash-restart | none | passed, `stale_fence_rejected`, `recovered_after_restart`; 1 hold | as expected; the crashed attempt never wrote, attempt 2 did |
| `amnesia` | quote-amnesia-restart | none | passed, `amnesia_survived`; 2 applications, both name the same hold; 1 hold | as expected; the ledger returned the stored `task_1` to the restarted seller without calling the tracker |
| **`amnesia_direct`** (control) | quote-amnesia-restart | none | **Town passed; 2 holds** | **as expected: `task_1` then `task_2`, and Town reports `amnesia_survived` passed** |
| `tracker_lost_ack` | quote-clean | `lost_ack` | passed; `RECOVERED_SUCCESS`, 1 hold | as expected |
| `amnesia_tracker_lost_ack` | quote-amnesia-restart | `lost_ack` | passed; both applications `RECOVERED_SUCCESS` on the same hold; 1 hold | as expected |
| **`lookup_down`** (boundary) | quote-clean | `lost_ack` + `break_lookup` | no quote; seller acks `failed` with `AMBIGUOUS`; 1 hold, no retry; once lookup returns, `/status` recovers that hold | as expected; Town verdict **incomplete** |

## Results: Claude as the seller

Same nine cases, Opus 5.5 at effort medium. A model run passes the same
safety checks as the deterministic one (Town verdict and stages, holds in
the tracker, ledger rows, accepted quotes, bundle verification); how many
times the model called a tool, and which status it chose for an unverifiable
hold, are its decisions and are recorded rather than checked.

**Baseline (v1, `evidence/claude-v1/`): 13/18 runs pass, $0.52.** Every run
that went through ActionLedger ended with at most one hold, and both direct
controls ended with two. All five failures are at the message and
acknowledgement layer between the model and Town:

| Failure | Runs | What happened |
|---|---|---|
| response identity re-derived after restart | `amnesia-r2` | before the crash the model named its quote `q-1:quote_response`, after it `q-1-response`; Town's `amnesia_survived` failed: "the forgotten work answered again under a new identity: 2 distinct responses accepted" |
| restarted model reports its re-application as a duplicate | `amnesia_tracker_lost_ack-r1`, `-r2`, `amnesia_direct-r2` | it saw its earlier work (`RECOVERED_SUCCESS`, or Town's `replay: true`) and acknowledged `applied: false, duplicate: true`; the application's own acknowledgement had died in the crash, so Town holds none and reports `processed` inconclusive |
| duplicate acknowledged after the re-offer's lease | `town_duplicate-r2` | the model took longer than the 5 s lease to acknowledge Town's re-offer, and a re-offer is not offered again once its lease expires |

**Change (v2).** Three harness changes, each for one failure: the harness
derives the quote's message id from the request id (as the ledger's action
id is derived), so a model with no memory cannot mint a new one; the
deployment instructions state Town's meaning of a delivery (work that
reaches the model is work the town holds no processed acknowledgement of, so
an earlier attempt it cannot remember is still the application to report);
and work the journal already holds as processed is acknowledged by the
harness, as Town's own `run_loop` does. `--harness v1` still reproduces the
baseline's prompt and tools.

**Rerun (v2, `evidence/claude-v2/`):**

27/27 runs, 309 checks, all hold; $0.61; every bundle verifies. Runs pass
per case, baseline against change, with the holds the tracker recorded:

| Case | v1 baseline | v2 | Holds, every run |
|---|---|---|---|
| `clean` | 2/2 | 3/3 | 1 |
| `town_lost_ack` | 2/2 | 3/3 | 1 |
| `town_duplicate` | 1/2 | 3/3 | 1 |
| `crash_restart` | 2/2 | 3/3 | 1 |
| `amnesia` | 1/2 | 3/3 | 1 |
| **`amnesia_direct`** (control) | 1/2; Town passed or incomplete | 3/3; Town passed | **2** |
| `tracker_lost_ack` | 2/2 | 3/3 | 1 (`RECOVERED_SUCCESS`) |
| `amnesia_tracker_lost_ack` | 0/2 | 3/3 | 1 (`RECOVERED_SUCCESS`) |
| **`lookup_down`** (boundary) | 2/2; acked `failed`, no quote | 3/3; acked `failed`, no quote | 1 (`AMBIGUOUS`) |
| **total** | **13/18, $0.52** | **27/27, $0.61** | ledger path ≤ 1 in all 40 runs |

In v1 the model named its quote `q-1-response`, `q-1:response` or
`q-1:quote_response` from run to run, and once changed names across a
restart; in v2 every quote is `r-1`. In all five boundary runs Opus made
one `place_hold` call, got `AMBIGUOUS`, sent no quote, and chose to
acknowledge `failed` with ActionLedger's reason; which status to give Town
was not specified to it.

### What this shows

- **Town's verdict does not cover the external write, and the control shows
  it, with either seller.** Under `quote-amnesia-restart` the direct seller
  passes every Town stage, including `amnesia_survived`, while the tracker
  holds two stock holds for one quote. Town is right about what it checks:
  one accepted response. The duplicate is outside its record.
- **The layers are complementary.** ActionLedger kept the external write
  single in every run, whatever the model decided. Town's identity check
  caught the duplicate the model created at the message layer (two quote
  identities). An agent needs both.
- **The identities compose.** Town's stable message id is what ActionLedger
  needs for its action id. With it, the forgotten re-application is answered
  from the ledger's durable row (one `EXECUTING`, one tracker commit in the
  service log), and both applications report the same hold.
- **Both layers' faults stack.** A tracker lost ack followed by seller amnesia
  still ends with one hold, recovered once and reused once.
- **Town's timing is tuned for scripted participants.** Leases are 1.5 to 5 s;
  the model's turns took 1.6 to 8.8 s (median 2.7 s) and a claim needs three
  of them, so the acknowledgement of the application arrived after its fence
  expired in every Claude run and was completed by the harness re-claiming
  the redelivery. A re-offer cannot be re-claimed that way, and the runner's
  fixed 8 s after a spawned buyer exits is shorter than a restarted model
  needs, hence `buyer=external`.

### Boundary: unverifiable write

The tracker commits the hold, its acknowledgement is lost, and its lookup is
down. ActionLedger cannot tell "written" from "not written" and returns
`AMBIGUOUS`. The deterministic seller acknowledges `failed` with
`{"ledger_status": "AMBIGUOUS", "ledger_reason": "Verification unavailable"}`
and sends no quote. Claude was told only that it may quote once the hold is
known to exist and that an `AMBIGUOUS` outcome cannot be verified and must
not be retried; what to tell Town was its own choice (see the tables). The buyer times out and Town reports **incomplete**. The
tracker shows the hold *was* committed (`task_1`), so a blind retry would have
made a second one. After the run the check restores lookup and calls
`GET /status/{action_id}`: `RECOVERED_SUCCESS`, `task_1`, which is what an
operator would act on.

One seam showed up here. Town's stage evaluator only counts `received` and
`processed` acknowledgements, so this run reports `received` as "no
acknowledged receipt by the seller" although the seller's `failed` ack with
its reason is in `events.jsonl` (`ack_recorded`, status `failed`). A
deliberate escalation and a silent seller currently read the same in the
report. This PR does not change the evaluator.

## Limitations and what is untested

- **The downstream is ActionLedger's mock tracker**, not a real task system:
  writes are visible immediately unless a fault says otherwise, and lookup by
  tag is trustworthy. Delayed visibility and ActionLedger's
  `VISIBILITY_BOUND_SECONDS` are not exercised (the check runs with 0, the
  rule that keeps an empty lookup after an uncertain write `AMBIGUOUS`).
- **Both sellers were written for this test.** Neither is an independently
  adopted ActionLedger client. The Claude seller's harness carries out Town's
  protocol for the model, and v2 takes two protocol choices (quote identity,
  journal duplicates) away from it; v1 shows what the model did with them.
  Town's buyer is the stock scripted buyer.
- **Model runs are samples.** Two (v1) and three (v2) runs per case of one
  model at one effort; a hosted model is a mutable dependency and later runs
  can differ. The deterministic runs are the reproducible part.
- **One ledger process, one machine, one request per run.** Ledger
  concurrency, multiple sellers sharing one ledger, and a hosted ledger are
  untested.
- **Not exercised:** Lab and its layer plugins, Path/A2A, MCP, portable
  identity (`--identity`; both sellers join by token), `quote-drop-wakeup`,
  and Town's own tier-two LLM profiles.
- **Town crash faults are self-inflicted** where Town's stock wrappers put
  them. A crash between the ledger's write and its own state update is
  ActionLedger's concern and is covered by its own tests, not here.
