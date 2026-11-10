NANDA Town System Fitness Report
========================================
Run:       sim-18beeb42305b
Scenario:  simops_crane (seed 42)
Faults:    none
Agents:    simops-desk (simops-desk), civil (simops-trade), concrete (simops-trade), steel (simops-trade), mechanical (simops-trade), electrical (simops-trade)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.11
Duration:  20.0 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/simops/simops_crane.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  referee_discovered   Passed                [ev-11, ev-27, ev-29, ev-31, ev-33, ev-35]
  briefs_private       Passed                [ev-63, ev-66, ev-69, ev-72, ev-75]
                       note: 5 briefs, each to its own trade; bodies redacted
  rules_enforced       Passed                [ev-110, ev-144]
                       note: 1 refused moves, each with a reason; 0 violations
  swap_signed_by_all   Passed                [ev-133, ev-139]
                       note: 2 commits, each signed by every party
  applied_once         Passed                [ev-133, ev-139]
                       note: 2 commits, 0 repeated deliveries, none applied twice
  forged_seat_refused  Not tested           not exercised in this run
                       note: no rogue in this run
  stale_calls_refused  Not tested           not exercised in this run
                       note: no write arrived after the week closed
  week_committed       Passed                [ev-143]
                       note: every task placed, board v3, 2 commits
  simops_verified      Passed                [ev-144]
                       note: replayed 24 calls, I1-I5 hold, log sha256:ddc68eb17efe…
  ledger_conserved     Passed               money was conserved across every movement [ev-145]
                       note: complete run recorded no money moved
  privacy              Passed               declared private fields never left the run [ev-145]
                       note: fields ['brief', 'seat_token'] never left the run

Events recorded: 145. Intents recorded: 55.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
