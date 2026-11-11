NANDA Town System Fitness Report
========================================
Run:       sim-5c682e939ebd
Scenario:  referral_unlocked_forecast (seed 42)
Faults:    none
Agents:    referral (referral_service), worker-hasty (research_worker), buyer-1 (research_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.13.18
Duration:  1.7 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run integrations/referral/scenarios/referral_unlocked_forecast.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  referral_connected               Passed                [ev-6]
  unlocked_delivery_refused        Passed                [ev-24]
  forecast_locked_before_delivery  Passed                [ev-26, ev-29]
  work_accepted_after_lock         Passed                [ev-29]
  reputation_matches_checks        Passed                [ev-33, ev-35]
  ledger_conserved                 Passed               money was conserved across every movement [ev-21, ev-30]
                                   note: 1000 cents conserved across every movement
  privacy                          Passed               declared private fields never left the run [ev-36]
                                   note: fields ['key'] never left the run

Events recorded: 36. Intents recorded: 27.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
