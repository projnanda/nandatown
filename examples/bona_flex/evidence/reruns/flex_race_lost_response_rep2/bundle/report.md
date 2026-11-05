NANDA Town System Fitness Report
========================================
Run:       bona-flex-flex_race_lost_response-s7
Scenario:  flex_race_lost_response (seed 7)
Faults:    drop flex_hold_result, duplicate flex_hold
Agents:    desk (flex_desk), seller-1 (flex_seller), seller-2 (flex_seller), buyer-1 (flex_buyer), buyer-2 (flex_buyer), meter (flex_meter)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.13.15
Duration:  20.3 logical seconds, deterministic
Verdict:   PASSED
Rerun:     python examples/bona_flex/run_check.py --cases flex_race_lost_response

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  no_double_sell                 Passed                [ev-50, ev-54, ev-93]
                                 note: held + confirmed W never exceeded the assessed capacity
  acceptance_order_respected     Passed                [ev-50, ev-54]
                                 note: first commit won; grants [['hold', 400000, False], ['hold', 100000, True]]
  retry_replays_original         Passed                [ev-50, ev-69, ev-54, ev-57]
                                 note: every retry returned the same seq and a byte-identical signed receipt
  retry_sends_identical_request  Passed                [ev-50, ev-69, ev-54, ev-57]
                                 note: every retry sent the identical request bytes (created_at included)
  duplicate_no_extra_commitment  Passed                [ev-50, ev-54, ev-57, ev-69, ev-48]
                                 note: exactly one hold per (buyer, idempotency key); 1 duplicated delivery(ies) seen
  settled_once                   Passed                [ev-83, ev-84, ev-86, ev-87]
                                 note: one delivery receipt per confirmed commitment; repeated settles returned it unchanged
  trust_accounted_once           Passed                [ev-11, ev-89, ev-90, ev-12, ev-91, ev-92]
                                 note: trust moved by exactly the settled commitments; repeats changed nothing
  receipts_verify                Passed                [ev-50, ev-54, ev-57, ev-64, ev-69, ev-75, ev-83, ev-84]
                                 note: every commitment and delivery carries a Bona receipt the desk verified (re-checked offline)
  money_follows_verdict          Passed                [ev-60, ev-72, ev-97, ev-100]
                                 note: escrow released only for credited verdicts, refunded otherwise
  capacity_not_disclosed         Passed                [ev-40, ev-43, ev-51, ev-55, ev-58, ev-65, ev-70, ev-76]
                                 note: no desk reply disclosed capacity
  clock_valid                    Passed                [ev-94]
                                 note: all six clock-validity checks held
  sidecar_bound                  Passed                [ev-95, ev-102]
                                 note: sidecar manifest sha256 bound in the trace (files re-checked offline)
  ledger_conserved               Passed               money was conserved across every movement [ev-60, ev-72, ev-97, ev-100]
                                 note: 200000 cents conserved across every movement
  privacy                        Passed               declared private fields never left the run [ev-102]
                                 note: fields ['budget_cents'] never left the run

Events recorded: 102. Intents recorded: 35.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
