NANDA Town System Fitness Report
========================================
Run:       path-cc2bc79f9a7e
Subject:   http://127.0.0.1:8940 (an already-running external agent)
Profile:   a2a-trial-then-hire@0.1 (sha256:766de600124b6815)
Condition: duplicate_request: the same logical order is delivered twice
Releases:  evaluator path-trial-hire-0.1, nandatown 0.2.0, python 3.12.14
Started:   2026-11-09 20:35:31 UTC
Verdict:   PASSED
Rerun:     nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire@0.1 --pin-card-digest sha256:3d7bf882d39b5428ba7d87d53c8656a1e05204481c8479823c6861833cd7aaa7

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  resolution              Passed               the subject was found through the declared path [ev-1]
  agent_card_retrieval    Passed               the agent card was fetched and digested [ev-2]
                          note: observed card sha256:3d7bf882d39b5428
  descriptor_consistency  Passed               the observed card matches the pinned one [ev-3, ev-2]
  protocol_invocation     Passed               a native protocol exchange returned a task [ev-4]
                          note: task task-fe94f30c517b state completed
  semantic_result         Passed               the task produced the required result [ev-5]
                          note: offline run: job job-b422e2e8e51a ranked 6 candidates, verdict 'recommended', recommended 'mock-a@1'; Town checked that the verdict holds together, not that the pick is the best provider
  duplicate_request       Passed               the duplicate order caused no second fulfillment [ev-7]
                          note: the same logical order was delivered twice; no second distinct fulfillment appeared

Events recorded: 7. Intents recorded: 4.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
