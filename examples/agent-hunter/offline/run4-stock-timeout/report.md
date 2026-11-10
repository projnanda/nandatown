NANDA Town System Fitness Report
========================================
Run:       path-c5a2bb7c3a89
Subject:   http://127.0.0.1:8940 (an already-running external agent)
Profile:   a2a-trial-then-hire-stock-timeout@0.1 (sha256:a1c6e2617ca0038a)
Condition: duplicate_request: the same logical order is delivered twice
Releases:  evaluator path-trial-hire-0.1, nandatown 0.2.0, python 3.12.15
Started:   2026-11-09 15:32:24 UTC
Verdict:   FAILED
First broken stage: protocol_invocation (a2a_timeout)
Rerun:     nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire-stock-timeout@0.1 --pin-card-digest sha256:8e6bb24ab71b5578957573665c36be168ddf13037fb1a8e90d8ff51b6a77bbe1

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  resolution              Passed               the subject was found through the declared path [ev-1]
  agent_card_retrieval    Passed               the agent card was fetched and digested [ev-2]
                          note: observed card sha256:8e6bb24ab71b5578
  descriptor_consistency  Passed               the observed card matches the pinned one [ev-3, ev-2]
  protocol_invocation     Failed               a native protocol exchange returned a task [ev-4]
                          note: a2a_timeout
  semantic_result         Not tested           not exercised in this run
                          note: not reached: an earlier boundary broke (no successful terminal task output was observed)
  duplicate_request       Not tested           not exercised in this run
                          note: not reached: an earlier boundary broke (the controlled condition was never reached)

Events recorded: 4. Intents recorded: 3.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
