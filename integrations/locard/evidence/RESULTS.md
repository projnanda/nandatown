# Locard compatibility check results

- Town: `nandatown 0.2.0`, commit `6f9a7c0` (evaluator from the bundles: `0.5.0`)
- Locard service: locard-mvp `96b6c51` at `http://127.0.0.1:8700`
- Run: 2026-11-11 13:02 EST, `python integrations/locard/run_checks.py`

| Case | Profile | What it tests | Expected | Observed | Town verdict | Bundle verifies |
|---|---|---|---|---|---|---|
| insured-buyer ✅ | `quote-clean` | Insured agent asks the coverage-gated vendor for a quote | quote served | quote served; Locard asked 1x; buyer exit 0 | passed | yes (`run-ca443bc10ff3`) |
| town-buyer-uninsured ✅ | `quote-clean` | Town's own scripted buyer (another agent, no Locard policy) asks the same vendor | refused (coverage_required) | refused (coverage_required); Locard asked 0x; buyer exit 5 | not passed (first broken: received) | yes (`run-b98d08cd6b63`) |
| suspended-after-violation ✅ | `quote-clean` | Insured agent reports an over-cap purchase to Locard monitoring, then asks | refused (policy_suspended:over_spend_cap) | refused (policy_suspended:over_spend_cap); Locard asked 1x; buyer exit 6 | not passed (first broken: received) | yes (`run-8a0e7a3f806a`) |
| tampered-token ✅ | `quote-clean` | Insured agent presents its policy token with the signature altered | refused (invalid_signature) | refused (invalid_signature); Locard asked 1x; buyer exit 6 | not passed (first broken: received) | yes (`run-b2a6de207cfe`) |
| locard-unreachable ✅ | `quote-clean` | Valid policy, but the vendor cannot reach Locard (fail closed) | refused (locard_unreachable) | refused (locard_unreachable); Locard asked 1x; buyer exit 6 | not passed (first broken: received) | yes (`run-40c41b8d66cc`) |
| duplicate-delivery ✅ | `quote-duplicate-delivery` | Town offers the same quote request twice (fault); coverage checked once | quote served | quote served; Locard asked 1x; buyer exit 0 | passed | yes (`run-d7390c95304c`) |
| lost-ack ✅ | `quote-lost-ack` | Town drops the vendor's first acknowledgement (fault); the retry is safe | quote served | quote served; Locard asked 1x; buyer exit 0 | passed | yes (`run-a0b3fa0c5c69`) |

Every case passes only if all expected fields match. Bundles (canonical files, without `state/`) are in this folder, one directory per case; check any of them offline with `nandatown verify integrations/locard/evidence/<case>`.
