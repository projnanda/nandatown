NANDA Town System Fitness Report
========================================
Run:       run-b05d604c6a7e
Profile:   quote-amnesia-restart (fault: crash_amnesia)
Task:      quote 2 x widget at 1995 cents, expecting 3990 cents
Releases:  evaluator 0.5.0, nandatown 0.2.0, python 3.13.0
Started:   2026-11-11 14:53:36 UTC
Verdict:   INCOMPLETE
First broken stage: amnesia_survived (no replay of the response followed the restarted seller's claim)
Rerun:     nandatown test-agent --profile quote-amnesia-restart --role seller --cmd '<operator-supplied-command>'

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  accepted           Passed               the town committed the request before reporting success [ev-4]
  claimed            Passed               a seller claimed the work under a lease [ev-5, ev-14]
  received           Passed               the seller acknowledged receipt through a valid fence [ev-16]
  processed          Passed               the seller acknowledged one application; the work it forgot before the crash was recomputed, not skipped [ev-16]
  response           Passed               the response was accepted and reached the buyer [ev-6, ev-8]
  correct            Passed               the buyer checked the total itself [ev-10]
  amnesia_survived   Inconclusive         work the seller forgot and recomputed produced one accepted response
                     note: no replay of the response followed the restarted seller's claim
  portable_identity  Not tested           not exercised in this run
                     note: this run used short-lived join tokens; rerun with --identity for grant-based portable identity

Events recorded: 17. Intents recorded: 11.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
