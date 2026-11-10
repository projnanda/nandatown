NANDA Town System Fitness Report
========================================
Run:       sim-8c7ca6564d71
Scenario:  termsmet_lost_reply (seed 42)
Faults:    drop tm_repair_result
Agents:    tm_adapter (termsmet_adapter), tm_buyer (termsmet_buyer), tm_provider (termsmet_provider)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.11.5
Duration:  12.5 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/integrations/termsmet/termsmet_lost_reply.yaml --seed 42

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  agreement_funding    Passed                [ev-12, ev-32, ev-49, ev-37, ev-54, ev-67, ev-59]
                       note: job job-0a262be2bebd437b83c9b1fd5cae5f42: v2 (repair window 10) accepted by both; simulated funding
  delivery_repair      Passed                [ev-77, ev-82, ev-84, ev-88]
                       note: submission 1 FAIL (MISSING_ORDER A102) -> report read -> submission 2 PASS
  release_gating       Passed                [ev-18, ev-113, ev-209, ev-215]
                       note: release at/after review deadline; 19 real-time polls; release 2.04s after PASS; no refund
  retrieved_artifact   Passed                [ev-215, ev-209, ev-221]
                       note: buyer received 3 orders / 6 units / 3560 cents, equal to the independent fixture
  lost_reply_recovery  Passed                [ev-88, ev-90, ev-91, ev-94, ev-100, ev-105, ev-108, ev-109]
                       note: PASS dropped in transit; provider unconfirmed -> 1 retry rejected INVALID_STATE -> status IN_REVIEW + report -> confirmed
  ledger_conserved     Passed               money was conserved across every movement [ev-228]
                       note: complete run recorded no money moved
  privacy              Not tested           not exercised in this run
                       note: no redaction declared by this scenario

Events recorded: 228. Intents recorded: 83.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
