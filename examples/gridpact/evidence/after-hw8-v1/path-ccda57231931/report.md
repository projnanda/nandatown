NANDA Town System Fitness Report
========================================
Run:       path-ccda57231931
Subject:   http://127.0.0.1:8941 (an already-running external agent)
Profile:   a2a-capability-fulfillment@0.3 (sha256:b004f857d3fff2dc)
Condition: duplicate_request: the same logical order is delivered twice
Releases:  evaluator path-0.3, nandatown 0.2.0, python 3.14.7
Started:   2026-11-11 05:03:18 UTC
Verdict:   PASSED
Rerun:     nandatown test-agent --url http://127.0.0.1:8941 --path-profile a2a-capability-fulfillment@0.3

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  resolution              Passed               the subject was found through the declared path [ev-1]
  agent_card_retrieval    Passed               the agent card was fetched and digested [ev-2]
                          note: observed card sha256:53e55f0f047fea73
  descriptor_consistency  Not tested           not exercised in this run
                          note: no pinned digest to compare; observed card digest is in the evidence
  protocol_invocation     Passed               a native protocol exchange returned a task [ev-3]
                          note: task task-d7919c1f4057 state completed
  semantic_result         Passed               the task produced the required result [ev-4]
                          note: exactly one fulfillment, total 3990
  duplicate_request       Passed               the duplicate order caused no second fulfillment [ev-6]
                          note: the same logical order was delivered twice; no second distinct fulfillment appeared

Events recorded: 6. Intents recorded: 4.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
