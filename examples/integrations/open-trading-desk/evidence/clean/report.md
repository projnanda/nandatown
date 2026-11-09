NANDA Town System Fitness Report
========================================
Run:       sim-c2d8b5dab5c8
Scenario:  otd_workflow (seed 42)
Faults:    none
Agents:    desk (otd_desk), owner (otd_owner), pm (otd_pm)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.11.14
Duration:  11.7 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/integrations/open-trading-desk/otd_workflow.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  connection           Passed                [ev-8]
                       note: live MCP stdio session; validator otd-3
  discovery_and_views  Passed                [ev-22, ev-24, ev-25, ev-28, ev-30, ev-31, ev-33, ev-35]
  decision_and_fill    Passed                [ev-44, ev-45, ev-47, ev-48, ev-294, ev-296, ev-297]
  risk_refusals        Passed                [ev-51, ev-53, ev-54, ev-57, ev-59, ev-60, ev-281, ev-283]
  scoring_and_chain    Passed                [ev-294, ev-296, ev-297]
  pm_knowledge         Passed                [ev-50, ev-299, ev-294, ev-296, ev-297]
  ledger_conserved     Passed               money was conserved across every movement [ev-300]
                       note: complete run recorded no money moved
  privacy              Not tested           not exercised in this run
                       note: no redaction declared by this scenario

Events recorded: 300. Intents recorded: 114.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
