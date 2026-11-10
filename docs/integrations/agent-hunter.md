# Agent-Hunter: testing a hiring agent and its provider agents

[Agent-Hunter](https://github.com/chris-a-ackerman/agent-hunter) is a hiring service for AI agents (MIT AI Studio, Fall 2026). Before a requester
agent delegates an invoice-extraction job, Agent-Hunter gives every candidate provider agent a
short trial with known answers, scores it with confidence intervals, and recommends one (or
reports a tie). This page shows how to rerun Town's checks of it. It's a new path: Town had no
`docs/integrations/` before.

**Topology.** Town (the requester) → Agent-Hunter's A2A door (`message/send`, port 8940) → the
core pipeline (job review → rubric → trial → candidates → scoring → ranking) → the candidate
provider agents, also over A2A. Agent-Hunter lives in its own repository. Its 7 real provider
agents are in a private repository, so the key-free reproduction below uses its mock providers.

| What | Profile | Evaluator |
| -- | -- | -- |
| The hiring agent | `a2a-trial-then-hire@0.1` | `path-trial-hire-0.1` |
| The hiring agent, a job it can't score (failure case) | `a2a-trial-then-hire-unscorable@0.1` | `path-trial-hire-0.1` |
| The hiring agent at the stock 15 s limit (boundary case) | `a2a-trial-then-hire-stock-timeout@0.1` | `path-trial-hire-0.1` |
| A provider agent being hired | `a2a-invoice-extraction@0.1` | `path-invoice-extraction-0.1` |

The profiles are described in the [README](../../README.md#a-hiring-agent-trial-then-hire).

## Versions

| | Version |
| -- | -- |
| Town | this fork's `integration/agent-hunter-trial-then-hire`; Town code last changed at `a409934`, `nandatown 0.2.0` |
| Agent-Hunter | [`chris-a-ackerman/agent-hunter`](https://github.com/chris-a-ackerman/agent-hunter) at `735f2d8` |
| Python | Town ≥ 3.11 (full suite passes on 3.11 and 3.12); Agent-Hunter 3.12 (via [uv](https://docs.astral.sh/uv/)) |

## Reproduce offline, with no keys

Offline mode runs Agent-Hunter's whole hire with stub agents and its five mock providers. It makes
no model calls and needs no `.env` or API key. Every hire result and the agent card say
`"mode": "offline"`, so an offline result is never mistaken for a live one. You need two
terminals.

**Terminal 1: Agent-Hunter** (needs [uv](https://docs.astral.sh/uv/getting-started/installation/)):

```bash
git clone https://github.com/chris-a-ackerman/agent-hunter agent-hunter
cd agent-hunter
git checkout 735f2d848ff0f088259def2ad249497e40bc3c75
uv sync
uv run python -m core.a2a --offline --port 8940
```

It serves the door on 8940. On the first hire it also starts the mock providers on 8101 and a
document server on 8200 (all on `127.0.0.1`), so keep those ports free. WeasyPrint may warn about
pango; offline mode doesn't render PDFs, so that's fine.

**Terminal 2: Town:**

```bash
git clone https://github.com/chris-a-ackerman/nandatown.git
cd nandatown
git checkout integration/agent-hunter-trial-then-hire
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Read the door's card digest with Town's own fingerprint, to pin it.
# The card names Agent-Hunter's commit, so the digest changes with the commit.
# At 735f2d8 it is sha256:8540cafdf22d3ca690b4f625b4ab418fa6f54294bce2054be4eccf3339779e1b.
DIGEST=$(python -c "from nandatown.a2a_adapter import fetch_card; from nandatown.records import fingerprint; print(fingerprint(fetch_card('http://127.0.0.1:8940')))")

nandatown a2a test http://127.0.0.1:8940
nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire@0.1 \
  --pin-card-digest "$DIGEST" --out runs/
nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire-unscorable@0.1 \
  --pin-card-digest "$DIGEST" --out runs/
```

**What to expect** (observed in the clean-clone check below). `a2a test` reports "A2A edge
passed": its quote probe correctly gets a `failed` task, because a quote isn't a hire request. For
the hire:

| Stage | `a2a-trial-then-hire@0.1` | `a2a-trial-then-hire-unscorable@0.1` |
| -- | -- | -- |
| resolution | passed | passed |
| agent_card_retrieval | passed | passed |
| descriptor_consistency | passed | passed |
| protocol_invocation | passed | passed |
| semantic_result | passed: "offline run: job … ranked 5 candidates, verdict …" | passed: "offline run: job … was rejected before any trial …; no candidate was paid" |
| duplicate_request | passed: the resent request returns the stored result | passed |
| **Run verdict** | **passed** | **passed** |

The exact verdict (`tie` or `recommended`) and the recommended mock can vary between runs, because
the mocks' errors are seeded per document and every hire draws new document IDs. The whole run
(the hire and its duplicate) takes about 20 s.

**Provider-level check, still offline.** The door started Agent-Hunter's mock providers, which
also speak A2A. `mock-a` is a valid provider. `mock-e` returns output that isn't an invoice, so it
is a negative control:

```bash
nandatown test-agent --url http://127.0.0.1:8101/p/mock-a/a2a --path-profile a2a-invoice-extraction@0.1 --out runs/
nandatown test-agent --url http://127.0.0.1:8101/p/mock-e/a2a --path-profile a2a-invoice-extraction@0.1 --out runs/
```

Expected: `mock-a` passes (5 of 6 stages passed, `descriptor_consistency` not tested unless you pin
its card the same way). `mock-e` fails `semantic_result`: "output.vendor_name: missing; …". The
profile's document is a synthetic invoice committed at
`examples/agent-hunter/fixtures/d/1070b4b25d3c.pdf` (tag `agent-hunter-fixture-v1`). The mocks
answer from a committed answer key without downloading it.

**Boundary: the stock 15 s limit.** Restart the door with a slow provider deadline, so one hire
takes longer than 15 s, then run the stock-timeout profile:

```bash
# Terminal 1 (stop the door first)
MOCK_TASK_DEADLINE_MS=20000 uv run python -m core.a2a --offline --port 8940
# Terminal 2
nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire-stock-timeout@0.1 \
  --pin-card-digest "$DIGEST" --out runs/
```

Expected: `protocol_invocation` fails with `a2a_timeout` after 15 s, and `semantic_result` and
`duplicate_request` are `not_tested`. One synchronous call at the stock limit can't fit a hire,
which is why the main profile allows 240 s per HTTP phase.

**Outside agent in the pool.** Town's own reference agent can sit in Agent-Hunter's hiring pool. It
offers quotes, not invoice extraction, so it should be listed and excluded with a reason:

```bash
# Terminal 3
nandatown a2a serve --port 8941          # its default port, 8940, is the door's
# Terminal 1 (stop the door first)
PROVIDER_TRANSPORT=a2a PROVIDER_A2A_URLS=http://127.0.0.1:8941 \
  uv run python -m core.a2a --offline --port 8940
```

Then rerun the hire (read `DIGEST` again first). The hire result's `candidates.excluded` names
Town's agent with the reason it was left out, and the hire goes ahead with the mocks. This is
Town's own fixture used as an outside, non-native A2A agent. It's not evidence that an independent
agent is compatible.

## Verify the recorded runs

The recorded bundles are in [`examples/agent-hunter/`](../../examples/agent-hunter). `verify`
replays the evaluator over the recorded observations; it doesn't call Agent-Hunter again.

```bash
nandatown verify examples/agent-hunter/offline/run3-trial-then-hire
nandatown verify-receipt examples/agent-hunter/offline/run3-trial-then-hire/receipt.json \
  --bundle examples/agent-hunter/offline/run3-trial-then-hire
```

| Folder | Mode | What it is |
| -- | -- | -- |
| [`baseline/`](../../examples/agent-hunter/baseline) | live | The live run before the idempotency fix: `duplicate_request` **failed** (a second job and trial, at extra cost) |
| [`fixed-door/`](../../examples/agent-hunter/fixed-door) | live | The same run after the fix: all 6 stages passed, and `nandatown proof` granted TOWN-TESTED. The unscorable job also passed (rejected, $0 to providers) |
| [`offline/`](../../examples/agent-hunter/offline) | offline | Runs on the door before the fix: the hire (duplicate failed), the stock-timeout boundary, the unscorable job, and the provider profile against `mock-a` (passed) and `mock-e` (failed) |
| [`outside-agent/`](../../examples/agent-hunter/outside-agent) | offline | Town's reference agent excluded with a reason, and a misleading card that claims invoice extraction, gets trialed, scores 0 and isn't recommended |

Some live runs keep only the receipt, attestation, manifest and run record, not the full bundle,
so that no per-provider results are published. Their READMEs say which, and what was withheld.
For those, `verify` can't replay the run, but the signed claim still checks:

```bash
nandatown verify-receipt examples/agent-hunter/fixed-door/live-trial-then-hire/receipt.json
```

## Live runs (for the Agent-Hunter team)

Live mode is the same door without `--offline`. It needs a Parley API key in Agent-Hunter's `.env`,
and the private provider service on port 8100 with `PROVIDER_TRANSPORT=a2a`. Each hire costs about
$0.09. The commands and results are in `examples/agent-hunter/baseline/README.md` and
`examples/agent-hunter/fixed-door/README.md`.

## What this doesn't show

- Only one synchronous `message/send` with a long timeout. Async `tasks/get` polling and streaming
  aren't tested, and `timeout_seconds` is per HTTP phase, not a total deadline.
- Whether the recommended provider is actually the best one. Town checks that the verdict holds
  together, not the trial's answer key. The provider profile checks output types, not field
  accuracy.
- Discovery through a deployed NANDA Index (only direct URLs), agent identity and signing beyond
  Town's receipt key, real payment, and the hire step itself.
- Independent operator reproduction: all recorded runs were made by the Agent-Hunter team.
  Offline mode is what lets anyone else repeat them.

The PR's "Results and limitations" section has the full table.
