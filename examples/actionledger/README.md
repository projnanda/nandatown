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
measures it from the outside.

## What runs where

```
 Town (this repo)                                  ActionLedger (its own checkout)
 ┌──────────────────────────────────────────┐     ┌───────────────────────────────┐
 │ stock buyer ◄─► coordinator ◄─► seller ───┼────►│ ledger :PORT  POST /execute   │
 │                 (faults:        cmd:      │     │   │                           │
 │                  lost ack,      actionledger_   │   ▼                           │
 │                  duplicate,     seller.py │     │ mock tracker :PORT  /tasks    │
 │                  crash, amnesia)          │     │   (faults: lost_ack, lookup)  │
 └──────────────────────────────────────────┘     └───────────────────────────────┘
```

- **ActionLedger runs separately**, from a checkout of its repository, as two
  uvicorn processes (ledger and tracker) with a fresh data directory per case.
  None of its code is in this PR.
- **`actionledger_seller.py`** is the Town-side adapter, a `cmd:` seller. For a
  new quote request it first places a stock hold through `POST /execute`, then
  lets Town's stock seller handler build and journal the quote. The hold's
  tracker id, the ledger status, and the action id go into the seller's
  `processed` acknowledgement note, so they are in Town's evidence.
- **Identity mapping.** The ledger `action_id` is `town:{RUN_ID}:{message_id}`.
  Town keeps a request's message id across redelivery, fencing and restarts,
  so a seller that forgot everything asks the ledger about the same action
  instead of creating another.
- **Status handling.** Only `CONFIRMED_SUCCESS` and `RECOVERED_SUCCESS` let the
  seller quote. `SAFE_TO_RETRY` is repeated once with the identical request.
  Anything else (`AMBIGUOUS` above all) is acknowledged `failed` with the
  ledger's status and reason in the note, and no quote is sent.
- **Faults are Town's own.** `FAULT=crash_after_claim` and `crash_amnesia` wrap
  the adapter with the stock seller's `crash_wrapper` and `amnesia_wrapper`;
  lost ack and duplicate delivery are injected by the coordinator. Tracker
  faults use ActionLedger's local `POST /faults`.
- **`--path direct`** is the negative control: the same seller and payload,
  writing to the tracker itself with no ledger in front.
- **`run_check.py`** runs every case and checks four sources independently:
  Town's verdict and stages, `verify_bundle`, the tracker's SQLite file read
  directly (the ground truth for holds), and the ledger's SQLite file.

## Reproduce

Versions recorded with the evidence (`evidence/summary.json`): Town `main` at
`6f9a7c0` (nandatown 0.2.0, evaluator 0.5.0); ActionLedger `main` at
`725ea40`; Python 3.13 on macOS (the live tests also pass with Town on Python
3.11). The adapter uses only Town and `httpx`, a Town dependency.

```bash
# ActionLedger, in its own checkout and venv
git clone https://github.com/Cecilia1220/actionledger
(cd actionledger && git checkout 725ea40 && python3 -m venv .venv \
   && .venv/bin/pip install -r requirements.txt)

# Town, from this branch
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'

export ACTIONLEDGER_HOME=$PWD/../actionledger    # wherever you cloned it
python examples/actionledger/run_check.py         # all 9 cases, ~70 s
python -m pytest -q tests/test_actionledger_integration.py   # 20 passed live
nandatown verify examples/actionledger/evidence/amnesia      # offline, no services
```

Without `ACTIONLEDGER_HOME` the test file still verifies every recorded bundle
and the summary offline (11 tests) and skips the live cases.
`run_check.py --cases amnesia,amnesia_direct` runs a subset, `--record`
rewrites `evidence/`, and `--keep` keeps each case's service log, Town report
and state directory.

To watch one run by hand, start ActionLedger as its README describes (ledger
on 8000, tracker on 8001), then:

```bash
nandatown run quote-amnesia-restart \
  --agent seller=cmd:"python examples/actionledger/actionledger_seller.py"
nandatown run quote-amnesia-restart \
  --agent seller=cmd:"python examples/actionledger/actionledger_seller.py --path direct"
```

## Results

Recorded run: 9/9 cases, 113 checks, all hold; every bundle passes
`verify_bundle`. Holds are counted in the tracker's database, not taken from
any participant's claim. The same nine cases also passed in the full run
before the recording and in the live pytest run after it.

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

### What this shows

- **Town's verdict does not cover the external write, and the control shows
  it.** Under `quote-amnesia-restart` the direct seller passes every Town
  stage, including `amnesia_survived`, while the tracker holds two stock holds
  for one quote. Town is right about what it checks: one accepted response.
  The duplicate is outside its record.
- **The identities compose.** Town's stable message id is what ActionLedger
  needs for its action id. With it, the forgotten re-application is answered
  from the ledger's durable row (one `EXECUTING`, one tracker commit in the
  service log), and both applications report the same hold. Town's side is
  unchanged: the re-sent quote gets `replay_returned`, as with the stock
  seller.
- **Both layers' faults stack.** A tracker lost ack followed by seller amnesia
  still ends with one hold, recovered once and reused once.

### Boundary: unverifiable write

The tracker commits the hold, its acknowledgement is lost, and its lookup is
down. ActionLedger cannot tell "written" from "not written", returns
`AMBIGUOUS`, and the seller acknowledges `failed` with
`{"ledger_status": "AMBIGUOUS", "ledger_reason": "Verification unavailable"}`
and sends no quote. The buyer times out and Town reports **incomplete**. The
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
- **The seller is a deterministic adapter written for this test**, not an
  independently adopted ActionLedger client, and not a model-driven agent:
  ActionLedger's own LLM clients are not in these runs. Town's buyer is the
  stock scripted buyer.
- **One ledger process, one machine, one request per run.** Ledger
  concurrency, multiple sellers sharing one ledger, and a hosted ledger are
  untested. Each case is one run, not a precommitted campaign.
- **Not exercised:** Lab and its layer plugins, Path/A2A, MCP, portable
  identity (`--identity`; the adapter joins by token), `quote-drop-wakeup`,
  and the tier-two LLM profiles.
- **Town crash faults are self-inflicted** by the stock wrappers the adapter
  reuses, as for Town's own seller. A crash between the ledger's write and its
  own state update is ActionLedger's concern and is covered by its own tests,
  not here.
