NANDA Town System Fitness Report
========================================
Run:       sim-9a66d9c60c06
Scenario:  probiai-insured-careless (seed 42)
Faults:    duplicate delivery
Agents:    seller-a (seller), seller-b (seller), buyer-1 (careless_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.9
Duration:  2.0 logical seconds, deterministic
Verdict:   FAILED
First broken stage: insurer_received (2 of 2 copies did not reach ProbiAI)
Rerun:     nandatown run examples/probiai/insured-careless.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  insurer_received        Failed                [ev-38, ev-44]
                          note: 2 of 2 copies did not reach ProbiAI
  double_payment_verdict  Inconclusive
                          note: ProbiAI did not see every payment, so its verdict is not tested
  ledger_conserved        Passed               money was conserved across every movement [ev-30, ev-36, ev-43]
                          note: 10000 cents conserved across every movement
  privacy                 Passed               declared private fields never left the run [ev-45]
                          note: fields ['budget_cents'] never left the run

Events recorded: 45. Intents recorded: 16.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
