NANDA Town System Fitness Report
========================================
Run:       sim-a1ffe8bf8c70
Scenario:  signed_answers_duplicate (seed 42)
Faults:    duplicate source_request
Agents:    outlet (sa_outlet), reader-subscriber (sa_reader)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.13
Duration:  1.3 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/signed_answers/scenarios/signed_answers_duplicate.yaml --seed 42 --plugin examples/signed_answers/signed_answers_town.py

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  outlet_listed                                       Passed                [ev-7]
  outlet_discovered                                   Passed                [ev-9]
  package_verified[reader-subscriber]                 Passed                [ev-20, ev-22]
  sentences_match_published_story[reader-subscriber]  Passed                [ev-20, ev-22]
  one_package_per_request[reader-subscriber]          Passed                [ev-12, ev-20, ev-22]
                                                      note: the redelivered request got the same package
  ledger_conserved                                    Passed               money was conserved across every movement [ev-23]
                                                      note: complete run recorded no money moved
  privacy                                             Passed               declared private fields never left the run [ev-23]
                                                      note: fields ['envelope', 'question'] never left the run

Events recorded: 23. Intents recorded: 5.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
