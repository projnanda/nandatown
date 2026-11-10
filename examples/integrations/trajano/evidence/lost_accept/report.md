NANDA Town System Fitness Report
========================================
Run:       sim-c5fff40605f6
Scenario:  trajano_lost_accept (seed 42)
Faults:    drop accept_offer_result
Agents:    market (trajano_market), buyer (trajano_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.14
Duration:  5.9 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/integrations/trajano/trajano_lost_accept.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  connection        Passed                [ev-7]
                    note: 12 buyer tools at Trajano 26d78f6c1f02, 5 seller agents
  reply_lost        Passed                [ev-64, ev-59, ev-65]
                    note: Town dropped the first accept reply; the buyer did not know whether it had bought
  retried_same_key  Passed                [ev-62, ev-68]
                    note: two accept calls, both with key accept-3
  replayed          Passed                [ev-62, ev-68, ev-71]
                    note: the retry returned the original result, order 1, unchanged
  order_delivered   Passed                [ev-78, ev-82]
                    note: order 1 confirmed, shipped and delivered by the seller agent
  ground_truth      Passed                [ev-87]
                    note: one order in the database, total 76.00 vs list optimum 79.07
  settlement        Passed               money moved only through recorded escrow [ev-83]
                    note: 7600 cents paid once through Town's ledger
  ledger_conserved  Passed               money was conserved across every movement [ev-83]
                    note: 20000 cents conserved across every movement
  privacy           Passed               declared private fields never left the run [ev-88]
                    note: fields ['api_key', 'signature'] never left the run

Events recorded: 88. Intents recorded: 30.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
