NANDA Town System Fitness Report
========================================
Run:       sim-48a93823512b
Scenario:  trajano_over_mandate (seed 42)
Faults:    none
Agents:    market (trajano_market), buyer (trajano_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.14
Duration:  3.9 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/integrations/trajano/trajano_over_mandate.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  connection        Passed                [ev-7]
                    note: 12 buyer tools at Trajano 26d78f6c1f02, 5 seller agents
  quotes            Passed                [ev-20, ev-23, ev-35, ev-38, ev-55, ev-71]
                    note: 2 rfqs, a seller offer on each
  careless_accept   Passed                [ev-16, ev-58]
                    note: every offer was above the 0.1000 unit cap; the buyer accepted anyway
  mandate_refused   Passed                [ev-62]
                    note: MANDATE_INVALID, not retryable, and not retried
  refusal_recorded  Passed                [ev-65, ev-71]
                    note: the server wrote a refused message on the thread and the buyer recorded it
  no_order          Passed                [ev-80]
                    note: no order in the database; every thread withdrawn
  no_payment        Passed                [ev-85]
                    note: no money moved
  ledger_conserved  Passed               money was conserved across every movement
                    note: 20000 cents conserved across every movement
  privacy           Passed               declared private fields never left the run [ev-85]
                    note: fields ['api_key', 'signature'] never left the run

Events recorded: 85. Intents recorded: 29.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
