NANDA Town System Fitness Report
========================================
Run:       sim-a93af70c6b6f
Scenario:  mcl_cmms_down (seed 42)
Faults:    none
Agents:    layer (mcl_layer), radio (mcl_radio), cmms (mcl_cmms), maintenance (mcl_maintenance), planning (mcl_planning), supervisor (mcl_supervisor)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.13
Duration:  3.8 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/integrations/mine-context-layer/mcl_cmms_down.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  connection          Passed                [ev-11]
                      note: live MCP stdio sessions, one per participant role
  id_resolution       Passed                [ev-29]
  degraded_source     Passed                [ev-11, ev-53]
  action_matches_key  Passed                [ev-80, ev-92]
  human_decision      Passed                [ev-87, ev-92]
  ledger_conserved    Passed               money was conserved across every movement [ev-95]
                      note: complete run recorded no money moved
  privacy             Not tested           not exercised in this run
                      note: no redaction declared by this scenario

Events recorded: 95. Intents recorded: 34.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
