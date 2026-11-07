NANDA Town System Fitness Report
========================================
Run:       sim-f09ae0933297
Scenario:  simops_week9_forged_weak_auth (seed 42)
Faults:    none
Agents:    simops-desk (simops-desk), civil (simops-trade), concrete (simops-trade), steel (simops-trade), mechanical (simops-trade), electrical (simops-trade), rogue (simops-rogue)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.11
Duration:  20.0 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/simops/simops_week9_forged_weak_auth.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  referee_discovered   Passed                [ev-12, ev-31, ev-33, ev-35, ev-37, ev-39]
  briefs_private       Passed                [ev-67, ev-70, ev-73, ev-76, ev-79]
                       note: 5 briefs, each to its own trade; bodies redacted
  rules_enforced       Passed                [ev-113, ev-187, ev-196, ev-199, ev-207]
                       note: 4 refused moves, each with a reason; 0 violations
  swap_signed_by_all   Passed                [ev-169]
                       note: 1 commits, each signed by every party
  applied_once         Passed                [ev-169]
                       note: 1 commits, 0 repeated deliveries, none applied twice
  forged_seat_refused  Passed                [ev-187, ev-196, ev-199]
                       note: 3 forged calls: 0 stopped by Town auth, 3 reached SimOps and were refused
  stale_calls_refused  Not tested           not exercised in this run
                       note: no write arrived after the week closed
  week_committed       Passed                [ev-206]
                       note: every task placed, board v2, 1 commits
  simops_verified      Passed                [ev-207]
                       note: replayed 30 calls, I1-I5 hold, log sha256:d54f73994b73…
  ledger_conserved     Passed               money was conserved across every movement [ev-208]
                       note: complete run recorded no money moved
  privacy              Passed               declared private fields never left the run [ev-208]
                       note: fields ['brief', 'seat_token'] never left the run

Events recorded: 208. Intents recorded: 79.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
