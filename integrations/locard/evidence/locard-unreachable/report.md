NANDA Town System Fitness Report
========================================
Run:       run-40c41b8d66cc
Profile:   quote-clean (fault: none)
Task:      quote 2 x widget at 1995 cents, expecting 3990 cents
Releases:  evaluator 0.5.0, nandatown 0.2.0, python 3.14.7
Started:   2026-11-11 18:02:13 UTC
Verdict:   INCOMPLETE
First broken stage: received (no acknowledged receipt by the seller)
Rerun:     nandatown run quote-clean --agent 'buyer=cmd:<operator-supplied-command>' --agent 'seller=cmd:<operator-supplied-command>'

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  accepted           Passed               the town committed the request before reporting success [ev-4]
  claimed            Passed               a seller claimed the work under a lease [ev-5]
  received           Inconclusive         the seller acknowledged receipt through a valid fence
                     note: no acknowledged receipt by the seller
  processed          Inconclusive         the seller applied the task exactly once
                     note: no processed acknowledgement
  response           Inconclusive         the response was accepted and reached the buyer
                     note: no quote response accepted and claimed by the buyer
  correct            Inconclusive         the buyer checked the total itself
                     note: the buyer made no correctness assertion
  portable_identity  Not tested           not exercised in this run
                     note: this run used short-lived join tokens; rerun with --identity for grant-based portable identity

Events recorded: 11. Intents recorded: 31.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
