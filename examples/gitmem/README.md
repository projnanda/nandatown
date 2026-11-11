# gitmem: a Track seller whose memory is a git repository

This integration puts [gitmem](https://github.com/amangang3/nandatown-memory-mvp)
(`git.v1`, a memory layer built as a course project) behind a real Track
participant. It also checks, from Town's own records, whether that memory
told the truth.

gitmem keeps an agent's memory as one markdown file per fact, and every
change is a git commit naming who asserted it, when, and from which event.
It captures memory in two lanes. The agent writes what it chose to
remember (the `self` lane). An outside observer derives facts from the
event trace on its own (the `observed` lane). One write contract guards
both lanes, and the two are compared afterwards. Until now the project ran
only as a Lab plugin, with agents of its own. This directory runs it
end to end on the Track: as a separate process, over Town's HTTP mailbox,
through a real crash and redelivery, judged by Town's evaluator.

## What is tested

| | |
|---|---|
| Interface | Track HTTP mailbox: join, claim under a lease, send, fenced ack |
| Entry point | `nandatown test-agent --role seller --cmd ...` (the bring-your-own-agent path) |
| Counterpart | Town's scripted buyer |
| Profiles | `quote-amnesia-restart` (answer, then crash before the ack) and `quote-crash-restart` (crash after the claim, before applying) |
| Judged by | Town's stage evaluator `0.5.0`, plus the gitmem observer over the same bundle |

The workflow under test is: the seller answers a quote request and
remembers the answer, crashes before acknowledging it, and is restarted by
Town. The coordinator then redelivers the request. With intact memory, the
seller should recognise the work and resend the identical reply. Town
should then return the original acceptance instead of accepting a second
answer.

The failure case is a person editing the memory while the seller is
down. On the restart, before the seller opens its store, a scripted hand
edit either changes the remembered total from 3990 to 3999 or deletes the
remembered quote. It writes the files directly, the way a text editor
would, never through the store. The seller then runs under one of two
policies:

- `verify` (the gitmem default): ask git for fact files that changed
  outside the store, restore them to their last commit, and record the
  repair as a fact.
- `trust`: believe the files, as a plain markdown memory would.

## Files

| File | What it is |
|---|---|
| `gitmem_seller.py` | the mailbox seller; its memory is a gitmem store in `STATE_DIR/memory` |
| `gitmem_observe.py` | the observer lane: derives facts from `events.jsonl`, writes them into the same store through the same contract, reconciles, and counts applications in git history |
| `memory_schema.json` | the write contract both lanes pass through |
| `run_check.py` | runs the six cases and compares expected with actual; exits nonzero on any mismatch |
| `test_gitmem_check.py` | the same cases as pytest (not in Town's default `testpaths`) |
| `evidence/<case>/` | the recorded run of each case: the five canonical records and `manifest.json`, the attestation, `report.md`, `gitmem-check.json`, the memory's full `git log -p` and the seller's log |

The gitmem library itself is **not** copied here. It stays in its own
repository, and the adapter imports it from a pinned checkout named by
`GITMEM_SRC`.

## Reproduce

```bash
# Town, from this checkout
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'

# gitmem, at the pinned commit
git clone https://github.com/amangang3/nandatown-memory-mvp /tmp/gitmem
git -C /tmp/gitmem checkout 121a10dc214a6f7475ff764a33f23d2be6d63d9d
export GITMEM_SRC=/tmp/gitmem/memory_mvp

# the compatibility check: six Track runs, about 20 seconds, no model account
python examples/gitmem/run_check.py
# or as pytest
python -m pytest -q examples/gitmem

# one case by hand
GITMEM_EDIT=total GITMEM_TRUST=trust \
  nandatown test-agent --role seller --profile quote-amnesia-restart \
    --cmd "python examples/gitmem/gitmem_seller.py"
python examples/gitmem/gitmem_observe.py runs/<run-id>
git -C runs/<run-id>/state/seller/memory log -p     # the memory's history

# offline verification of the recorded evidence
for c in examples/gitmem/evidence/*; do nandatown verify "$c"; done
```

The check needs `git` on `PATH`, because the store is a git repository.

## Expected and actual

`run_check.py` output on Town `6f9a7c0`:

```
case               town         agree contra apps found left refused  result
----------------------------------------------------------------------------
intact             passed           3      0    1     0    0       0  ok
edit-total-verify  passed           3      0    1     2    0       0  ok
edit-total-trust   incomplete       1      2    1     0    2       1  ok
delete-trust       passed           3      0    2     0    2       0  ok
delete-verify      passed           3      0    1     2    0       0  ok
crash-restart      passed           3      0    1     0    0       0  ok

6 of 6 cases matched.
```

The columns are:

- **town**: Town's verdict.
- **agree / contra**: the seller's remembered `reply_id`, reply fingerprint
  and total, compared with the same three facts derived from Town's events.
  The observed reply fingerprint is Town's `content_fingerprint` of the
  accepted response. The observed total is the one the buyer acknowledged.
- **apps**: how many "applied q-1" commits the memory's git history holds.
- **found**: outside edits the seller found on restart.
- **left**: outside edits still in the files after the run.
- **refused**: resends Town refused with `identity_reuse_rejected`.

| Case | Expected | Actual |
|---|---|---|
| `intact` | memory survives the crash, the restart resends the remembered reply, and Town returns the original acceptance (`replay_returned`) | as expected: one application, three facts agree. The reply's fingerprint matches Town's stock seller's |
| `edit-total-verify` | the edit is found and restored from git, and the identical reply is resent | as expected: 2 modified files found and restored, a repair commit, PASSED |
| `edit-total-trust` | the seller resends `r-1` with total 3999. I predicted Town would **fail** the run | **Town refused the resend** (409 `identity_reuse_rejected`). The verdict was **INCOMPLETE, not FAILED**: `amnesia_survived` had "no replay". The observer contradicts the total (3999 against 3990) and the reply fingerprint |
| `delete-trust` | the seller forgets, applies the work again, and Town absorbs the identical answer | as expected: **Town PASSED, but the memory's history shows two applications** |
| `delete-verify` | the deleted facts are restored from git before the redelivery | as expected: one application, PASSED |
| `crash-restart` | stale fence rejected, work applied once | as expected |

Recorded bundles:

| Case | Run | Bundle fingerprint |
|---|---|---|
| intact | run-c3bc9fb1f486 | `sha256:b6549393dee14f7c0c0c66a22516d52fee0b75ebf4b4710d1a4665c151b1aac5` |
| edit-total-verify | run-f5b6fe03ca99 | `sha256:d685563c3b3dccd80e04879e968674255d123bdea7787acc6b82a4be3c7652d0` |
| edit-total-trust | run-b05d604c6a7e | `sha256:683a0f2875c2c4ddfb73b5c35c24ebab4793608ad52c9bea8d0e74eadf98e4d5` |
| delete-trust | run-3a484ad939a5 | `sha256:61b0161e0818051b88ff96cf48c318f70c70c381bc4a3ced7bd57ae3021524a1` |
| delete-verify | run-bcf5573a0979 | `sha256:25a3378d3c53b8749f9a0025e62f43f1efe47a25cc71f7a3746edd6705284c32` |
| crash-restart | run-69cf06fb9a85 | `sha256:e0174c9c3386382161351ab8149d2daa35285e8c182a899e95e46419955e0c00` |

All six pass `nandatown verify` from this directory. The matrix also
matched in three back-to-back reruns (18 runs) before this evidence was
recorded.

## What this shows about compatibility

- **A git-backed memory works as a Track participant's journal** without
  any change to Town. The stock seller keeps a SQLite journal. This seller
  keeps a git repository. With intact memory, Town records the same kinds
  of events and the same reply fingerprint (`sha256:174cc1a8…`) as for its
  stock seller, and gives the same verdict.
- **Town's message identity is a live check on memory, but only for
  content Town already holds.** When the edited memory changed the reply,
  the coordinator refused the resend, because the same identity with
  different content is rejected. That works only because the reply ID is
  derived from the request ID and Town had already accepted the original.
  An edit to anything Town never saw would not be caught this way.
- **The evaluator records the damage but doesn't name it.** In
  `edit-total-trust`, the seller's acknowledgement asserts
  `"total_cents": 3999` (event `ev-16`), and the `processed` stage still
  passes. The verdict is INCOMPLETE because one replay is missing, not
  because the seller asserted a wrong total. The observer names the cause:
  the remembered total contradicts the one the buyer received.
- **Some damage Town can't see at all.** In `delete-trust`, the seller did
  the work twice. Town's verdict is PASSED, as it is for its own
  `quote-amnesia-restart` seller, whose PR notes the same limit. The memory's
  git history is what records the second application. For a quote that's
  harmless. For a charge it would not be.
- **The memory's evidence reaches Town's bundle.** The seller puts a
  `gitmem` object in its ack note (outside edits found, restored, resend
  refused), so a bundle reader sees what the memory saw without access to
  the seller's machine.

## Integration finding: Town's stop signal reached the memory's git processes

Town stops a participant with `killpg(SIGTERM)`, then `SIGKILL` 0.5 s later.
That signal reaches the participant's whole process group. The first
version of this seller lost its final memory write in two of the six
cases on its first full run. The post-ack "town recorded q-1" commit was cut between writing the
fact file and `git add`, because the `git` child process received Town's
SIGTERM. The leftover files then looked exactly like an outside edit.

The fix is in `gitmem_seller.py`. The store's git commands run in their own
session, and the seller holds a SIGTERM that arrives mid-write until the
write is done. After the fix, the deferral fired in 9 of 18 runs and every
case matched.

This is worth knowing for any participant whose durable state involves
child processes. The runner also discards a participant's stderr, so the
seller writes its log to `STATE_DIR/gitmem_seller.log`.

## Other notes

- `quote-amnesia-restart` tells Town's own seller (through `FAULT`) to wipe
  its journal. This seller acts out the same crash point but keeps its
  memory, because the memory is what's under test. The memory loss is
  reproduced separately, as the `delete` hand edit. For the same reason,
  the evaluator's fixed note for this profile ("the work it forgot before
  the crash was recomputed") is not accurate for the `intact` and `verify`
  cases. There, the seller resent a remembered reply and recomputed
  nothing.
- The hand edit is scripted, inside the seller's launch, to make the case
  reproducible. It stands in for a person with a text editor, and it runs
  before the store is opened.
- A staged but uncommitted change (the store's own interrupted write) is
  not counted as an outside edit. Only the working-tree column of
  `git status` is.

## What remains untested

- **An outside agent.** The seller here is an adapter written for this
  check, and gitmem's author wrote both. It is not an independently
  developed agent. A model-driven seller (gitmem's `llm_agents.py`) on
  the Track was not run.
- **Edits made through git.** A person who edits a fact and commits it is
  not caught by `verify`; only the observer would contradict it. Signed
  commits or a protected store would be needed.
- **Edits to facts Town never saw.** Reputation, preferences, notes about
  a counterparty: Town holds nothing to contradict these. This was the
  original project's main finding in the Lab ("a corrupted price is
  caught, because a price is checkable; a corrupted reputation is caught by
  nothing"), and it is not exercised on the Track.
- **A live observer.** Town's events endpoint is admin-only, so the
  observer runs after the run over the bundle. The seller cannot consult
  Town's record before acting; only the coordinator's 409 reaches it live.
- **Other faults and roles:** `quote-lost-ack`, `quote-duplicate-delivery`,
  `quote-drop-wakeup`, the LLM profiles, the buyer role, `--identity`
  grants, A2A/Path, and Windows (the process-group handling is POSIX only).
- **Memory across runs or machines,** and the access-policy half of gitmem
  (who may read which lane), which needs more than one memory holder.

## Versions

| | |
|---|---|
| Town | `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`, nandatown 0.2.0, Track evaluator 0.5.0 |
| Python | 3.13.0 (macOS) |
| gitmem | `amangang3/nandatown-memory-mvp@121a10dc214a6f7475ff764a33f23d2be6d63d9d` |
| Adapter | `gitmem_seller.py` `sha256:eb31688711d3c156dd007be0bcdb49a32054ce9f9c8ff9710b4d62c6ced5cc53` |
| Unpinned | the seller runs as `cmd`, so Town records no immutable release; each `gitmem-check.json` records the gitmem commit and adapter hash itself |

Credit: gitmem's `store.py` is adapted from
[git-for-memory](https://github.com/akg745/git-for-memory) (akg745), which
is why it is imported from its own repository and not copied here.
