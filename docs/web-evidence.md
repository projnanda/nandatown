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
python scripts/setup_web_evidence.py
```

The setup script creates `.venv`, installs the project and installs Chromium.
Set the key only in the environment; do not put a real key in a file or commit
it:

```
export DEEPSEEK_API_KEY="..."
```

On PowerShell, use `$env:DEEPSEEK_API_KEY = "..."`. Use the virtual
environment's `nandatown` executable if the environment is not activated.

DeepSeek model `deepseek-flash` is the default. Every request explicitly sends
thinking mode `disabled` and reasoning effort `none`.

## Run the Northstar demo

The normal demo needs one command. It serves the three-page Northstar site on
loopback, runs both agents, prints a compact result panel, and stops the server:

```
nandatown web-evidence
```

Add `--headed` to watch Chromium. Add `--json` when another program needs to
parse stdout.

## Run a custom page

Custom pages must currently use a literal loopback IP such as `127.0.0.1`.
Public URLs and hostnames are temporarily rejected to avoid DNS-rebinding
ambiguity. For example, start the small test fixture in one terminal:

```
python -m http.server 8765 --directory tests/fixtures/web_evidence
```

Run both agents in another terminal by providing both custom inputs:

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

Only a literal loopback IP is accepted. Top-level navigation, redirects and
subresources must stay on the exact starting origin. Public URLs and hostnames
are temporarily disabled, eliminating the separate DNS resolution that could
otherwise permit rebinding. The browser uses a fresh context, blocks service
workers, permissions, downloads, popups and non-GET/HEAD requests, and exposes
no shell or form tool.

Terminal rendering makes control bytes visible instead of executing them. This
does not change prompts, evidence, model responses or JSON artifacts.

These network controls bound a read-only test harness. They are not a general
browser security sandbox and custom pages still run inside Chromium.
