NANDA Town System Fitness Report
========================================
Run:       sim-413128730cf9
Scenario:  referral_fabricated_citation (seed 42)
Faults:    none
Agents:    referral (referral_service), worker-cheap (research_worker), worker-honest (research_worker), buyer-1 (research_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.13.18
Duration:  1.9 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run integrations/referral/scenarios/referral_fabricated_citation.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  referral_connected               Passed                [ev-7]
  town_discovery                   Passed                [ev-23, ev-25]
  forecast_locked_before_delivery  Passed                [ev-34, ev-47, ev-37, ev-50]
  fabrication_rejected             Passed                [ev-37, ev-38]
  rematched_and_accepted           Passed                [ev-41, ev-50, ev-51]
  reputation_matches_checks        Passed                [ev-40, ev-54, ev-56]
  credential_verified              Passed                [ev-55]
  ledger_conserved                 Passed               money was conserved across every movement [ev-30, ev-38, ev-43, ev-51]
                                   note: 1000 cents conserved across every movement
  privacy                          Passed               declared private fields never left the run [ev-57]
                                   note: fields ['key'] never left the run

Events recorded: 57. Intents recorded: 41.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
