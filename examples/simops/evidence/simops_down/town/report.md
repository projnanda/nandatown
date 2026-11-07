NANDA Town System Fitness Report
========================================
Run:       sim-331547d23018
Scenario:  simops_week9 (seed 42)
Faults:    none
Agents:    simops-desk (simops-desk), civil (simops-trade), concrete (simops-trade), steel (simops-trade), mechanical (simops-trade), electrical (simops-trade)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.11
Duration:  20.0 logical seconds, deterministic
Verdict:   INCOMPLETE
First broken stage: briefs_private (SimOps never answered, so no SimOps behaviour was observed)
Rerun:     nandatown run examples/simops/simops_week9.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  referee_discovered   Passed                [ev-11, ev-27, ev-29, ev-31, ev-33, ev-35]
  briefs_private       Inconclusive
                       note: SimOps never answered, so no SimOps behaviour was observed
  rules_enforced       Inconclusive
                       note: SimOps never answered, so no SimOps behaviour was observed
  swap_signed_by_all   Inconclusive
                       note: SimOps never answered, so no SimOps behaviour was observed
  applied_once         Inconclusive
                       note: SimOps never answered, so no SimOps behaviour was observed
  forged_seat_refused  Inconclusive
                       note: SimOps never answered, so no SimOps behaviour was observed
  stale_calls_refused  Inconclusive
                       note: SimOps never answered, so no SimOps behaviour was observed
  week_committed       Inconclusive
                       note: SimOps never answered, so no SimOps behaviour was observed
  simops_verified      Inconclusive
                       note: SimOps never answered, so no SimOps behaviour was observed
  ledger_conserved     Passed               money was conserved across every movement [ev-62]
                       note: complete run recorded no money moved
  privacy              Passed               declared private fields never left the run [ev-62]
                       note: fields ['brief', 'seat_token'] never left the run

Events recorded: 62. Intents recorded: 21.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
