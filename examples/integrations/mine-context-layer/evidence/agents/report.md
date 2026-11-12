NANDA Town System Fitness Report
========================================
Run:       sim-7d14b577cd4e
Scenario:  mcl_agents (seed 42)
Faults:    none
Agents:    layer (mcl_layer), radio (mcl_agent), cmms (mcl_agent), maintenance (mcl_agent), planning (mcl_agent), supervisor (mcl_supervisor)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.13
Duration:  2.6 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/integrations/mine-context-layer/mcl_agents.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  connection          Passed                [ev-11]
                      note: live MCP stdio sessions, one per participant role
  real_agents         Passed                [ev-29, ev-36, ev-43, ev-50, ev-62]
  id_resolution       Passed                [ev-62]
  action_matches_key  Passed                [ev-62]
  human_decision      Passed                [ev-57, ev-62]
  ledger_conserved    Passed               money was conserved across every movement [ev-65]
                      note: complete run recorded no money moved
  privacy             Not tested           not exercised in this run
                      note: no redaction declared by this scenario

Events recorded: 65. Intents recorded: 22.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
