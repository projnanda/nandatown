NANDA Town System Fitness Report
========================================
Run:       sim-f9c371b18672
Scenario:  otd_lost_reply (seed 42)
Faults:    drop order_result
Agents:    desk (otd_desk), owner (otd_owner), pm (otd_pm)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.11.14
Duration:  12.7 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/integrations/open-trading-desk/otd_lost_reply.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  connection           Passed                [ev-8]
                       note: live MCP stdio session; validator otd-3
  discovery_and_views  Passed                [ev-22, ev-24, ev-25, ev-28, ev-30, ev-31, ev-33, ev-35]
  decision_and_fill    Passed                [ev-44, ev-45, ev-47, ev-48, ev-301, ev-303, ev-304]
  risk_refusals        Passed                [ev-58, ev-60, ev-61, ev-64, ev-66, ev-67, ev-288, ev-290]
  scoring_and_chain    Passed                [ev-301, ev-303, ev-304]
  pm_knowledge         Passed                [ev-50, ev-57, ev-306, ev-301, ev-303, ev-304]
  fault_and_retry      Passed                [ev-49, ev-51, ev-45, ev-47, ev-48, ev-52, ev-54, ev-55]
  ledger_conserved     Passed               money was conserved across every movement [ev-307]
                       note: complete run recorded no money moved
  privacy              Not tested           not exercised in this run
                       note: no redaction declared by this scenario

Events recorded: 307. Intents recorded: 116.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
