NANDA Town System Fitness Report
========================================
Run:       sim-cb220c1265e2
Scenario:  cg_meetup_dropout (seed 42)
Faults:    drop cg_post_scores
Agents:    common-ground-room (cg_room_adapter), jack-agent (cg_person), javier-agent (cg_person), sergio-agent (cg_person)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.14
Duration:  11.2 logical seconds, deterministic
Verdict:   FAILED
First broken stage: posts_accepted (the room must accept windows and scores from all of ['jack', 'javier', 'sergio'])
Rerun:     nandatown run examples/integrations/common-ground/cg_meetup_dropout.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  room_interface          Passed                [ev-7]
  live_room               Passed                [ev-7]
                          note: calls answered by a running Common Ground room
  discovery               Passed               cards published and peers found through the index [ev-19]
  plan_created            Passed                [ev-22]
  all_joined              Passed                [ev-33, ev-36, ev-39]
  posts_accepted          Failed                [ev-48, ev-51, ev-54, ev-64, ev-67]
                          note: the room must accept windows and scores from all of ['jack', 'javier', 'sergio']
  same_plan_for_everyone  Inconclusive         
                          note: every person's agent must receive one and the same plan
  slot_in_every_window    Inconclusive         
                          note: no agreed plan to check against the windows
  fairness_consistent     Inconclusive         
                          note: no agreed plan to check against the scores
  fairest_option          Not tested           not exercised in this run
                          note: the trace has no opening hours, so Town cannot redo the room's search; Common Ground's verify.py does that check
  dropout_reported        Passed                [ev-62, ev-115, ev-118, ev-123]
                          note: the room kept the plan open with no deadline; the other agents stopped on their own limit and named the missing person
  ledger_conserved        Passed               money was conserved across every movement [ev-124]
                          note: complete run recorded no money moved
  privacy                 Passed               declared private fields never left the run [ev-124]
                          note: fields ['home', 'max_travel_min', 'travel_mode'] never left the run

Events recorded: 124. Intents recorded: 47.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
