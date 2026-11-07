NANDA Town System Fitness Report
========================================
Run:       sim-b96897f38761
Scenario:  simops_week9_forged (seed 42)
Faults:    none
Agents:    simops-desk (simops-desk), civil (simops-trade), concrete (simops-trade), steel (simops-trade), mechanical (simops-trade), electrical (simops-trade), rogue (simops-rogue)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.11
Duration:  20.0 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/simops/simops_week9_forged.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  referee_discovered   Passed                [ev-12, ev-32, ev-34, ev-36, ev-38, ev-40]
  briefs_private       Passed                [ev-68, ev-71, ev-74, ev-77, ev-80]
                       note: 5 briefs, each to its own trade; bodies redacted
  rules_enforced       Passed                [ev-114, ev-202]
                       note: 1 refused moves, each with a reason; 0 violations
  swap_signed_by_all   Passed                [ev-170]
                       note: 1 commits, each signed by every party
  applied_once         Passed                [ev-170]
                       note: 1 commits, 0 repeated deliveries, none applied twice
  forged_seat_refused  Passed                [ev-189, ev-196, ev-199]
                       note: 3 forged calls: 3 stopped by Town auth, 0 reached SimOps and were refused
  stale_calls_refused  Not tested           not exercised in this run
                       note: no write arrived after the week closed
  week_committed       Passed                [ev-201]
                       note: every task placed, board v2, 1 commits
  simops_verified      Passed                [ev-202]
                       note: replayed 30 calls, I1-I5 hold, log sha256:d54f73994b73…
  ledger_conserved     Passed               money was conserved across every movement [ev-203]
                       note: complete run recorded no money moved
  privacy              Passed               declared private fields never left the run [ev-203]
                       note: fields ['brief', 'seat_token'] never left the run

Events recorded: 203. Intents recorded: 76.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
