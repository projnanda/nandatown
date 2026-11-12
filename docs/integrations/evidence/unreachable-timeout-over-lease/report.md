NANDA Town System Fitness Report
========================================
Run:       run-eeae9274465e
Profile:   quote-crash-restart (fault: crash_after_claim)
Task:      quote 2 x widget at 1995 cents, expecting 3990 cents
Releases:  evaluator 0.5.0, nandatown 0.2.0, python 3.12.10
Started:   2026-11-12 01:52:07 UTC
Verdict:   INCOMPLETE
First broken stage: received (no acknowledged receipt by the seller)
Rerun:     nandatown test-agent --profile quote-crash-restart --role seller --cmd '<operator-supplied-command>'

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  accepted                 Passed               the town committed the request before reporting success [ev-4]
  claimed                  Passed               a seller claimed the work under a lease [ev-5, ev-8, ev-11, ev-14, ev-17, ev-20, ev-23, ev-26, ev-29, ev-32, ev-35, ev-38, ev-41, ev-44, ev-47, ev-50, ev-53, ev-57, ev-59, ev-61]
  received                 Inconclusive         the seller acknowledged receipt through a valid fence
                           note: no acknowledged receipt by the seller
  processed                Inconclusive         the seller applied the task exactly once
                           note: no processed acknowledgement
  response                 Inconclusive         the response was accepted and reached the buyer
                           note: no quote response accepted and claimed by the buyer
  correct                  Inconclusive         the buyer checked the total itself
                           note: the buyer made no correctness assertion
  recovered_after_restart  Passed               accepted work survived the crash and was redelivered [ev-6, ev-9, ev-12, ev-15, ev-18, ev-21, ev-24, ev-27, ev-30, ev-33, ev-36, ev-39, ev-42, ev-45, ev-48, ev-51, ev-54, ev-7, ev-10, ev-13, ev-16, ev-19, ev-22, ev-25, ev-28, ev-31, ev-34, ev-37, ev-40, ev-43, ev-46, ev-49, ev-52, ev-56, ev-58, ev-60, ev-62, ev-8]
  stale_fence_rejected     Passed               the old attempt could not act after its lease [ev-7, ev-10, ev-13, ev-16, ev-19, ev-22, ev-25, ev-28, ev-31, ev-34, ev-37, ev-40, ev-43, ev-46, ev-49, ev-52, ev-56, ev-58, ev-60, ev-62]
  portable_identity        Not tested           not exercised in this run
                           note: this run used short-lived join tokens; rerun with --identity for grant-based portable identity

Events recorded: 63. Intents recorded: 158.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
