NANDA Town System Fitness Report
========================================
Run:       sim-48cb7f470eff
Scenario:  rebbi_lost_reply (seed 42)
Faults:    drop borrow_decision
Agents:    rebbi (rebbi_adapter), lender-a (rebbi_lender), lender-b (rebbi_lender), worker-01 (rebbi_worker), client (rebbi_client)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.13.15
Duration:  30.0 logical seconds, deterministic
Verdict:   FAILED
First broken stage: one_loan_per_request (worker-01/job-1/borrow-1 booked loans [1, 2]; Rebbi's ledger holds 2 loans for 1 lent request(s))
Rerun:     nandatown run examples/rebbi/rebbi_lost_reply.yaml --seed 42 --plugin examples/rebbi/rebbi_plugin.py

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  rebbi_connected       Passed                [ev-9, ev-10, ev-11]
                        note: 3 live MCP sessions, one per fleet agent
  rebbi_version_pinned  Passed                [ev-8]
                        note: Rebbi at 3db016d06ad1b0ef093a0a2ae7fe54703e060edd, clean tree
  headroom_published    Passed                [ev-21, ev-25]
                        note: every lender's reserve is published in Rebbi
  fault_injected        Passed                [ev-46]
                        note: Town injected a drop of borrow_decision
  worker_retried        Passed                [ev-47]
                        note: the worker resent the same request
  loan_granted          Passed                [ev-44]
                        note: Rebbi lent the full shortfall, lender reserve intact
  one_loan_per_request  Failed                [ev-44, ev-51]
                        note: worker-01/job-1/borrow-1 booked loans [1, 2]; Rebbi's ledger holds 2 loans for 1 lent request(s)
  job_paid              Passed                [ev-74, ev-72]
                        note: the client received the result and released escrow
  loans_settled         Failed                [ev-80, ev-76]
                        note: loan 1 is active
  rebbi_invariants      Passed                [ev-80]
                        note: Rebbi's own ledger check passes
  retry_gets_original   Failed                [ev-44, ev-51]
                        note: worker-01/job-1/borrow-1: a repeat booked loan 2 instead of returning loan 1
  ledger_conserved      Passed               money was conserved across every movement [ev-31, ev-72]
                        note: 10000 cents conserved across every movement
  privacy               Not tested           not exercised in this run
                        note: no redaction declared by this scenario

Events recorded: 82. Intents recorded: 22.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
