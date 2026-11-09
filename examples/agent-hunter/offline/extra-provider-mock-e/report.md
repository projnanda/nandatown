NANDA Town System Fitness Report
========================================
Run:       path-a90fbbc9663e
Subject:   http://127.0.0.1:8101/p/mock-e/a2a (an already-running external agent)
Profile:   a2a-invoice-extraction@0.1 (sha256:7592ba5632918388)
Condition: duplicate_request: the same logical order is delivered twice
Releases:  evaluator path-invoice-extraction-0.1, nandatown 0.2.0, python 3.12.15
Started:   2026-11-09 15:31:57 UTC
Verdict:   FAILED
First broken stage: semantic_result (the provider result is not a valid extraction: output.vendor_name: missing; output.invoice_number: missing; output.invoice_date: missing; output.currency: missing; output.line_items: missing; and 4 more schema mismatches)
Rerun:     nandatown test-agent --url http://127.0.0.1:8101/p/mock-e/a2a --path-profile a2a-invoice-extraction@0.1 --pin-card-digest sha256:c094748a722b8f9293f5de4f3e6f656ef9640be7c0401b7a9c3d81df7195c491

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  resolution              Passed               the subject was found through the declared path [ev-1]
  agent_card_retrieval    Passed               the agent card was fetched and digested [ev-2]
                          note: observed card sha256:c094748a722b8f92
  descriptor_consistency  Passed               the observed card matches the pinned one [ev-3, ev-2]
  protocol_invocation     Passed               a native protocol exchange returned a task [ev-4]
                          note: task task-084b3d38b727 state completed
  semantic_result         Failed               the task produced the required result [ev-5]
                          note: the provider result is not a valid extraction: output.vendor_name: missing; output.invoice_number: missing; output.invoice_date: missing; output.currency: missing; output.line_items: missing; and 4 more schema mismatches
  duplicate_request       Passed               the duplicate order caused no second fulfillment [ev-7]
                          note: the same logical order was delivered twice; no second distinct fulfillment appeared

Events recorded: 7. Intents recorded: 4.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
