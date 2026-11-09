NANDA Town System Fitness Report
========================================
Run:       path-b02781b22f2b
Subject:   http://127.0.0.1:8101/p/mock-a/a2a (an already-running external agent)
Profile:   a2a-invoice-extraction@0.1 (sha256:7592ba5632918388)
Condition: duplicate_request: the same logical order is delivered twice
Releases:  evaluator path-invoice-extraction-0.1, nandatown 0.2.0, python 3.12.15
Started:   2026-11-09 15:31:56 UTC
Verdict:   PASSED
Rerun:     nandatown test-agent --url http://127.0.0.1:8101/p/mock-a/a2a --path-profile a2a-invoice-extraction@0.1 --pin-card-digest sha256:c254b3d0e6ede0e1e442103f6c50489006b397f7b32d25a84156087a0462ff74

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  resolution              Passed               the subject was found through the declared path [ev-1]
  agent_card_retrieval    Passed               the agent card was fetched and digested [ev-2]
                          note: observed card sha256:c254b3d0e6ede0e1
  descriptor_consistency  Passed               the observed card matches the pinned one [ev-3, ev-2]
  protocol_invocation     Passed               a native protocol exchange returned a task [ev-4]
                          note: task task-f754a1a2ea9e state completed
  semantic_result         Passed               the task produced the required result [ev-5]
                          note: task nandatown-invoice-001: the output matches the request's output_schema (types and required keys only; field accuracy is not checked); cost_usd 0.004
  duplicate_request       Passed               the duplicate order caused no second fulfillment [ev-7]
                          note: the same logical order was delivered twice; no second distinct fulfillment appeared

Events recorded: 7. Intents recorded: 4.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
