NANDA Town System Fitness Report
========================================
Run:       path-f225095edd10
Subject:   http://127.0.0.1:8941 (an already-running external agent)
Profile:   a2a-capability-fulfillment@0.3 (sha256:b004f857d3fff2dc)
Condition: duplicate_request: the same logical order is delivered twice
Releases:  evaluator path-0.3, nandatown 0.2.0, python 3.14.7
Started:   2026-11-11 05:27:22 UTC
Verdict:   FAILED
First broken stage: duplicate_request (idempotency defect: the duplicate produced a second distinct fulfillment)
Rerun:     nandatown test-agent --url http://127.0.0.1:8941 --path-profile a2a-capability-fulfillment@0.3

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  resolution              Passed               the subject was found through the declared path [ev-1]
  agent_card_retrieval    Passed               the agent card was fetched and digested [ev-2]
                          note: observed card sha256:97be0d82b2f66b35
  descriptor_consistency  Not tested           not exercised in this run
                          note: no pinned digest to compare; observed card digest is in the evidence
  protocol_invocation     Passed               a native protocol exchange returned a task [ev-3]
                          note: task task-de6cebef1f46 state completed
  semantic_result         Passed               the task produced the required result [ev-4]
                          note: exactly one fulfillment, total 3990
  duplicate_request       Failed               the duplicate order caused no second fulfillment [ev-4, ev-6]
                          note: idempotency defect: the duplicate produced a second distinct fulfillment

Events recorded: 6. Intents recorded: 4.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
