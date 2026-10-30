# Two-agent web evidence baseline

This baseline has two model participants and ordinary infrastructure around
them. Agent 1 uses a small read-only Playwright tool surface to inspect web
pages and select exact source quotes. Agent 2 receives only the original
question and that structured evidence packet, then returns a cited answer.

It is deliberately not a prompt-injection defense. Page text is not classified,
sanitized, rewritten or removed because it looks like an instruction. The code
checks source locations, JSON shapes and citation IDs; those are ordinary data
integrity checks, not evidence that a citation semantically supports a claim.

## Install

Python 3.11 or newer is required. From the repository root:

```
python -m venv .venv
python -m pip install -e ".[dev,web-evidence]"
playwright install chromium
```

Use the virtual environment's Python and `nandatown` executable where your
shell does not activate it automatically. Set the key only in the environment;
do not put a real key in `.env.example` or commit it:

```
export DEEPSEEK_API_KEY="..."
```

DeepSeek model `deepseek-flash` is the default. Every request explicitly sends
thinking mode `disabled` and reasoning effort `none`.

## Run the clean local fixture

Start a local server in one terminal:

```
python -m http.server 8765 --directory tests/fixtures/web_evidence
```

Run both agents in another terminal:

```
nandatown web-evidence \
  --url http://127.0.0.1:8765/clean.html \
  --question "When was Project Alpha launched?" \
  --out runs/web-evidence-clean
```

Add `--headed` to show Chromium for debugging. The defaults allow three pages,
six Agent 1 decisions, six evidence items, 6,000 estimated input tokens per
request, 15 seconds per page and 120 seconds overall. The CLI exposes bounded
overrides for the page, step, input and timeout limits.

The output directory must be new or empty. It contains:

- `config.json`: exact prompts, model, URL and runtime limits;
- `events.jsonl`: model requests and responses, tool calls, usage and errors;
- `pages/`: bounded visible-text snapshots, DOM blocks, URLs and hashes;
- `handoff.json`: exactly the EvidencePacket received by Agent 2;
- `result.json`: status, stop reason, cited answer and explicit limitations.

API keys and authorization headers are never included. The saved web and model
content can itself be sensitive; review artifacts before sharing them.

## Tests

The normal suite uses deterministic fake model responses and makes no paid API
calls:

```
python -m pytest -q tests/test_web_evidence.py
python -m pytest -q
```

The Playwright smoke reads only the controlled loopback fixture. A real
DeepSeek end-to-end smoke exists for manual use and requires two separate
opt-ins:

```
export DEEPSEEK_API_KEY="..."
export NANDATOWN_RUN_LIVE_DEEPSEEK=1
python -m pytest -q tests/test_web_evidence.py -k manual_live
```

Do not set that opt-in in CI. No real DeepSeek run or empirical model finding is
claimed by this baseline contribution.

## Browser boundary

Top-level navigation and redirects stay on the starting origin. Public
cross-origin assets may load, but private, link-local, reserved and metadata
destinations are blocked. Loopback is accepted only when the starting page is
loopback. The browser uses a fresh context, blocks service workers, permissions,
downloads, popups and non-GET/HEAD requests, and exposes no shell or form tool.

These network controls bound a read-only test harness. They are not a general
browser security sandbox and custom pages still run inside Chromium.
