# Lost Link: end-to-end lost-and-found compatibility

Lost Link is a shared lost-and-found service for personal agents. This example
connects the **separately running, unmodified project** to current Town Lab.
It adds only an HTTP adapter, three scenario files, an evaluator, a bounded
runner, tests and execution evidence. Lost Link's application code stays in
[cathyliu0510/Lost-Link](https://github.com/cathyliu0510/Lost-Link).

## Interface and workflow

Town's local registry discovers the finder and human-review roles. Town's
signed in-memory message transport coordinates a finder participant, an owner
participant and a **scripted human-operator fixture with its own API credential**.
Their adapter calls the real Lost Link FastAPI service over loopback HTTP:

1. Finder registers a found report, including a private verification detail.
2. Owner discovers participants, registers a paraphrased lost report, searches
   and submits a claim for the actual found report returned by the service.
3. Owner checks that pickup instructions are absent while the claim is pending.
4. Operator reads the private inbox and requests clarification; owner answers.
5. Operator approves pickup; only then does the owner receive instructions.
6. Operator simulates confirmation of a hand-over; owner reads `returned`.

The operator fixture represents the human API boundary; it is not an LLM or
an agent granted approval authority. These three roles share a local test
process, so this does not establish OS-level credential isolation. No model
credentials are required or used. This is project/service compatibility with
scripted clients, not independent adoption of a Town reference seller.

## Versions and setup

- Town upstream base: `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`, version 0.2.0.
- Project: `c26cddbfc9721c12acafa4603f70265c6f37bd11` in the repository above.
- Integration/evaluator contract: `lost-link-lifecycle@1`; Town evaluator `lab-0.2.6`.
- Recorded Python: 3.12.14. CI-supported Python 3.11/3.12 can run this example.
- Exact recorded third-party dependencies: `requirements.lock`.

Start from this PR checkout, based on the Town commit above. Run the commands
from the Town repository root. The external project is a separate sibling:

```bash
git clone https://github.com/cathyliu0510/Lost-Link.git ../Lost-Link
git -C ../Lost-Link checkout c26cddbfc9721c12acafa4603f70265c6f37bd11
python3 -m venv .venv
.venv/bin/python -m pip install -r examples/lost_link/requirements.lock
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python examples/lost_link/run.py --project-root ../Lost-Link --out /tmp/lost-link-town
```

Local socket binding must be permitted. The runner starts a real uvicorn
service on a randomly allocated **127.0.0.1-only** port, with a fresh SQLite
database and ephemeral credentials for each scenario, then stops it. It does
not deploy anything, reset an existing database or contact external services.
Its temporary `NANDATOWN_HOME` and keystore are not included in the output.
The project needs only the service dependencies already installed for Town;
the optional LLM demo dependency is not needed.

## Expected versus observed

| Scenario | Expected | Recorded observation |
| --- | --- | --- |
| `clean.yaml` | Entire return workflow completes | **passed**, 14 project HTTP calls, 2 reports, 1 case, `returned`; reports closed |
| `duplicate-and-misuse.yaml` | Duplicate Town notification causes idempotent API retries; an agent cannot approve | **passed**, 20 calls, still 2 reports/1 case; agent approval HTTP **403**, forged approval field **422**; final `returned` |
| `dropped-review.yaml` | Dropped review notification prevents progress and must not release pickup instructions | **incomplete**, 5 calls; case stays `submitted`, no human actions or pickup instructions |

The dropped review is a real failure to finish the workflow, not a successful
return. The runner exits successfully only because that negative control
produces the predeclared **incomplete** result and the independent database
check confirms the expected pending state. There is currently no retry path
for that lost operator notification. Missing approval/return stages remain
`not_enough_evidence`; they are never reported as passes.

## Evidence and independent checks

[`evidence/summary.json`](evidence/summary.json) records every scenario's
expected/actual verdict, stage results, project request count, wall time,
versions and adapter hash. Each scenario has the five canonical Town records,
their unchanged manifest and a Town attestation. A separate
`service_snapshot.json` records database counts, case states, human actions
and service audit rows, read by the runner outside the participant roles.
Credentials, private service state, keystores and verification-answer values
are excluded. All data and human actions are synthetic.

Offline verification does not call the project, run participants or require
the external checkout. Explicitly load this example's evaluator through the
runner before Town's bundle verifier replays the observations:

```bash
.venv/bin/python examples/lost_link/run.py --verify examples/lost_link/evidence
.venv/bin/python -m pytest -q tests/test_lost_link_integration.py
LOSTLINK_PROJECT_ROOT=../Lost-Link .venv/bin/python -m pytest -q tests/test_lost_link_integration.py
```

The first two commands verify supplied evidence; the last repeats the live
three-scenario check as well. Plain `nandatown verify BUNDLE` in a fresh
process does not load external evaluator plugins; use the documented runner.
Recorded source-tree paths identify the initial operator's checkout and are
not used to import code during replay. No automatic execution of paths from
an evidence bundle is performed.

## Compatibility shown and remaining limits

Exercised: Town local discovery and signed delivery to custom roles; real
Lost Link HTTP schemas, per-agent bearer credentials, human-only approval,
private inbox, report/claim idempotency, clarification, status/pickup gating
and closure. The retrieved candidate is checked against the found ID;
the verifier also rejects removed approval evidence or a substituted target.

Not exercised: a deployed NANDA Index, A2A or MCP wire conformance, portable
Town identity grants, a live LLM, independent human review quality, notification
recovery, concurrent service capacity or physical recovery of an item.
Town's simulated signatures and attestation commit to recorded observations;
they do not independently prove ownership, agent safety or real-world outcomes.

Course context: MIT AI Studio Fall 2026, HW8 submission.
