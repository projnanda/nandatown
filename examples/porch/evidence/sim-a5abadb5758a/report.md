NANDA Town System Fitness Report
========================================
Run:       sim-a5abadb5758a
Scenario:  porch_home (seed 42)
Faults:    duplicate home_action_request
Agents:    owner (porch_home_owner), porch (porch_gateway), auditor (porch_hub_auditor), energy-agent (porch_home_agent), concierge-agent (porch_home_agent), stranger (porch_home_agent), impersonator (porch_impersonator)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.3
Duration:  40.0 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/porch/porch_home.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  gateway_reachable       Passed                [ev-28, ev-83, ev-34, ev-47, ev-58, ev-69, ev-88, ev-99]
                          note: Porch answered 13 MCP calls
  discovery               Passed               cards published and peers found through the index [ev-15, ev-24, ev-79, ev-147]
                          note: 3 agents found the gateway's verified card
  home_described          Passed                [ev-28, ev-83]
                          note: describe_home matched the expected home
  decisions_match_policy  Passed                [ev-34, ev-47, ev-58, ev-69, ev-88, ev-99, ev-115, ev-126]
                          note: 9 of 9 steps got the expected decision; every allowed step was executed and verified by Porch, no other step was executed
  hub_state_confirmed     Passed                [ev-42, ev-53, ev-64, ev-75, ev-94, ev-105, ev-121, ev-132]
                          note: 11 of 11 hub reads matched the expected state
  owner_approval          Passed                [ev-99, ev-110, ev-115]
                          note: the request was held, the owner approved it through Porch, and the retry with the approval id was executed and verified
  unknown_agent_rejected  Passed                [ev-151, ev-156]
                          note: Porch refused all 2 calls from an agent with no Porch key
  impersonation_blocked   Passed                [ev-167, ev-171, ev-174]
                          note: the request signed with the wrong key failed Town's signature check, never reached Porch, and the device did not change
  duplicate_applied_once  Passed                [ev-32, ev-37, ev-177]
                          note: 1 request was delivered twice by Town and Porch's audit log holds exactly one entry for it
  audit_complete          Passed                [ev-177, ev-34, ev-47, ev-58, ev-69, ev-88, ev-99, ev-115]
                          note: 9 Porch audit entries match 9 adapter decisions by audit id, agent, request and decision
  ledger_conserved        Passed               money was conserved across every movement [ev-178]
                          note: complete run recorded no money moved
  privacy                 Not tested           not exercised in this run
                          note: no redaction declared by this scenario

Events recorded: 178. Intents recorded: 65.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
