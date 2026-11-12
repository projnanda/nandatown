NANDA Town System Fitness Report
========================================
Run:       run-4cbd86fe3588
Profile:   quote-crash-restart (fault: crash_after_claim)
Task:      quote 2 x widget at 1995 cents, expecting 3990 cents
Releases:  evaluator 0.5.0, nandatown 0.2.0, python 3.12.10
Started:   2026-11-12 01:53:04 UTC
Verdict:   INCOMPLETE
First broken stage: received (no acknowledged receipt by the seller)
Rerun:     nandatown test-agent --profile quote-crash-restart --role seller --cmd '<operator-supplied-command>'

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  accepted                 Passed               the town committed the request before reporting success [ev-4]
  claimed                  Passed               a seller claimed the work under a lease [ev-5, ev-7, ev-9, ev-11, ev-13, ev-15, ev-17, ev-19, ev-21, ev-23, ev-25, ev-27, ev-29, ev-31, ev-33, ev-35, ev-37, ev-39, ev-41, ev-43, ev-45, ev-47, ev-49, ev-51, ev-53, ev-55, ev-57, ev-59, ev-61, ev-63, ev-65, ev-67, ev-70, ev-72, ev-74, ev-76, ev-78, ev-80]
  received                 Inconclusive         the seller acknowledged receipt through a valid fence
                           note: no acknowledged receipt by the seller
  processed                Inconclusive         the seller applied the task exactly once
                           note: no processed acknowledgement
  response                 Inconclusive         the response was accepted and reached the buyer
                           note: no quote response accepted and claimed by the buyer
  correct                  Inconclusive         the buyer checked the total itself
                           note: the buyer made no correctness assertion
  recovered_after_restart  Inconclusive         accepted work survived the crash and was redelivered
                           note: no lease end followed by redelivery
  stale_fence_rejected     Inconclusive         the old attempt could not act after its lease
                           note: no stale fence was rejected
  portable_identity        Not tested           not exercised in this run
                           note: this run used short-lived join tokens; rerun with --identity for grant-based portable identity

Events recorded: 82. Intents recorded: 197.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
