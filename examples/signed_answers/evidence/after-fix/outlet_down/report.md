NANDA Town System Fitness Report
========================================
Run:       sim-578cf676b94a
Scenario:  signed_answers_clean (seed 42)
Faults:    none
Agents:    outlet (sa_outlet), reader-subscriber (sa_reader), reader-stranger (sa_reader)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.13
Duration:  1.0 logical seconds, deterministic
Verdict:   FAILED
First broken stage: outlet_listed (the outlet's card must be in the Town registry, verified)
Rerun:     nandatown run examples/signed_answers/scenarios/signed_answers_clean.yaml --seed 42 --plugin examples/signed_answers/signed_answers_town.py

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  outlet_listed                                       Failed                [ev-6]
                                                      note: the outlet's card must be in the Town registry, verified
  outlet_discovered                                   Failed                [ev-9, ev-10]
                                                      note: every reader must find the outlet through the registry
  package_verified[reader-subscriber]                 Inconclusive
                                                      note: every answer to a subscriber's reader must be a completed task with a matching package of at least one sentence, whose id and signature check out under the key on the outlet's Town card
  sentences_match_published_story[reader-subscriber]  Inconclusive
                                                      note: the published story could not be compared
  no_package_without_credential[reader-stranger]      Inconclusive
                                                      note: a reader with no subscriber credential must get a completed answer and no package
  ledger_conserved                                    Passed               money was conserved across every movement [ev-11]
                                                      note: complete run recorded no money moved
  privacy                                             Passed               declared private fields never left the run [ev-11]
                                                      note: fields ['envelope', 'question'] never left the run

Events recorded: 11. Intents recorded: 2.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
