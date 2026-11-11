# MindVault seller (MAS.665 HW8)

A Track seller whose price list lives in [MindVault](https://github.com/welkinarrow/MindVault),
a shared memory service for agents with access levels, session taint and an LLM
reviewer. MindVault runs as its own service; this folder holds only the integration.

| File | What it does |
| --- | --- |
| `mv_seller.py` | Seller on Town's HTTP contract (based on `examples/byoa_seller.py`). `MEMORY=mindvault` reads the unit price from MindVault block `b-701` (L2, finance key), replies, then writes a deal note that MindVault labels L2 from session taint. `MEMORY=off` uses `PRICE_CENTS` and writes nothing. Standard library only. |
| `intern_probe.py` | After a run, reads every deal note by id with MindVault's L0 `intern` key and runs fixed searches. Only `LEVEL_TOO_LOW` counts as protected; `NOT_FOUND` (a quarantined note) is reported apart. |
| `run_compare.sh` | 2 conditions x 2 profiles (`quote-clean`, `quote-crash-restart`) x 3 runs. Resets MindVault's database and restarts the service before every MindVault run. |
| `summarize.py` | `results.csv`, one row per run, from Town bundles and the seller's call log. |
| `../../../tests/test_example_mindvault.py` | Both modes and the crash case against a mock MindVault, so CI needs no key or service. |

## Versions

- Town: `projnanda/nandatown` main at `6f9a7c0`.
- MindVault: tag `hw8-base` (`c8b209e`) on `welkinarrow/MindVault`. Reviewer model `claude-sonnet-4-6`.

## Reproduce

```bash
# MindVault (separate checkout; needs an Anthropic key for its reviewer)
git clone https://github.com/welkinarrow/MindVault && cd MindVault && git checkout hw8-base
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/gen_keys.py                  # writes config/agents.json
cp hw8/mindvault.env.example ~/.config/mindvault.env  # fill in your key

# Town
cd nandatown && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
source ~/.config/mindvault.env
MINDVAULT_DIR=/path/to/MindVault bash examples/integrations/mindvault/run_compare.sh

# No key: mock tests only
.venv/bin/python -m pytest -q tests/test_example_mindvault.py
```

One run by hand (MindVault already started on port 8000):

```bash
MEMORY=mindvault MINDVAULT_URL=http://127.0.0.1:8000/mcp \
MINDVAULT_AGENTS=/path/to/MindVault/config/agents.json MV_LOG=mv-calls.jsonl \
nandatown run quote-crash-restart --agent seller="cmd:python examples/integrations/mindvault/mv_seller.py"
```

## Failure case: crash and restart

Per request the seller reads b-701, writes the deal note, replies, journals, acks.
The note comes before the reply because MindVault reviews each write synchronously
with an LLM (1.5 to 9 s observed) and Town's runner allows a seller only about 8 s
after the buyer has its answer. With the note after the reply, all three MindVault
crash-restart runs ended INCOMPLETE (seller stopped before its ack); those bundles are
kept as evidence of that first design.

Under `quote-crash-restart` the seller reads the price and writes the deal note for its
first claim, then "crashes" before replying: it stalls past the lease, shows the stale
fence is refused, and exits 3. Town restarts it and redelivers. The restarted seller has
no journal entry, so it applies the work again. MindVault's `memory_write` has no
idempotency key, so a second deal note is written. This is the edge case we measure
(row 5 of the results table), not a bug we hide.

## Results (Nov 11, 2026; 12 runs, `evidence/results.csv`, bundles in `evidence/bundles/`)

| # | Condition x profile | Expected | Observed (3 of 3 runs each) |
| --- | --- | --- | --- |
| 1 | off x clean | All Town stages pass; no MindVault calls | PASSED, 6/6 stages, 0 calls, ~0.35 s |
| 2 | mindvault x clean | All stages pass; deal note L2 and approved; intern cannot read it | PASSED, 6/6; 2 calls; note L2 `unverified` (reviewer pass); intern `LEVEL_TOO_LOW`; 0 search hits; 1.6-2.6 s |
| 3 | off x crash-restart | All stages pass | PASSED, 8/8 incl. `stale_fence_rejected`, `recovered_after_restart`; ~3 s |
| 4 | mindvault x crash-restart | Stages pass; L2 after restart; intern cannot read | PASSED, 8/8; 4 calls; both notes L2 (`session_max_read`); intern `LEVEL_TOO_LOW` on the approved note; ~6.7 s |
| 5 | mindvault x crash-restart, duplicate | Staging may hold 2 notes | Reproduced: 2 notes every run. The reviewer quarantined the second as an "exact duplicate of existing block w-0001", so only one is published. The intern gets `NOT_FOUND` for it (hidden by review, not by level) |

The reviewer caught the duplicate here only because it saw w-0001 as context. That is
a side effect, not idempotency: a `request_id` on `memory_write` would be the real fix.
Every committed bundle passes `nandatown verify` (hashes match, evaluator reproduces).

## Not tested

One seller, one task (2 widgets). Track's other faults (duplicate delivery, lost ack,
amnesia) not run. No LLM agent as the seller. 3 runs per combination. The probe
covers the intern only, by id and three fixed searches. The mock test checks the
seller's calls, not MindVault's policy.
