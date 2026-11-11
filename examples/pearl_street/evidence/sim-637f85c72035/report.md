NANDA Town System Fitness Report
========================================
Run:       sim-637f85c72035
Scenario:  pearl_street_peak (seed 7)
Faults:    none
Agents:    exchange (pearl_exchange), utility (grid_operator), meter (meter), dc-08 (flex_provider), pvb-48 (flex_provider), pvb-35 (flex_provider), pvb-12 (flex_provider), pvb-36 (flex_provider)
Releases:  evaluator lab-0.2.6, nandatown 0.2.0, python 3.12.10
Duration:  300.1 logical seconds, deterministic
Verdict:   FAILED
First broken stage: awards_within_attested_limits (pvb-35 awarded 18000 kW against an attested 8702 kW (declared max_kw 26000, accepted unchecked))
Rerun:     nandatown run T:\examples\pearl_street\pearl_street_peak.yaml --seed 7

The journey: bring, connect, attempt, disrupt, inspect, improve.

Stages (each one a separate claim with its own failure boundary):
  discovery_and_registration     Passed                [ev-12, ev-21, ev-43, ev-47, ev-51, ev-55, ev-59, ev-63]
                                 note: exchange, meter and 5 providers published verified cards; every provider's limits were attested and its register accepted
  boundary_rejections            Passed                [ev-151, ev-108, ev-134]
                                 note: pvb-35 telemetry -> unauthorized_sender; pvb-12 offer -> outside_mandate; pvb-36 offer -> after_gate_closure
  uniform_price_clearing         Passed                [ev-99, ev-102, ev-105, ev-111, ev-114, ev-117]
                                 note: pvb-48 9000 kW, pvb-35 18000 kW, dc-08 3000 kW; uniform price 120 $/MWh = floor of the last accepted offer
  settlement_matches_meter       Passed                [ev-286, ev-289, ev-292]
                                 note: pvb-48 delivered 9000 of 9000 kW, credit $3240.00, charge $0.00; pvb-35 delivered 8702 of 18000 kW, credit $3132.72, charge $27894.00; dc-08 delivered 3000 of 3000 kW, credit $1080.00, charge $0.00
  ledger_mirrors_settlement      Passed                [ev-297, ev-303, ev-301, ev-308]
                                 note: every credit was paid operator->provider and every shortfall charge provider->operator, in cents rounded from the settlement
  reputation_follows_delivery    Passed                [ev-299, ev-305, ev-310]
                                 note: pvb-48 record 1.000 -> good (score 1); pvb-35 record 0.483 -> bad (score -1); dc-08 record 1.000 -> good (score 1)
  settlement_coverage            Passed                [ev-286, ev-289, ev-292]
                                 note: all 5 configured providers accounted for: dc-08 settled once, pvb-48 settled once, pvb-35 settled once, pvb-12 rejected, pvb-36 rejected
  awards_within_attested_limits  Failed                [ev-111, ev-114, ev-117]
                                 note: pvb-35 awarded 18000 kW against an attested 8702 kW (declared max_kw 26000, accepted unchecked)
  ledger_conserved               Passed               money was conserved across every movement [ev-297, ev-301, ev-303, ev-308]
                                 note: 35000000 cents conserved across every movement
  privacy                        Not tested           not exercised in this run
                                 note: no redaction declared by this scenario

Events recorded: 311. Intents recorded: 144.
This result applies only to the named agents, releases, scenario, failure, evaluator, and time window.
One run is one scoped observation, not a certificate.
Improve: fix what failed and rerun the same profile.
