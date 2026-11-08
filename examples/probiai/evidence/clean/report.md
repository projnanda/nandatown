NANDA Town System Fitness Report
========================================
Run:       sim-83e5a418e6a9
Scenario:  probiai-insured-clean (seed 42)
Faults:    duplicate delivery
Agents:    seller-a (seller), seller-b (seller), buyer-1 (buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.9
Duration:  2.0 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/probiai/insured-clean.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  insurer_received        Passed                [ev-38]
                          note: ProbiAI kept all 1 insured payments
  double_payment_verdict  Passed                [ev-37]
                          note: each order paid once, and ProbiAI opened no claim
  ledger_conserved        Passed               money was conserved across every movement [ev-30, ev-36]
                          note: 10000 cents conserved across every movement
  privacy                 Passed               declared private fields never left the run [ev-44]
                          note: fields ['budget_cents'] never left the run

Events recorded: 44. Intents recorded: 15.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
