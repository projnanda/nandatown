NANDA Town System Fitness Report
========================================
Run:       run-a0b3fa0c5c69
Profile:   quote-lost-ack (fault: lost_ack)
Task:      quote 2 x widget at 1995 cents, expecting 3990 cents
Releases:  evaluator 0.5.0, nandatown 0.2.0, python 3.14.7
Started:   2026-11-11 18:02:23 UTC
Verdict:   PASSED
Rerun:     nandatown run quote-lost-ack --agent 'buyer=cmd:<operator-supplied-command>' --agent 'seller=cmd:<operator-supplied-command>'

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  accepted            Passed               the town committed the request before reporting success [ev-4]
  claimed             Passed               a seller claimed the work under a lease [ev-5]
  received            Passed               the seller acknowledged receipt through a valid fence [ev-11]
  processed           Passed               the seller applied the task exactly once [ev-11]
  response            Passed               the response was accepted and reached the buyer [ev-6, ev-8]
  correct             Passed               the buyer checked the total itself [ev-9]
  ack_retry_survived  Passed               a lost acknowledgement was retried and recorded [ev-7, ev-11]
  portable_identity   Not tested           not exercised in this run
                      note: this run used short-lived join tokens; rerun with --identity for grant-based portable identity

Events recorded: 12. Intents recorded: 7.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
