# FactoryBridge: manufacturing fulfillment over Town HTTP mailbox

FactoryBridge coordinates printing, PCB, purchased parts, inspection, assembly and shipping. This example connects the actual planner, marketplace, supplier simulator and independent checker to Nanda Town. The HTTP workflow has **separate requester and FactoryBridge participant processes**; both join, discover, send, claim and acknowledge through a real loopback TCP coordinator. Neither participant calls TownDB directly.

The original direct-TownDB scripted smoke test remains available. The new live-model command does **not** substitute a scripted model or silently fall back when credentials/inference fail.

## Public runtime and versions

- Town base: `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`.
- HTTP adapter source used for the recorded run: `bb8e0f2` (full hash is in `evidence/http-live/results-http.json`). Subsequent documentation/evidence commits do not change that source.
- Public runtime: [zoeyyyyyyyyyyy/factorybridge-runtime](https://github.com/zoeyyyyyyyyyyy/factorybridge-runtime), pinned to `876bfac929dd225c5693e7d554a18bcd3186ffe8`.
- Original project source: `55fd8d81d52cf554745b91e250e5fa74842fd49e`. Its repository stays private; **reviewers do not need access to it**.

The public runtime is a clean, history-free export. Its manifest verifies 21 unchanged original source files, including the planner, marketplace, simulator, wire types, transport and checker. Only the arbitrary synthetic fixture was extracted into a package resource. It contains no fresh/held-out data, benchmark cases or raw experiment logs, keys, budget journals, uploads or original repository history. The project implementation is not copied into Town.

## Setup

Use Python 3.11+ and a checkout containing this PR. Recorded HTTP validation used Python 3.13.6; see `evidence/http-live/environment.json` for observed dependency versions. Python 3.11/3.12 execution has not been verified here.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'

git clone https://github.com/zoeyyyyyyyyyyy/factorybridge-runtime.git ../factorybridge-runtime
git -C ../factorybridge-runtime checkout 876bfac929dd225c5693e7d554a18bcd3186ffe8
```

No private FactoryBridge checkout, CAD/slicer tools, Docker or hardware is required.

## No-cost checks

```bash
FACTORYBRIDGE_RUNTIME_ROOT="$(cd ../factorybridge-runtime && pwd)" \
  python -m pytest -q tests/test_factorybridge_http.py tests/test_coordinator.py tests/test_schema_sync.py

python examples/factorybridge/run_http_check.py \
  --factory-root ../factorybridge-runtime \
  --out /tmp/factorybridge-http-scripted --scripted

python examples/factorybridge/run_check.py \
  --factory-root ../factorybridge-runtime \
  --out /tmp/factorybridge-db-smoke
```

Use a new output directory each time. `--scripted` explicitly marks a deterministic test double, not live-model evidence. `pytest` never makes a paid model call; the two end-to-end tests require the explicit `FACTORYBRIDGE_RUNTIME_ROOT` environment variable and otherwise skip. Coordinator/redaction/client-guard tests still run without it.

## Real planner run

Supply **your own** model key through an ignored local file. No provider key is sent to Town or the requester. The example below reproduces the recorded model/settings; prices are declared accounting inputs, not a verified provider invoice. Check them with your provider before running.

```bash
export MODEL=gpt-6-luna
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_API_KEY_FILE=/absolute/path/to/your-local-key.txt
export FACTORYBRIDGE_ALLOW_REMOTE=1
export LLM_REASONING_EFFORT=none
export LLM_TOKEN_PARAMETER=max_completion_tokens
export LLM_MAX_CALLS=6
export LLM_MAX_TOTAL_TOKENS=40000
export LLM_MAX_OUTPUT_TOKENS=1400
export LLM_TIMEOUT_S=55
export LLM_INPUT_USD_PER_MILLION=0.10
export LLM_OUTPUT_USD_PER_MILLION=0.50
export LLM_TOTAL_USD_BUDGET=0.25
export LLM_SPEND_LEDGER_FILE="$HOME/.local/state/factorybridge/hw8-model-budget.json"
export SSL_CERT_FILE="$(python -c 'import certifi; print(certifi.where())')"

python examples/factorybridge/run_http_check.py \
  --factory-root ../factorybridge-runtime \
  --out /tmp/factorybridge-http-live --live-model
```

Do not commit the key, budget journal or environment files. The journal requires POSIX locking (macOS/Linux). A live run needs network access to the configured model service and permission to bind loopback sockets. Provider/usage failures stop the check; there is no automatic paid retry. The recorded run reused an already approved cumulative $10 workspace journal; the public command uses a fresh $0.25 journal. Neither cap was binding.

## Expected versus observed

Checks were fixed in the requester before the live run:

- Manufacturing order: all required good units, customer arrival by day 8, simulated spend at most $100; observe a print delay and shipping cancellation/replanning.
- Model: actual provider-reported calls/tokens with complete accounting, matching the configured model, and `scripted_reviewer=false`.
- Identical message retry: Town accepts it as a replay. Changing its body under the same message ID returns HTTP 409.
- Logical duplicate under a new delivery ID: return the cached execution ID; do not execute the planner or charge simulated suppliers again.
- Zero printed quantity: reject before simulation or inference.
- Authentication: wrong join token returns 403; missing participant session returns 401.

Recorded live outcome: **PASS**. GPT-6 Luna made **2 model calls**, reporting **6,248 input / 119 output tokens**. Fulfillment arrived on **day 8**, cost **$42.50 simulated**, and used **25 marketplace calls**. Declared-rate API cost was **$0.0006843**. The logical order executed once, its duplicate reused that execution, and the invalid order executed zero times with zero model calls. Both HTTP authentication checks and the changed-envelope conflict check passed.

The driver starts a bounded coordinator, creates/finishes its run through admin HTTP, then exports redacted participant results and Town events. The coordinator alone owns its database. Leases are 240 seconds; participants are bounded to 180 seconds. A scoped local result journal is written before sending the response, enabling the tested same-process redelivery behavior. No claim of production exactly-once execution is made.

Evidence:

- `evidence/http-live/results-http.json`: model usage, outcome, per-request execution counts, buyer/worker HTTP exchanges and version provenance.
- `evidence/http-live/town-events-http.json`: coordinator custody/events, with ephemeral fences/credentials redacted.
- `evidence/http-live/environment.json`: versions, resource limits and accounting assumptions.
- `evidence/http-live/SHA256SUMS`: integrity hashes of the published evidence.
- The top-level `evidence/results.json` and `evidence/town-events.json` retain the original scripted direct-TownDB run from adapter commit `47dfd8915559fc44432ac72f2fae155a2a6dbe58`; they are not presented as new live-model results.

## Scope and limitations

The real planner invokes supplier tools and reacts to the simulated delay; it is not replaced by Town's reference seller or a hard-coded quote response. Prices, plate usage, suppliers, inspections and delivery remain arbitrary synthetic fixtures. This tests the FactoryBridge coordination workflow; it does not run FactoryOS CAD/slicing, control equipment, pay suppliers or establish physical quality.

Town currently requires quote-shaped `TestProfile.task` metadata, so the profile retains that schema while the manufacturing order travels in `manufacturing.request`/`manufacturing.response` bodies. The example uses its own declared manufacturing evaluator, not Town's stock quote evaluator or standard evidence-bundle badge/receipt verification. A2A, MCP transport, portable identities, remote deployment, coordinator/participant crash recovery and concurrency remain untested. Deduplication is demonstrated for a retained participant journal during this bounded run, not after arbitrary crashes or journal loss. The full Town suite was not run; 32 targeted tests and two independent runtime wheel tests passed.

MIT AI Studio Fall 2026, HW8.
