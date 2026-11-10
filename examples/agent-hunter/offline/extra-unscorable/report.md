NANDA Town System Fitness Report
========================================
Run:       path-7b5a8118649d
Subject:   http://127.0.0.1:8940 (an already-running external agent)
Profile:   a2a-trial-then-hire-unscorable@0.1 (sha256:a808fd6a8c136075)
Condition: duplicate_request: the same logical order is delivered twice
Releases:  evaluator path-trial-hire-0.1, nandatown 0.2.0, python 3.12.15
Started:   2026-11-09 15:31:42 UTC
Verdict:   FAILED
First broken stage: duplicate_request (idempotency defect: the duplicate produced a second distinct fulfillment; attempt 1 gave job 'job-b1c671ecb574', trial None, attempt 2 gave job 'job-a2ad34355a5c', trial None)
Rerun:     nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire-unscorable@0.1 --pin-card-digest sha256:8e6bb24ab71b5578957573665c36be168ddf13037fb1a8e90d8ff51b6a77bbe1

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  resolution              Passed               the subject was found through the declared path [ev-1]
  agent_card_retrieval    Passed               the agent card was fetched and digested [ev-2]
                          note: observed card sha256:8e6bb24ab71b5578
  descriptor_consistency  Passed               the observed card matches the pinned one [ev-3, ev-2]
  protocol_invocation     Passed               a native protocol exchange returned a task [ev-4]
                          note: task task-b1a4f18c8224 state completed
  semantic_result         Passed               the task produced the required result [ev-5]
                          note: offline run: job job-b1c671ecb574 was rejected before any trial ('Not an invoice-extraction job we can score: the output schema lacks currency, invoice_date, invoice_number, line_items, total, vendor_name.'); no candidate was paid
  duplicate_request       Failed               the duplicate order caused no second fulfillment [ev-5, ev-7]
                          note: idempotency defect: the duplicate produced a second distinct fulfillment; attempt 1 gave job 'job-b1c671ecb574', trial None, attempt 2 gave job 'job-a2ad34355a5c', trial None

Events recorded: 7. Intents recorded: 4.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
