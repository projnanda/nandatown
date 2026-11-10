NANDA Town System Fitness Report
========================================
Run:       sim-9b41efeb1988
Scenario:  jang_market (seed 42)
Faults:    duplicate jang_turn, duplicate delivery
Agents:    tv-seller (jang_seller), tv-buyer (jang_buyer)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.14.7
Duration:  3.0 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/jang/jang_market.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  jang_reachable           Passed                [ev-5, ev-15]
                           note: JANG health, registration and mandate lodging answered for both roles
  discovered               Passed                [ev-7, ev-10, ev-13]
                           note: the buyer found the seller through the Town registry and received a quote
  mandates_lodged          Passed                [ev-5, ev-15]
                           note: JANG accepted both owner-signed mandates (signer: python)
  negotiated               Passed                [ev-19, ev-22, ev-26, ev-31, ev-34, ev-37, ev-40, ev-43]
                           note: 10 alternating offers closed in one agreement on all 4 terms
  agreement_verified       Passed                [ev-53, ev-56]
                           note: buyer and seller each verified JANG's signature with the trusted key and found the terms inside their own mandate
  duplicate_turn_absorbed  Passed                [ev-24, ev-29]
                           note: the duplicated turn signal was recognised from JANG's session state; no extra offer
  settled_once             Passed                [ev-57, ev-63, ev-69]
                           note: escrow of 120000 cents (agreed price x 1) released exactly once; the duplicated delivery was recognised
  reputation               Passed               receipts drove the public score [ev-66, ev-65]
                           note: recorded receipt claims and reference score arithmetic agree; claim truth and reporter authority are not tested
  mandate_not_shared       Passed                [ev-11, ev-13, ev-16, ev-20, ev-23, ev-27, ev-32, ev-35]
                           note: 16 inter-agent messages carried only protocol keys; no mandate field
  ledger_conserved         Passed               money was conserved across every movement [ev-57, ev-63]
                           note: 200000 cents conserved across every movement
  privacy                  Passed               declared private fields never left the run [ev-70]
                           note: fields ['mandate'] never left the run

Events recorded: 70. Intents recorded: 22.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
