# ATL in Nanda Town: campaign summary

## Lab scenarios (seeded simulation, no model)

| Scenario / payments layer | Runs | Verdict PASSED | Money to the rival (cents, total) | ATL verdicts | Valid ATL receipts |
|---|---|---|---|---|---|
| marketplace / ledger.v1 (Town baseline) | 100 | 100 | 0 | none | 0 |
| marketplace / atl.v1 | 100 | 100 | 0 | approve 200 | 200 |
| registry_eviction_unowned / ledger.v1 (Town baseline) | 100 | 0 | 378000 | none | 0 |
| registry_eviction_unowned / atl.v1 | 100 | 0 | 0 | refuse 100 | 0 |
| registry_eviction (index.owned.v1) / ledger.v1 (Town baseline) | 100 | 100 | 0 | none | 0 |
| registry_eviction (index.owned.v1) / atl.v1 | 100 | 100 | 0 | approve 100 | 100 |
| over-cap purchase / atl.v1 | 100 | 100 | 0 | escalate 100 | 0 |

Stages that did not pass:

- registry_eviction_unowned / atl.v1: `honest_trade_completed` failed in 100 run(s)
- registry_eviction_unowned / atl.v1: `listing_intact` failed in 100 run(s)
- registry_eviction_unowned / ledger.v1 (Town baseline): `honest_trade_completed` failed in 100 run(s)
- registry_eviction_unowned / ledger.v1 (Town baseline): `listing_intact` failed in 100 run(s)
