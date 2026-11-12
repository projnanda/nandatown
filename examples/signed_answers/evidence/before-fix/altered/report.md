NANDA Town System Fitness Report
========================================
Run:       sim-49b406c565f0
Scenario:  signed_answers_altered (seed 42)
Faults:    none
Agents:    outlet (sa_outlet), reader-subscriber (sa_reader)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.13
Duration:  1.2 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/signed_answers/scenarios/signed_answers_altered.yaml --seed 42 --plugin examples/signed_answers/signed_answers_town.py

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  outlet_listed                                Passed                [ev-7]
  outlet_discovered                            Passed                [ev-9]
  altered_package_rejected[reader-subscriber]  Passed                [ev-13, ev-17]
  ledger_conserved                             Passed               money was conserved across every movement [ev-18]
                                               note: complete run recorded no money moved
  privacy                                      Passed               declared private fields never left the run [ev-18]
                                               note: fields ['envelope', 'question'] never left the run

Events recorded: 18. Intents recorded: 4.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
