NANDA Town System Fitness Report
========================================
Run:       bona-flex-flex_clean-s7
Scenario:  flex_clean (seed 7)
Faults:    none
Agents:    desk (flex_desk), seller-1 (flex_seller), seller-2 (flex_seller), buyer-1 (flex_buyer), buyer-2 (flex_buyer), meter (flex_meter)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.13.15
Duration:  20.3 logical seconds, deterministic
Verdict:   PASSED
Rerun:     python examples/bona_flex/run_check.py --cases flex_clean

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  no_double_sell                 Passed                [ev-49, ev-52, ev-83]
                                 note: held + confirmed W never exceeded the assessed capacity
  acceptance_order_respected     Not tested           not exercised in this run
                                 note: no contention in this scenario
  retry_replays_original         Not tested           not exercised in this run
                                 note: no retried request
  retry_sends_identical_request  Not tested           not exercised in this run
                                 note: no retried request
  duplicate_no_extra_commitment  Passed                [ev-49, ev-52]
                                 note: exactly one hold per (buyer, idempotency key); 0 duplicated delivery(ies) seen
  settled_once                   Passed                [ev-73, ev-74, ev-76, ev-77]
                                 note: one delivery receipt per confirmed commitment; repeated settles returned it unchanged
  trust_accounted_once           Passed                [ev-11, ev-79, ev-80, ev-12, ev-81, ev-82]
                                 note: trust moved by exactly the settled commitments; repeats changed nothing
  receipts_verify                Passed                [ev-49, ev-52, ev-61, ev-64, ev-73, ev-74, ev-76, ev-77]
                                 note: every commitment and delivery carries a Bona receipt the desk verified (re-checked offline)
  money_follows_verdict          Passed                [ev-55, ev-58, ev-87, ev-90]
                                 note: escrow released only for credited verdicts, refunded otherwise
  capacity_not_disclosed         Passed                [ev-40, ev-43, ev-50, ev-53, ev-62, ev-65, ev-75, ev-78]
                                 note: no desk reply disclosed capacity
  clock_valid                    Passed                [ev-84]
                                 note: all six clock-validity checks held
  sidecar_bound                  Passed                [ev-85, ev-92]
                                 note: sidecar manifest sha256 bound in the trace (files re-checked offline)
  ledger_conserved               Passed               money was conserved across every movement [ev-55, ev-58, ev-87, ev-90]
                                 note: 200000 cents conserved across every movement
  privacy                        Passed               declared private fields never left the run [ev-92]
                                 note: fields ['budget_cents'] never left the run

Events recorded: 92. Intents recorded: 32.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
