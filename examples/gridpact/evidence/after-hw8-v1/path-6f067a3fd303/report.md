NANDA Town System Fitness Report
========================================
Run:       path-6f067a3fd303
Subject:   http://127.0.0.1:8941 (an already-running external agent)
Profile:   a2a-order-conflict@0.1 (sha256:1f7448132dad17aa)
Condition: conflicting_retry: the order id is sent again with changed terms {'quantity': 5}
Releases:  evaluator path-order-conflict-0.1, nandatown 0.2.0, python 3.14.7
Started:   2026-11-11 05:03:19 UTC
Verdict:   PASSED
Rerun:     nandatown test-agent --url http://127.0.0.1:8941 --path-profile a2a-order-conflict@0.1

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  resolution              Passed               the subject was found through the declared path [ev-1]
  agent_card_retrieval    Passed               the agent card was fetched and digested [ev-2]
                          note: observed card sha256:53e55f0f047fea73
  descriptor_consistency  Not tested           not exercised in this run
                          note: no pinned digest to compare; observed card digest is in the evidence
  protocol_invocation     Passed               a native protocol exchange returned a task [ev-3]
                          note: task task-e12a349da184 state completed
  semantic_result         Passed               the task produced the required result [ev-4]
                          note: exactly one fulfillment, total 3990
  conflicting_retry       Passed               a reused order id with changed terms was refused [ev-5]
                          note: the order id was reused with changed terms {'quantity': 5} and the task ended 'rejected'; the first quote stands. Town does not check the subject's stated reason

Events recorded: 5. Intents recorded: 4.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
