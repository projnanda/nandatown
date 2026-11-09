NANDA Town System Fitness Report
========================================
Run:       sim-04a400c12eee
Scenario:  mcl_workflow (seed 42)
Faults:    none
Agents:    layer (mcl_layer), radio (mcl_radio), cmms (mcl_cmms), maintenance (mcl_maintenance), planning (mcl_planning), supervisor (mcl_supervisor), visitor (mcl_intruder)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.13
Duration:  4.2 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/integrations/mine-context-layer/mcl_workflow.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  connection           Passed                [ev-12]
                       note: live MCP stdio sessions, one per participant role
  id_resolution        Passed                [ev-38]
  provenance           Passed                [ev-50, ev-72]
  permission_boundary  Passed                [ev-67, ev-33, ev-111]
  action_matches_key   Passed                [ev-99, ev-111]
  human_decision       Passed                [ev-106, ev-111]
  ledger_conserved     Passed               money was conserved across every movement [ev-114]
                       note: complete run recorded no money moved
  privacy              Not tested           not exercised in this run
                       note: no redaction declared by this scenario

Events recorded: 114. Intents recorded: 41.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
