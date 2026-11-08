NANDA Town System Fitness Report
========================================
Run:       sim-a455e3eaab54
Scenario:  probiai-insured-repeat (seed 42)
Faults:    duplicate delivery
Agents:    seller-a (seller), seller-b (seller), buyer-1 (buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.9
Duration:  4.3 logical seconds, deterministic
Verdict:   FAILED
First broken stage: double_payment_verdict (false alarm: ProbiAI opened a double-payment claim but the ledger shows each order paid once)
Rerun:     nandatown run examples/probiai/insured-repeat.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  insurer_received        Passed                [ev-38, ev-63]
                          note: ProbiAI kept all 2 insured payments
  double_payment_verdict  Failed                [ev-64]
                          note: false alarm: ProbiAI opened a double-payment claim but the ledger shows each order paid once
  ledger_conserved        Passed               money was conserved across every movement [ev-30, ev-36, ev-56, ev-61]
                          note: 10000 cents conserved across every movement
  privacy                 Passed               declared private fields never left the run [ev-68]
                          note: fields ['budget_cents'] never left the run

Events recorded: 68. Intents recorded: 24.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
