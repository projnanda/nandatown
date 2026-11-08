# Stellar REDECO compliance agent in the Lab

**Subject:** [`stellar-redeco-agent`](https://github.com/isaisrz/stellar-redeco-agent),
an independently developed A2A agent (Stellar Compliance AI, MIT AI Studio
Fall 2026). It prepares answers to REDECO consumer complaints (CONDUSEF,
Mexico) for a human compliance officer and never files them itself.

**Interface tested:** A2A `message/send` with one JSON event per call and one
`contextId` per run, driven from a Lab scenario. The agent runs in its own
process. Town reaches it only over HTTP.

| File | What it is |
| --- | --- |
| `redeco_plugin.py` | Adapter role `redeco_agent` (forwards inputs to the A2A endpoint and performs the returned actions), three Town fixtures (`redeco_portal`, `redeco_client`, `redeco_officer`) and two validators |
| `redeco_compliance.yaml` | Two synthetic complaints, one folio mistyped by the client, the portal read **duplicated**, the client's first answer **dropped** |
| `redeco_portal_down.yaml` | Boundary case: the regulator portal cannot be read |
| `verify_bundle.py` | Runs `verify_bundle` with this example's validators loaded (see Limitations) |

The portal, client and officer are Town fixtures. Only the agent behind the
URL is independent of Town, so a pass here is a result about that agent and
this adapter, not about the fixtures.

## Reproduce

```bash
# 1. the agent (separate repository)
git clone https://github.com/isaisrz/stellar-redeco-agent && cd stellar-redeco-agent
git checkout v0.1.1
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]' && pytest -q
stellar-redeco-agent serve --port 8950          # leave running

# 2. Town, from this checkout
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
nandatown run examples/integrations/stellar-redeco/redeco_compliance.yaml --out runs
nandatown run examples/integrations/stellar-redeco/redeco_portal_down.yaml --out runs
python examples/integrations/stellar-redeco/verify_bundle.py runs/<run id>
python -m pytest -q tests/test_example_stellar_redeco.py   # no agent needed
```

`REDECO_AGENT_URL` overrides the agent URL in the scenario.
To reproduce the failing baseline, run `v0.1.0` of the agent on another port
and set `REDECO_AGENT_URL` to it.

## What is judged

`redeco_compliance` (one unit of logical time = one CONDUSEF business day,
starting 2026-10-28):

| Stage | Must hold |
| --- | --- |
| `agent_reachable` | the card is read and every call completes; otherwise every other stage is inconclusive, never passed or failed |
| `intake_once` | one case per portal complaint even though the snapshot is delivered twice |
| `typo_linked` | the client's `90424` is linked to the portal's `90824` (one-digit slip, as in production) |
| `portal_counter` | the agent's "X de 10" equals the counter the real portal showed for the same dates on 2026-11-05 (6 de 10, 5 de 10) |
| `lost_message_recovered` | after the client's answer is dropped, the agent reminds once and still produces the draft |
| `grounded_drafts` | each complaint gets a draft, and every citation in it appears in a client message that was actually delivered |
| `human_gate` | only the officer files, only after approving; an agent action that tries to file is recorded and never performed |
| `filed_on_time` | every complaint is filed by its deadline |

`redeco_portal_down`: `fail_closed`. The agent must escalate with a reason,
an owner and a next action, and must not report "nothing open" or produce a draft.

## Observed (Town `6f9a7c0`, evaluator `lab-0.2.6`, seed 42)

| Run | Agent | Verdict | Bundle fingerprint |
| --- | --- | --- | --- |
| baseline | v0.1.0 | FAILED at `grounded_drafts`: no draft for `91058` | `sha256:8cfca0ba4a91b8e3be0d6f9a14d1b73830aef664677de60888e9e22538032b5d` |
| after fix | v0.1.1 | PASSED, 8/8 stages | `sha256:d69ceeb8a282138f1f2a4ccb422ccef55095f330c17fcb2f93365de1f91b5a51` |
| portal down | v0.1.1 | PASSED (`fail_closed`) | `sha256:5f754c355a3cc3d6e3cfef7e6c81a3a3c020a7b0c0375c398b277cc715c9854f` |
| agent not running | none | INCOMPLETE, every stage inconclusive | `sha256:3349d13cfb2548fcd00295856efa08d775246e48ae7c55cc023d2b80bc2cb23d` |

The baseline failure was a real agent defect that the agent's own unit tests
missed. v0.1.0 always cited "the number that made the collection call". For
a complaint without one, that citation could never be grounded, so the agent
blocked the case instead of drafting it. v0.1.1 cites only what the draft
relies on, and adds a regression test.

## Limitations and what remains untested

- `nandatown verify` does not load scenario `plugin_files`. It reports these
  bundles as "evaluator replay mismatch" because the validator is not
  registered at verify time. `verify_bundle.py` loads this one reviewed file
  explicitly. A general opt-in (`verify --plugin`) would be a separate Town change.
- Path compatibility: `nandatown a2a test` passes (valid card, completed
  round trip). `nandatown test-agent --url` fails `semantic_result` by design,
  because the agent is not a quote seller. A domain Path profile would be needed.
- The Lab drives the agent synchronously. Real latency, timeouts mid-run,
  concurrency across runs and agent restarts (state is in memory per
  `contextId`) are not exercised.
- No negative control for `human_gate`: no tested agent version tries to file.
- Synthetic data only. The production agent (Telegram, portal reader, LLM
  drafting) is private and is not tested here.
