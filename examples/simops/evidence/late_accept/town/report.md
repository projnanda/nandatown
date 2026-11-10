NANDA Town System Fitness Report
========================================
Run:       sim-5a6de1de5e30
Scenario:  simops_week9_late (seed 42)
Faults:    delay swap_offer
Agents:    simops-desk (simops-desk), civil (simops-trade), concrete (simops-trade), steel (simops-trade), mechanical (simops-trade), electrical (simops-trade)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.11
Duration:  20.0 logical seconds, deterministic
Verdict:   FAILED
First broken stage: week_committed (unscheduled at the end: ['ELE-roughin'])
Rerun:     nandatown run examples/simops/simops_week9_late.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  referee_discovered   Passed                [ev-11, ev-27, ev-29, ev-31, ev-33, ev-35]
  briefs_private       Passed                [ev-63, ev-66, ev-69, ev-72, ev-75]
                       note: 5 briefs, each to its own trade; bodies redacted
  rules_enforced       Passed                [ev-109, ev-128, ev-138]
                       note: 2 refused moves, each with a reason; 0 violations
  swap_signed_by_all   Not tested           not exercised in this run
                       note: nothing was committed in this run
  applied_once         Not tested           not exercised in this run
                       note: nothing was committed and no call was delivered twice
  forged_seat_refused  Not tested           not exercised in this run
                       note: no rogue in this run
  stale_calls_refused  Passed                [ev-128]
                       note: 1 late writes, all refused
  week_committed       Failed                [ev-137]
                       note: unscheduled at the end: ['ELE-roughin']
  simops_verified      Passed                [ev-138]
                       note: replayed 22 calls, I1-I5 hold, log sha256:cc3bec8fccb5…
  ledger_conserved     Passed               money was conserved across every movement [ev-139]
                       note: complete run recorded no money moved
  privacy              Passed               declared private fields never left the run [ev-139]
                       note: fields ['brief', 'seat_token'] never left the run

Events recorded: 139. Intents recorded: 52.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
