NANDA Town System Fitness Report
========================================
Run:       path-7217932335ba
Subject:   http://127.0.0.1:8940 (an already-running external agent)
Profile:   a2a-trial-then-hire-unscorable@0.1 (sha256:a808fd6a8c136075)
Condition: duplicate_request: the same logical order is delivered twice
Releases:  evaluator path-trial-hire-0.1, nandatown 0.2.0, python 3.12.14
Started:   2026-11-09 20:08:22 UTC
Verdict:   PASSED
Rerun:     nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire-unscorable@0.1 --pin-card-digest sha256:f4386869a5a275d6826de26b996e50fe10be2dde005ee9d2a3b39bdde4d2b740

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  resolution              Passed               the subject was found through the declared path [ev-1]
  agent_card_retrieval    Passed               the agent card was fetched and digested [ev-2]
                          note: observed card sha256:f4386869a5a275d6
  descriptor_consistency  Passed               the observed card matches the pinned one [ev-3, ev-2]
  protocol_invocation     Passed               a native protocol exchange returned a task [ev-4]
                          note: task task-227a0414f5d3 state completed
  semantic_result         Passed               the task produced the required result [ev-5]
                          note: live run: job job-1a02803b030f was rejected before any trial ('This job asks for a persuasive, subjective cover letter rather than extracting objectively scored invoice fields.'); no candidate was paid
  duplicate_request       Passed               the duplicate order caused no second fulfillment [ev-7]
                          note: the same logical order was delivered twice; no second distinct fulfillment appeared

Events recorded: 7. Intents recorded: 4.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
