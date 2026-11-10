NANDA Town System Fitness Report
========================================
Run:       sim-ea310737e72c
Scenario:  jang_mandate_violation (seed 42)
Faults:    none
Agents:    tv-seller (jang_seller), tv-buyer (jang_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.14.7
Duration:  2.9 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/jang/jang_mandate_violation.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  jang_reachable       Passed                [ev-5, ev-15]
                       note: JANG health, registration and mandate lodging answered for both roles
  violation_refused    Passed                [ev-22, ev-23]
                       note: JANG refused the out-of-mandate offer with 422 MANDATE_VIOLATION; the refusal recorded only the dimension, never the limit
  recovered_in_bounds  Passed                [ev-51, ev-52, ev-55, ev-56]
                       note: after the refusal the buyer re-decided from JANG's state and an in-bounds agreement was verified and settled
  mandate_not_shared   Passed                [ev-11, ev-13, ev-16, ev-20, ev-25, ev-28, ev-31, ev-34]
                       note: 16 inter-agent messages carried only protocol keys; no mandate field
  ledger_conserved     Passed               money was conserved across every movement [ev-56, ev-61]
                       note: 200000 cents conserved across every movement
  privacy              Passed               declared private fields never left the run [ev-66]
                       note: fields ['mandate'] never left the run

Events recorded: 66. Intents recorded: 22.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
