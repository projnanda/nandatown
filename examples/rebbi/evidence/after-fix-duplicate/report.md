NANDA Town System Fitness Report
========================================
Run:       sim-05b41a108c10
Scenario:  rebbi_duplicate (seed 42)
Faults:    duplicate borrow_request
Agents:    rebbi (rebbi_adapter), lender-a (rebbi_lender), lender-b (rebbi_lender), worker-01 (rebbi_worker), client (rebbi_client)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.13.15
Duration:  30.0 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/rebbi/rebbi_duplicate.yaml --seed 42 --plugin examples/rebbi/rebbi_plugin.py

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  rebbi_connected          Passed                [ev-9, ev-10, ev-11]
                           note: 3 live MCP sessions, one per fleet agent
  rebbi_version_pinned     Passed                [ev-8]
                           note: Rebbi at 07da0565da42d74a224dcd2148c03eac0105d37a, clean tree
  headroom_published       Passed                [ev-21, ev-25]
                           note: every lender's reserve is published in Rebbi
  fault_injected           Passed                [ev-43]
                           note: Town injected a duplicate of borrow_request
  loan_granted             Passed                [ev-45]
                           note: Rebbi lent the full shortfall, lender reserve intact
  one_loan_per_request     Passed                [ev-45, ev-48]
                           note: 1 request(s), one loan each
  job_paid                 Passed                [ev-73, ev-71]
                           note: the client received the result and released escrow
  loans_settled            Passed                [ev-79, ev-75]
                           note: every loan settled; unspent tokens went back
  rebbi_invariants         Passed                [ev-79]
                           note: Rebbi's own ledger check passes
  duplicate_gets_original  Passed                [ev-45, ev-48]
                           note: the repeat got the original decision back, marked replayed
  ledger_conserved         Passed               money was conserved across every movement [ev-31, ev-71]
                           note: 10000 cents conserved across every movement
  privacy                  Not tested           not exercised in this run
                           note: no redaction declared by this scenario

Events recorded: 81. Intents recorded: 21.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
