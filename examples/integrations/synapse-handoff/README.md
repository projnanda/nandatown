# Synapse record handoff

Synapse provides an agent-facing record with scoped access and source citations. This example tests whether two participants can relay a cited claim through NANDA Town and verify what arrived against the live record.

## Workflow

1. The sender joins Town, discovers the verifier, and obtains a cited synthetic mobility claim via the record's `request_brief` MCP tool.
2. The sender submits the claim through Town's HTTP mailbox. The verifier claims that message and passes its received contents to the record's `check_claims` tool under a separate read grant.
3. The verifier acknowledges the claim, sends the verification receipt back through Town, and the sender claims and acknowledges it.

The second run changes one source value before transmission while preserving the citation. Both runs use the real Town coordinator and real MCP tools. The profile's required quote metadata is unused. This example evaluates its own trace.

## Reproduce

Town base `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244` (0.2.0). Recorded project version `c68c7366cc89e96417bfff00419d00eafb4b29c4` (0.0.1). Recorded with Python 3.13.

From the NANDA Town checkout containing this example:

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python examples/integrations/synapse-handoff/run_check.py \
  --verify examples/integrations/synapse-handoff/evidence/local.json
python -m pytest -q tests/test_example_synapse_handoff.py
```

For a live rerun, use an authorized synthetic-only record instance at the indicated project version. Its operator must provision two distinct, current bearer tokens bound to distinct agents. Each agent needs a preapproved read grant for the same synthetic person, the `shift_handoff` purpose, and the `mobility` kind. The record must already contain a cited mobility claim. Set the following in the environment using the endpoint and credentials supplied by the operator:

- `SYNAPSE_HW8_MCP_URL` — the authorized record's HTTPS `/mcp` URL, or an HTTP loopback URL for a local instance.
- `SYNAPSE_HW8_SENDER_TOKEN` and `SYNAPSE_HW8_VERIFIER_TOKEN` — the two scoped bearers.
- `SYNAPSE_HW8_PERSON_ID` — the designated synthetic record ID.

```sh
python examples/integrations/synapse-handoff/run_check.py \
  --project-version c68c7366cc89e96417bfff00419d00eafb4b29c4 \
  --out /tmp/synapse-handoff-evidence.json
python examples/integrations/synapse-handoff/run_check.py \
  --verify /tmp/synapse-handoff-evidence.json
```

The recorded run used a freshly seeded, disposable instance of the actual record over loopback, with synthetic credentials and no model calls. Offline verification needs no endpoint or credentials. A live rerun requires access to the synthetic service.

## Expected and observed

| Case | Expected | Observed |
| --- | --- | --- |
| Faithful | `matches_record`; correlated receipt returned through Town | PASS |
| Altered source value | `differs_from_record` with `value_mismatch`; correlated receipt returned through Town | PASS |

The recorded trace includes accepted, claimed, and acknowledged events for the claim and its receipt in both cases. It contains content fingerprints and verdicts. The raw mailbox database, claim bodies, and credentials are omitted. Offline verification checks trace sequencing and fingerprints against the recorded verdict, while a live rerun calls the record again.

The result establishes that this workflow can carry and check a cited claim through the mailbox. Verification means correspondence to the record. The run uses prepared read grants and covers one synthetic item kind. It leaves live identity provisioning, writes, revocation, deployed discovery, and model behavior untested.
