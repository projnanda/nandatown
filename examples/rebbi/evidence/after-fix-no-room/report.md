NANDA Town System Fitness Report
========================================
Run:       sim-0f55dd860d1c
Scenario:  rebbi_no_room (seed 42)
Faults:    none
Agents:    rebbi (rebbi_adapter), lender-a (rebbi_lender), lender-b (rebbi_lender), worker-01 (rebbi_worker), client (rebbi_client)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.13.15
Duration:  30.0 logical seconds, deterministic
Verdict:   PASSED
Rerun:     nandatown run examples/rebbi/rebbi_no_room.yaml --seed 42 --plugin examples/rebbi/rebbi_plugin.py

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  rebbi_connected        Passed                [ev-9, ev-10, ev-11]
                         note: 3 live MCP sessions, one per fleet agent
  rebbi_version_pinned   Passed                [ev-8]
                         note: Rebbi at 07da0565da42d74a224dcd2148c03eac0105d37a, clean tree
  headroom_published     Passed                [ev-21, ev-25]
                         note: every lender's reserve is published in Rebbi
  cannot_move_explained  Passed                [ev-44]
                         note: CANNOT_MOVE, with each lender's reason
  job_refunded           Passed                [ev-51, ev-50]
                         note: the worker declined and the client got its escrow back
  nothing_spent          Passed                [ev-52]
                         note: the worker spent nothing it did not have
  no_loans               Passed                [ev-52]
                         note: no loan was booked
  rebbi_invariants       Passed                [ev-52]
                         note: Rebbi's own ledger check passes
  ledger_conserved       Passed               money was conserved across every movement [ev-31, ev-50]
                         note: 10000 cents conserved across every movement
  privacy                Not tested           not exercised in this run
                         note: no redaction declared by this scenario

Events recorded: 54. Intents recorded: 12.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
