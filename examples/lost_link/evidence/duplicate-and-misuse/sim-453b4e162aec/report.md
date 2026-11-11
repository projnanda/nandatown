NANDA Town System Fitness Report
========================================
Run:       sim-453b4e162aec
Scenario:  lost-link-duplicate-and-misuse (seed 42)
Faults:    duplicate lostlink_found_available
Agents:    finder (lostlink.finder), owner (lostlink.owner), operator (lostlink.human_fixture)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.14
Duration:  0.9 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run /Users/zhangkemeng/Documents/Codex/2026-11-04/https-github-com-cathyliu0510-lost-link/work/hw8/nandatown/examples/lost_link/duplicate-and-misuse.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  town_discovery                    Passed                [ev-18, ev-29]
  reports_registered                Passed                [ev-8, ev-19, ev-30]
  target_retrieved                  Passed                [ev-20, ev-31]
  search_private_detail_withheld    Passed                [ev-21, ev-32]
  claim_links_reports               Passed                [ev-22, ev-33]
  pickup_withheld_before_approval   Passed                [ev-23]
  operator_receives_private_claim   Passed                [ev-37]
  clarification_round_trip          Passed                [ev-41, ev-42]
  human_approval_releases_pickup    Passed                [ev-46, ev-49]
  return_confirmed                  Passed                [ev-52, ev-55]
  agent_cannot_approve              Passed                [ev-24]
  forged_approval_flag_refused      Passed                [ev-25]
  misuse_keeps_case_pending         Passed                [ev-26]
  duplicate_delivery_is_idempotent  Passed                [ev-34]
  ledger_conserved                  Passed               money was conserved across every movement [ev-56]
                                    note: complete run recorded no money moved
  privacy                           Passed               declared private fields never left the run [ev-56]
                                    note: fields ['Authorization', 'X-Human-Token', 'private_detail', 'your_private_detail'] never left the run

Events recorded: 56. Intents recorded: 15.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
