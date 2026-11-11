NANDA Town System Fitness Report
========================================
Run:       sim-d40b24873700
Scenario:  cg_meetup_duplicate (seed 42)
Faults:    duplicate cg_post_scores
Agents:    common-ground-room (cg_room_adapter), jack-agent (cg_person), javier-agent (cg_person), sergio-agent (cg_person)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.14
Duration:  6.4 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/integrations/common-ground/cg_meetup_duplicate.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  room_interface          Passed                [ev-7]
  live_room               Passed                [ev-7]
                          note: calls answered by a running Common Ground room
  live_agents             Passed                [ev-42, ev-45, ev-48]
                          note: each person's windows, tags and scores came from a Claude Code agent using only that person's private MCP server
  discovery               Passed               cards published and peers found through the index [ev-19]
  plan_created            Passed                [ev-22]
  all_joined              Passed                [ev-33, ev-36, ev-39]
  posts_accepted          Passed                [ev-51, ev-54, ev-57, ev-67, ev-70, ev-73]
                          note: accepted from every agent
  same_plan_for_everyone  Passed                [ev-97, ev-100, ev-103]
  slot_in_every_window    Passed                [ev-97, ev-51, ev-54, ev-57]
  fairness_consistent     Passed                [ev-97, ev-67, ev-70, ev-73]
                          note: nobody vetoed the place; each agent's total and the lowest score match the posted scores
  fairest_option          Not tested           not exercised in this run
                          note: the trace has no opening hours, so Town cannot redo the room's search; Common Ground's verify.py does that check
  duplicate_contained     Passed                [ev-65, ev-78, ev-88]
                          note: the room answered the repeated call with an error; the agent ignored the stale answer and kept one plan
  ledger_conserved        Passed               money was conserved across every movement [ev-104]
                          note: complete run recorded no money moved
  privacy                 Passed               declared private fields never left the run [ev-104]
                          note: fields ['home_area', 'notes_from_owner', 'preferences_in_their_words', 'title'] never left the run

Events recorded: 104. Intents recorded: 35.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
