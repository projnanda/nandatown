NANDA Town System Fitness Report
========================================
Run:       run-69cf06fb9a85
Profile:   quote-crash-restart (fault: crash_after_claim)
Task:      quote 2 x widget at 1995 cents, expecting 3990 cents
Releases:  evaluator 0.5.0, nandatown 0.2.0, python 3.13.0
Started:   2026-11-11 14:53:46 UTC
Verdict:   PASSED
Rerun:     nandatown test-agent --profile quote-crash-restart --role seller --cmd '<operator-supplied-command>'

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  accepted                 Passed               the town committed the request before reporting success [ev-4]
  claimed                  Passed               a seller claimed the work under a lease [ev-5, ev-11]
  received                 Passed               the seller acknowledged receipt through a valid fence [ev-13]
  processed                Passed               the seller applied the task exactly once [ev-13]
  response                 Passed               the response was accepted and reached the buyer [ev-12, ev-14]
  correct                  Passed               the buyer checked the total itself [ev-15]
  recovered_after_restart  Passed               accepted work survived the crash and was redelivered [ev-6, ev-7, ev-11, ev-9]
  stale_fence_rejected     Passed               the old attempt could not act after its lease [ev-7]
  portable_identity        Not tested           not exercised in this run
                           note: this run used short-lived join tokens; rerun with --identity for grant-based portable identity

Events recorded: 17. Intents recorded: 16.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
