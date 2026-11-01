# Two-agent Full-Page evidence and PromptArmor

`nandatown web-evidence` is the unfiltered baseline. Agent 1 selects relevant
pages through bounded read-only Playwright tools. The complete visible text of
each selected page becomes one source-linked EvidenceItem. Agent 2 receives
the original question and EvidencePacket and returns a JSON answer with evidence
IDs. The Agent implementations and their system prompts are shared unchanged.
No substring selection or silent packet truncation is allowed. Baseline defaults
remain `deepseek-flash`, thinking disabled, reasoning effort none, temperature 0.

`nandatown web-evidence-protected` replaces the former whitespace-marking defense
with PromptArmor. Its defaults are GPT-4.1/temperature 0 for both agents and
GPT-6 Luna/reasoning medium for the guard. The guard does not send temperature
when reasoning is active. No marking or security appendix is added to Agent 2.
This is a behavior change to the existing protected command and its artifact
contract, identified as `promptarmor-handoff@1`. Historical marking artifacts
retain their historical interpretation; do not relabel them as PromptArmor runs.

## Install and controlled inputs

Python 3.11 or 3.12:

```bash
python -m pip install -e '.[dev,web-evidence]'
python -m playwright install chromium
python -m http.server 8765 --bind 127.0.0.1 --directory tests/fixtures/web_evidence
```

The last command stays running. Set the credential in the environment, not a
file or command argument: baseline uses `DEEPSEEK_API_KEY`; protected uses
`OPENAI_API_KEY`. Use a dedicated test account if running models. These are
explicit live commands; none is part of ordinary mock acceptance tests.

```bash
nandatown web-evidence --url http://127.0.0.1:8765/clean.html --question "When was Project Alpha launched?" --out runs/clean-baseline
nandatown web-evidence-protected --url http://127.0.0.1:8765/clean.html --question "When was Project Alpha launched?" --out runs/clean-protected
nandatown web-evidence-protected-replay --packet runs/clean-protected/handoff.json --out runs/clean-replay
```

Replay starts no browser or collector and uses the packet's question. Protected
and replay accept `--model`, `--base-url`, `--agent-key-env`, `--guard-model`,
`--guard-base-url`, `--guard-key-env` and `--guard-reasoning-effort`. The default
medium effort is omitted instead when a GPT-4.1 guard is selected; explicitly
requesting reasoning for GPT-4.1 is rejected. Agent temperature is always zero.
Protected transport omits reasoning parameters for its GPT-4.1 agents. There is
no automatic HTTP retry or credential fallback.

The baseline's optional `--provider openai` path is retained as before. The new
protected transport uses the OpenAI-compatible contract and accepts only
`--provider openai`. Endpoint overrides must not contain embedded credentials,
query parameters or fragments. Authorization belongs in the selected environment
variable and is never recorded.

## Actual upstream implementation

Four original modules (`config`, `prompts`, `detector`, `sanitize`) from
[hosseinsadeghi/promptarmor](https://github.com/hosseinsadeghi/promptarmor) at
`970d72286643f6faac7ceb8e647df7cbfffb221b` are vendored without algorithm or prompt
changes. `SOURCE.json` records the revision and canonical-LF SHA-256 values; the
MIT license ships in both wheel and source distribution. The small package
initializer is ours. No upstream evaluation harness, model client, scripts,
model weights, tests or datasets are vendored. Git and sibling checkouts are
not runtime dependencies. Imports and source hashes are checked and recorded.

This repository is an independent reimplementation, not the paper authors'
official code. Its broad workflow matches [PromptArmor section 3](https://arxiv.org/html/2507.15219v1#S3):
LLM detection and span extraction, followed by fuzzy removal. Exact prompt
identity with the paper and reproduction of its results are not established.
No new research performance or independent-agent adoption claim is made here.

The real upstream `Detector.detect` calls the real `build_system_prompt` with
the user task and `include_definition=False`; upstream `parse_output` also runs.
Our wrapper then strictly checks the raw JSON to reject the upstream parser's
permissive fallback and type coercions. The actual `sanitize` and
`fuzzy_remove_one` execute with `remove` and `removal_length_guard=3.0`. This is
not a keyword detector or a replacement removal algorithm.

## Boundary, limits and failure case

Every complete `EvidenceItem.text` and nonempty `completion_note` is processed
separately and serially. The question and metadata are not rewritten. Empty
strings skip the guard model. Metadata is not screened, and Agent 1 sees the
original untrusted page before this layer: this is a handoff defense, not
protection for every untrusted ingress point of a full agent application.

Agent defaults: three pages, six decisions, six evidence items, 6,000 estimated
input tokens, 15 seconds/page and 120 seconds combined collection/answering time.
That estimate is approximate, not the model's real tokenizer or context limit.
Guard time is accounted separately: per-request HTTP timeout up to 120 seconds,
600-second processing budget with elapsed-time checks before forwarding, no
chunking, truncation or automatic retry. HTTP timeout controls I/O waits; it is
not a guarantee of immediate cancellation of every OS operation. Existing Agent
2 structured-output correction attempts (maximum two) are preserved.

Malformed JSON, inconsistent decision/spans, a reported span that cannot be
removed, an overbroad match rejected by the upstream length guard, timeout or
endpoint failure stops the whole handoff with `defense_error`. Agent 2 is not
called and original text is never forwarded as fallback. This can reduce
availability. A useful bounded failure check is an upstream decision that flags
an absent span: the mock regression asserts zero answerer calls. No network is
needed to exercise it.

Removing all reported spans does not mean every malicious span was found.
Upstream removes only the first occurrence per span. Partial/incomplete detection
and false positives remain possible. `completed` and valid citation IDs do not
establish factual correctness, semantic citation support or attack resistance.

## Artifacts and provenance

Output directories must be new or empty. Baseline artifacts keep their prior
meaning. Protected saves:

- `config.json`: original Agent prompts and effective Agent/guard settings.
- `handoff.json`: original untouched packet; replay preserves the supplied bytes.
- `agent2_handoff.json`: exact sanitized packet, written only after all fields pass.
- `promptarmor.json`: original and cleaned text separately, independent labeled
  hashes, upstream decisions, raw guard output and precise deletion operations.
- `manifest.json`: pinned upstream identity, source hashes and actual import paths.
- `events.jsonl`: actual request bodies, successful raw responses, usage and errors;
  no authorization headers. Error metadata is bounded; raw server error messages
  are not stored because they can echo credentials.
- `result.json`: status, answer, split Agent1/guard/Agent2 usage and separate times,
  explicit provenance limitations and `guardrail_version`.

Source SHA-256, offsets and `validation=verbatim_match` in the presentation copy
are **original-source metadata only**, never validation of cleaned text. The
sidecar's independently labeled cleaned hash and deletion map describe the
cleaned text. IDs and URLs remain stable for citations. Sanitized text must not
be interpreted as a verbatim quote of the original page.

A current-thread profiling callback observes the actual upstream removal return
frames without changing the algorithm. Each successful deletion records its
order, current range, removed text and original ranges. Ranges are half-open
Python Unicode code-point indices, not UTF-8 bytes or UTF-16 units. Later fuzzy
matches can bridge an earlier deletion and map to multiple original ranges.
Reconstructing the cleaned output from original text and these ranges must
exactly match upstream output; otherwise forwarding stops. Failed partial
sanitization records a candidate and validated deletion audit explicitly marked
not authorized for delivery. The original packet remains available for review.

Saved page/model content can itself be sensitive. Credential headers are not
saved, but this is not general content redaction; review artifacts before sharing.

## Browser boundary and offline verification

Only literal loopback IPs are accepted. Navigation, redirects and subresources
must stay on the initial origin. The fresh browser context blocks service
workers, permissions, downloads, popups and non-GET/HEAD requests. No shell or
form tools exist. This bounds the harness, not a general browser sandbox.

```bash
python -m pytest -q tests/test_promptarmor_integration.py tests/test_promptarmor_cli.py tests/test_web_evidence.py tests/test_web_evidence_cli.py
```

PromptArmor tests use HTTP MockTransport and a fake browser, while exercising
real upstream prompting, detection, parsing and removal. A network-blocking
fixture fails even if a connection error is caught by application code. Baseline
contains a controlled loopback Playwright smoke and a paid DeepSeek smoke guarded
by two explicit environment opt-ins; neither is needed for PromptArmor tests.
CI needs no model credentials. Live efficacy has not been established by these
mock tests. No real-agent compatibility or independent adoption is implied.
