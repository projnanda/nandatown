NANDA Town System Fitness Report
========================================
Run:       run-e2d1614fabdb
Profile:   quote-clean (fault: none)
Task:      quote 2 x widget at 1995 cents, expecting 3990 cents
Releases:  evaluator 0.5.0, nandatown 0.2.0, python 3.13.14
Started:   2026-11-11 18:36:13 UTC
Verdict:   PASSED
Rerun:     nandatown run quote-clean --agent 'seller=cmd:<operator-supplied-command>'

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  accepted           Passed               the town committed the request before reporting success [ev-4]
  claimed            Passed               a seller claimed the work under a lease [ev-5]
  received           Passed               the seller acknowledged receipt through a valid fence [ev-7]
  processed          Passed               the seller applied the task exactly once [ev-7]
  response           Passed               the response was accepted and reached the buyer [ev-6, ev-8]
  correct            Passed               the buyer checked the total itself [ev-9]
  portable_identity  Not tested           not exercised in this run
                     note: this run used short-lived join tokens; rerun with --identity for grant-based portable identity

Events recorded: 11. Intents recorded: 10.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
