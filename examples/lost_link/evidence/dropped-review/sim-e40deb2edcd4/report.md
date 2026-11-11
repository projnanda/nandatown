NANDA Town System Fitness Report
========================================
Run:       sim-e40deb2edcd4
Scenario:  lost-link-dropped-review (seed 42)
Faults:    drop lostlink_review_requested
Agents:    finder (lostlink.finder), owner (lostlink.owner), operator (lostlink.human_fixture)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.14
Duration:  0.3 logical seconds, deterministic
Verdict:   INCOMPLETE
First broken stage: operator_receives_private_claim (Required observation is missing)
Rerun:     nandatown run /Users/zhangkemeng/Documents/Codex/2026-11-04/https-github-com-cathyliu0510-lost-link/work/hw8/nandatown/examples/lost_link/dropped-review.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  town_discovery                   Passed                [ev-17]
  reports_registered               Passed                [ev-8, ev-18]
  target_retrieved                 Passed                [ev-19]
  search_private_detail_withheld   Passed                [ev-20]
  claim_links_reports              Passed                [ev-21]
  pickup_withheld_before_approval  Passed                [ev-22]
  operator_receives_private_claim  Inconclusive
                                   note: Required observation is missing
  clarification_round_trip         Inconclusive
                                   note: Required observation is missing
  human_approval_releases_pickup   Inconclusive
                                   note: Required observation is missing
  return_confirmed                 Inconclusive
                                   note: Required observation is missing
  ledger_conserved                 Passed               money was conserved across every movement [ev-25]
                                   note: complete run recorded no money moved
  privacy                          Passed               declared private fields never left the run [ev-25]
                                   note: fields ['Authorization', 'X-Human-Token', 'private_detail', 'your_private_detail'] never left the run

Events recorded: 25. Intents recorded: 8.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
