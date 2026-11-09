NANDA Town System Fitness Report
========================================
Run:       sim-37aef89f9a7a
Scenario:  tastetwin_shared_taste (seed 7)
Faults:    duplicate choice_report
Agents:    tastetwin (tt_adapter), cafe-1369 (tt_cafe), cafe-tatte (tt_cafe), cafe-diesel (tt_cafe), cafe-bloc11 (tt_cafe), cafe-darwins (tt_cafe), cafe-flour (tt_cafe), pang (tt_user), planner-a (tt_planner), planner-b (tt_planner)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.13.18
Duration:  10.4 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/tastetwin/tastetwin_shared_taste.yaml --seed 7

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  live_service         Passed                [ev-13, ev-44, ev-50, ev-66, ev-69, ev-87, ev-93]
                       note: every MCP call to cafe-taste-twin 0.1.0 succeeded
  discovery            Passed               cards published and peers found through the index [ev-15, ev-40, ev-38, ev-83, ev-81]
                       note: 2 planners found the verified taste.rank card and all 6 cafe listings
  ranking_contract     Passed                [ev-53, ev-96]
                       note: 2 rankings: 1-3 results from the sent candidates, valid request_id, descending scores, close_call consistent with the top-two gap
  choice_binding       Passed                [ev-53, ev-69]
                       note: record_choice used the ranking's request_id and a cafe that was offered
  choice_applied_once  Passed                [ev-64, ev-72, ev-69, ev-47, ev-90]
                       note: the choice message was delivered twice; choice count moved by 1 (1 record_choice call(s) succeeded)
  shared_memory        Passed                [ev-47, ev-90, ev-80]
                       note: planner-b got no message from the reporting planner and an empty private memory, yet saw choice count 0 -> 1 and the stated preference ['Tatte']
  ledger_conserved     Passed               money was conserved across every movement [ev-99]
                       note: complete run recorded no money moved
  privacy              Passed               declared private fields never left the run [ev-99]
                       note: fields ['feedback', 'said', 'says', 'weights'] never left the run

Events recorded: 99. Intents recorded: 26.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
