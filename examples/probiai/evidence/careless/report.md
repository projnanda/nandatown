NANDA Town System Fitness Report
========================================
Run:       sim-64e309e2e79d
Scenario:  probiai-insured-careless (seed 42)
Faults:    duplicate delivery
Agents:    seller-a (seller), seller-b (seller), buyer-1 (careless_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.9
Duration:  2.0 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/probiai/insured-careless.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  insurer_received        Passed                [ev-38, ev-44]
                          note: ProbiAI kept all 2 insured payments
  double_payment_verdict  Passed                [ev-37, ev-43, ev-45]
                          note: the ledger shows order-buyer-1-1 paid twice; ProbiAI opened a claim by itself
  ledger_conserved        Passed               money was conserved across every movement [ev-30, ev-36, ev-43]
                          note: 10000 cents conserved across every movement
  privacy                 Passed               declared private fields never left the run [ev-46]
                          note: fields ['budget_cents'] never left the run

Events recorded: 46. Intents recorded: 16.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
