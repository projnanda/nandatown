NANDA Town System Fitness Report
========================================
Run:       sim-0aa17846cf12
Scenario:  referral_fabricated_citation_reputation_control (seed 42)
Faults:    none
Agents:    referral (referral_service), worker-cheap (research_worker), worker-honest (research_worker), buyer-1 (research_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.13.18
Duration:  1.7 logical seconds, deterministic
Verdict:   FAILED
First broken stage: fabrication_rejected (the buyer paid a worker whose citations are fabricated)
Rerun:     nandatown run integrations/referral/scenarios/referral_fabricated_citation_reputation_control.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  referral_connected               Passed                [ev-7]
  town_discovery                   Passed                [ev-20, ev-22]
  forecast_locked_before_delivery  Inconclusive
                                   note: every accepted Referral delivery needs a forecast locked before it, and every verdict must show that forecast
  fabrication_rejected             Failed                [ev-31]
                                   note: the buyer paid a worker whose citations are fabricated
  rematched_and_accepted           Inconclusive
                                   note: after the rejection the buyer must rematch with the fabricator excluded, and pay the worker whose delivery passes
  reputation_matches_checks        Failed                [ev-34]
                                   note: the trust layer moved a score without a matching Referral check, e.g. credit for a delivery whose citations were never verified
  credential_verified              Inconclusive
                                   note: Referral's signed credential for the hired worker must verify in Town against Referral's published key, and name that worker
  ledger_conserved                 Passed               money was conserved across every movement [ev-25, ev-31]
                                   note: 1000 cents conserved across every movement
  privacy                          Passed               declared private fields never left the run [ev-36]
                                   note: fields ['key'] never left the run

Events recorded: 36. Intents recorded: 14.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
