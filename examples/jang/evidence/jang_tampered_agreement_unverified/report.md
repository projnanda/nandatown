NANDA Town System Fitness Report
========================================
Run:       sim-9561ba9ff4e7
Scenario:  jang_tampered_agreement_unverified (seed 42)
Faults:    none
Agents:    tv-seller (jang_seller), tv-buyer (jang_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.14.7
Duration:  2.9 logical seconds, deterministic
Verdict:   FAILED
First broken stage: tamper_detected (the buyer accepted an agreement whose terms were altered after JANG signed it)
Rerun:     nandatown run examples/jang/jang_tampered_agreement_unverified.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  jang_reachable        Passed                [ev-5, ev-15]
                        note: JANG health, registration and mandate lodging answered for both roles
  negotiated            Passed                [ev-19, ev-22, ev-25, ev-28, ev-31, ev-34, ev-37, ev-40]
                        note: 10 alternating offers closed in one agreement on all 4 terms
  tamper_detected       Failed                [ev-51]
                        note: the buyer accepted an agreement whose terms were altered after JANG signed it
  no_payment_on_tamper  Failed                [ev-51, ev-56]
                        note: money moved or goods shipped against a tampered agreement
  mandate_not_shared    Passed                [ev-11, ev-13, ev-16, ev-20, ev-23, ev-26, ev-29, ev-32]
                        note: 16 inter-agent messages carried only protocol keys; no mandate field
  ledger_conserved      Passed               money was conserved across every movement [ev-56, ev-61]
                        note: 200000 cents conserved across every movement
  privacy               Passed               declared private fields never left the run [ev-63]
                        note: fields ['mandate'] never left the run

Events recorded: 63. Intents recorded: 21.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
