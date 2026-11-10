NANDA Town System Fitness Report
========================================
Run:       sim-4b1dcbb3d627
Scenario:  trajano_deal (seed 42)
Faults:    none
Agents:    market (trajano_market), buyer (trajano_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.14
Duration:  3.9 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/integrations/trajano/trajano_deal.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  connection        Passed                [ev-7]
                    note: 12 buyer tools at Trajano 26d78f6c1f02, 5 seller agents
  discovery         Passed               cards published and peers found through the index [ev-6, ev-9]
                    note: buyer found market under procurement.fasteners
  quotes            Passed                [ev-20, ev-23, ev-35, ev-38, ev-55]
                    note: 2 rfqs, a seller offer on each
  negotiation       Passed               offers alternated to an agreed price inside bounds [ev-45, ev-55]
                    note: one counter, answered with a final offer (round limit 1)
  order_placed      Passed                [ev-62, ev-64, ev-65]
                    note: order 1 placed under the signed mandate
  order_delivered   Passed                [ev-72, ev-76]
                    note: order 1 confirmed, shipped and delivered by the seller agent
  others_withdrawn  Passed                [ev-69]
                    note: threads [2] withdrawn in the database
  ground_truth      Passed                [ev-81]
                    note: one order in the database, total 76.00 vs list optimum 79.07
  settlement        Passed               money moved only through recorded escrow [ev-77]
                    note: 7600 cents paid once through Town's ledger
  ledger_conserved  Passed               money was conserved across every movement [ev-77]
                    note: 20000 cents conserved across every movement
  privacy           Passed               declared private fields never left the run [ev-82]
                    note: fields ['api_key', 'signature'] never left the run

Events recorded: 82. Intents recorded: 28.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
