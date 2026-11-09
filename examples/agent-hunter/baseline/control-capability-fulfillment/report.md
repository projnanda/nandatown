NANDA Town System Fitness Report
========================================
Run:       path-c177e2a149de
Subject:   http://127.0.0.1:8940 (an already-running external agent)
Profile:   a2a-capability-fulfillment@0.3 (sha256:b004f857d3fff2dc)
Condition: duplicate_request: the same logical order is delivered twice
Releases:  evaluator path-0.3, nandatown 0.2.0, python 3.12.14
Started:   2026-11-09 18:07:21 UTC
Verdict:   FAILED
First broken stage: protocol_invocation (expected successful terminal task state 'completed', observed 'failed')
Rerun:     nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-capability-fulfillment@0.3

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  resolution              Passed               the subject was found through the declared path [ev-1]
  agent_card_retrieval    Passed               the agent card was fetched and digested [ev-2]
                          note: observed card sha256:3620f36b732f51a2
  descriptor_consistency  Not tested           not exercised in this run
                          note: no pinned digest to compare; observed card digest is in the evidence
  protocol_invocation     Failed               a native protocol exchange returned a task [ev-3]
                          note: expected successful terminal task state 'completed', observed 'failed'
  semantic_result         Not tested           not exercised in this run
                          note: not reached: an earlier boundary broke (no successful terminal task output was observed)
  duplicate_request       Not tested           not exercised in this run
                          note: not reached: an earlier boundary broke (the controlled condition was never reached)

Events recorded: 3. Intents recorded: 3.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
