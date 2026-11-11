NANDA Town System Fitness Report
========================================
Run:       sim-71cefcb9d548
Scenario:  referral_unreachable_fail_closed (seed 42)
Faults:    none
Agents:    referral (referral_service), worker-cheap (research_worker), worker-honest (research_worker), buyer-1 (research_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.13.18
Duration:  1.5 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run integrations/referral/scenarios/referral_unreachable_fail_closed.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  outage_recorded       Passed                [ev-7]
  no_unchecked_payment  Passed                [ev-29]
  no_unchecked_credit   Passed                [ev-29]
  buyer_stopped         Passed                [ev-28]
  ledger_conserved      Passed               money was conserved across every movement
                        note: 1000 cents conserved across every movement
  privacy               Passed               declared private fields never left the run [ev-29]
                        note: fields ['key'] never left the run

Events recorded: 29. Intents recorded: 11.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
