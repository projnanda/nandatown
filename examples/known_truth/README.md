# Known-truth qualification gate (Track example)

A Town buyer that delegates only to a seller whose qualification card shows
it passed every exact check on a known-answer benchmark. Otherwise it
refuses, records why, and sends no work. The admitted seller can be a real
outside agent, connected through `nandatown mcp serve`.

Both participants are plain standard-library Python with no `nandatown`
import, in the style of `examples/byoa_seller.py`.

**Scripted mode proves gate mechanics only. It runs no model and is not
evidence about any agent.**

## Files

| File | Purpose |
|---|---|
| `qualifying_buyer.py --card PATH` | Checks the card, then refuses (exit 6 or 7) or runs the normal quote task (exit 0) |
| `candidate_seller.py --mode scripted\|live` | Scripted: the mailbox behavior of `byoa_seller.py`. Live: runs an outside project's agent through `nandatown mcp serve` (calls a paid model; not run by the tests) |
| `check_card.py PATH` | Validates a card: exit 0 qualifying, 6 valid but not qualifying, 7 invalid |
| `_town_http.py` | Shared HTTP helper |
| `cards/` | Three cards exported from the project's saved evaluation: one qualifying, one not qualifying (3 of 6 checks), one edited after hashing |
| `evidence/run-9310c30f4b28/` | Recorded live run (see below) |

A card is JSON with public fields only (candidate, model, counts, names of
failed checks). `card_sha256` is the SHA-256 of
`json.dumps(body, sort_keys=True, separators=(",", ":"))`, where `body` is
the card without that key. The buyer recomputes it and fails closed.

## Reproduce

Town commit `6f9a7c0`, Python 3.11.

```
python3.11 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
```

Offline test (no credentials, loopback only, about 19 s):

```
.venv/bin/python -m pytest -q tests/test_known_truth_example.py
```

Each case by hand (`CARD` is a file in `cards/`):

```
nandatown run PROFILE \
  --agent buyer=cmd:"python examples/known_truth/qualifying_buyer.py --card examples/known_truth/cards/CARD" \
  --agent seller=cmd:"python examples/known_truth/candidate_seller.py --mode scripted"
```

Recorded live run, checked offline:

```
nandatown verify examples/known_truth/evidence/run-9310c30f4b28
```

## Results (expected versus observed)

| Case | Profile | Expected | Observed |
|---|---|---|---|
| Qualifying card, scripted seller | `quote-duplicate-delivery` | PASSED; `duplicate_recognized` and `correct` passed; the buyer's acknowledgement note carries the card reference | As expected |
| Not-qualifying card, scripted seller | `quote-clean` | No quote request; refusal `not_qualified` naming the failed checks; buyer exit 6 | As expected; run INCOMPLETE at `accepted` |
| Tampered card, scripted seller | `quote-clean` | Refusal `card_hash_mismatch`; buyer exit 7 | As expected; run INCOMPLETE |
| Qualifying card, live agent, first run | `quote-duplicate-delivery` | PASSED | **INCOMPLETE.** Correct answer, but the agent ended its turn after one job, so `duplicate_recognized` was inconclusive |
| Qualifying card, live agent, rerun | `quote-duplicate-delivery` | PASSED | PASSED; the agent recognized the redelivery and resent the identical response |

The three scripted cases are what the test file asserts. A refusal is
recorded as a `qualification_refused` message plus the seller's
acknowledgement note, which carries the reason. No Town source was changed.

## Live mode

Recorded November 11, 2026. The seller was a separate, private project's
candidate agent (Claude Haiku 5.5, medium effort), given Town's bundled
`quote.read` and `town-protocol` skill text.

```
KNOWNTRUTH_PROJECT_DIR=<project folder> PROJECT_PYTHON=<its python> \
  nandatown run quote-duplicate-delivery \
  --agent buyer=cmd:"python examples/known_truth/qualifying_buyer.py --card examples/known_truth/cards/qualifying__haiku55_medium.json" \
  --agent seller=cmd:"python examples/known_truth/candidate_seller.py --mode live --candidate haiku55_medium --yes"
```

It needs the project checkout and the operator's own model key, so it cannot
be rerun from this repository alone. The bundle in `evidence/` is the record.

The first live run failed in a useful way. With the skill text alone, the
agent treated the job as a one-shot task and stopped listening. Adding the
short `SESSION_RULES` paragraph in `candidate_seller.py` (keep claiming; on a
repeated request id resend the same response and mark it a duplicate) fixed
it on the rerun.

## Limitations

- One synthetic Town task. Being qualified on a billing-records benchmark says little about quoting; this shows the gate and the adapter, not that the benchmark predicts the task.
- One live run per condition; one benchmark and one evaluation run behind the cards.
- Scripted mode proves gate mechanics only.
- The agent has no durable journal; it recognized the redelivery within one session.
- Only the duplicate-delivery fault was exercised.
- The card is hash-checked, not signed. No portable identity. No payments.
