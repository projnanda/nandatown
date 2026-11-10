# SharpeShooter research Track

Town's SharpeShooter profiles exercise a bounded structured-research exchange.
They do not turn Town into a strategy evaluator. SharpeShooter owns the
workflow and receipt proof; Town owns transport observations and
requester-attributed validation stages. The result correlates those two layers
without treating one as proof of the other.

## Frozen input and local fixture reproduction

The only authorized product input is the immutable allowlisted
[SharpeShooter release `hw8-ingress-42ff7b384e92`](https://github.com/sidsanghvi/sharpeshooter-hw8-reproduction/releases/tag/hw8-ingress-42ff7b384e92).
Do not substitute a moving branch or rebuild that release when reproducing the
reviewed integration.

Development checks use the deterministic fixture process in
`tests/fixtures/sharpeshooter_task_fixture.py`. They make no model call, use no
provider credential, and do not execute the released SharpeShooter command.
From a Nanda checkout with development dependencies installed, run the two
fixture-only coordinator cases independently:

```bash
.venv/bin/python -m pytest -q tests/test_sharpeshooter_research_flow.py \
  -k test_fixture_only_healthy_profile_runs_through_the_real_coordinator

.venv/bin/python -m pytest -q tests/test_sharpeshooter_research_flow.py \
  -k test_fixture_only_duplicate_profile_runs_through_the_real_coordinator
```

These tests are deterministic development fixtures. They are not the
registered clean or duplicate cases, do not create or promote registered
evidence, and cannot support a real compatibility claim by themselves.
Registered real cases and evidence remain reserved for Milestone 5.

The adapter launches a configured executable directly with a fixed argument
vector, a minimal allowlisted environment, bounded output, and no shell. That
is a subprocess containment boundary, not a sandbox for hostile code. Its
durable per-request journal records `started` before launch and `completed`
only after validating a bounded project result. A completed entry is replayed
without relaunching; an interrupted `started` entry fails closed because v1 has
no automatic project-level resume protocol.

The requester independently binds the Town response to the request ID and
digest, task digest and content, selected specification, ordered candidate
results, receipt integrity hash, distinct accepting reviewer, OOS budget
transition, receipt consumption, and preselected operator policy. The
operator-policy fields are frozen deterministic inputs, not a live human click.
A receipt hash supplies tamper-evidence only; it is not an independently signed
truth attestation.

## Exact claim boundaries

The following ordered text is part of the frozen contract and travels with the
bounded result:

1. The task is bounded and structured; it is not unrestricted prose research.
2. Task content controls provenance and tested specifications, but the deterministic evaluator does not understand the economic thesis.
3. Synthetic metrics do not validate strategy performance.
4. Town proves transport and attributed requester checks; SharpeShooter proves its workflow/receipt; cross-system evidence correlates them.
5. A receipt hash is tamper-evidence, not an independently signed truth attestation.
6. The run does not prove real-agent reasoning, A2A/MCP conformance, hostile-code sandboxing, production readiness, or arbitrary interoperability.
7. A `started` journal fails closed after interruption; v1 does not promise automatic project-level resumption.
