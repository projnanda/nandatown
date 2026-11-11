**40 of 41 cases matched their expected verdict; every bundle verifies.**

### law

| case | profile | expected | observed | Cerata decision | failed buyer checks | match | `verify` | bundle |
|---|---|---|---|---|---|---|---|---|
| fl-call-after-8pm | quote-clean | PASSED | PASSED | block | — | yes | yes | `81c3b12fe546…` |
| call-ca-address-ny-number-early | quote-clean | PASSED | PASSED | block | — | yes | yes | `55459a1ff982…` |
| call-co-address-fl-number-late | quote-clean | PASSED | PASSED | block | — | yes | yes | `c2a71eff91cb…` |
| call-dst-fallback-after-9pm | quote-clean | PASSED | PASSED | block | — | yes | yes | `c92f07457aae…` |
| call-dst-fallback-uncovered-state | quote-clean | PASSED | PASSED | flag | — | yes | yes | `a0a8b988ca3c…` |
| ai-voice-no-written-consent | quote-clean | PASSED | PASSED | block | — | yes | yes | `a1d220cf2f10…` |
| call-national-dnc | quote-clean | PASSED | PASSED | block | — | yes | yes | `36b74d69233c…` |
| job-post-uncovered-state | quote-clean | PASSED | FAILED | hold | decision | NO | yes | `aea7b2e9b853…` |
| fl-fourth-call-in-24h | quote-clean | PASSED | PASSED | block | — | yes | yes | `8b9cc7db887e…` |
| text-consent-unknown | quote-clean | PASSED | PASSED | flag | — | yes | yes | `470555d6ad91…` |
| fl-clean-call | quote-clean | PASSED | PASSED | clear | — | yes | yes | `ca4c42e3c2f2…` |
| email-no-unsubscribe | quote-clean | PASSED | PASSED | block | — | yes | yes | `a343772342ec…` |
| email-compliant | quote-clean | PASSED | PASSED | clear | — | yes | yes | `65c2b48fe06a…` |
| text-after-stop | quote-clean | PASSED | PASSED | block | — | yes | yes | `144eb58faf35…` |
| rule-not-yet-in-force | quote-clean | PASSED | PASSED | clear | — | yes | yes | `3569d09ab393…` |
| fl-clean-call-repeat | quote-clean | PASSED | PASSED | clear | — | yes | yes | `0fb3771ff8df…` |
| fl-call-exactly-8pm | quote-clean | PASSED | PASSED | block | — | yes | yes | `03d8bab71a6b…` |
| call-utc-timestamp | quote-clean | PASSED | PASSED | block | — | yes | yes | `5e896313eacd…` |
| call-unknown-location | quote-clean | PASSED | PASSED | flag | — | yes | yes | `69a7a9d3a335…` |
| call-uncovered-state | quote-clean | PASSED | PASSED | block | — | yes | yes | `de833c39e86d…` |
| call-uncovered-purpose | quote-clean | PASSED | PASSED | flag | — | yes | yes | `1a87de7ab559…` |
| listing-no-kids | quote-clean | PASSED | PASSED | block | — | yes | yes | `b503d0c13371…` |

### faults

| case | profile | expected | observed | Cerata decision | failed buyer checks | match | `verify` | bundle |
|---|---|---|---|---|---|---|---|---|
| fl-call-after-8pm | quote-duplicate-delivery | PASSED | PASSED | block | — | yes | yes | `4f362bfc7b50…` |
| fl-call-after-8pm | quote-lost-ack | PASSED | PASSED | block | — | yes | yes | `2d1093497575…` |
| fl-call-after-8pm | quote-drop-wakeup | PASSED | PASSED | block | — | yes | yes | `3f2f6f7804de…` |
| fl-call-after-8pm | quote-crash-restart | PASSED | PASSED | block | — | yes | yes | `cbae2a19b3ff…` |
| fl-call-after-8pm | quote-amnesia-restart | PASSED | PASSED | block | — | yes | yes | `9d4e58dc1d36…` |
| fl-clean-call | quote-duplicate-delivery | PASSED | PASSED | clear | — | yes | yes | `444547f4560c…` |
| fl-clean-call | quote-lost-ack | PASSED | PASSED | clear | — | yes | yes | `9d9f1a3121e6…` |
| fl-clean-call | quote-drop-wakeup | PASSED | PASSED | clear | — | yes | yes | `b17aa7455c3c…` |
| fl-clean-call | quote-crash-restart | PASSED | PASSED | clear | — | yes | yes | `53ac5f35ae5f…` |
| fl-clean-call | quote-amnesia-restart | PASSED | PASSED | clear | — | yes | yes | `ef36146a1af2…` |

### control

| case | profile | expected | observed | Cerata decision | failed buyer checks | match | `verify` | bundle |
|---|---|---|---|---|---|---|---|---|
| naive-adapter-amnesia | quote-amnesia-restart | NOT_PASSED | INCOMPLETE | block | — | yes | yes | `ed09d77e79ff…` |
| cerata-unreachable | quote-clean | PASSED | PASSED | unavailable | — | yes | yes | `d571d89076ee…` |
| cerata-unreachable-expects-decision | quote-clean | FAILED | FAILED | unavailable | — | yes | yes | `52a4b0be10b0…` |
| bad-api-key | quote-clean | PASSED | PASSED | unavailable | — | yes | yes | `2f31a2b8652c…` |
| reference-buyer | quote-clean | INCOMPLETE | INCOMPLETE |  | — | yes | yes | `8129671f5450…` |

### repeat

| case | profile | expected | observed | Cerata decision | failed buyer checks | match | `verify` | bundle |
|---|---|---|---|---|---|---|---|---|
| listing-no-kids#1 | quote-clean | PASSED | PASSED | block | — | yes | yes | `d8a1ec32e99a…` |
| listing-no-kids#2 | quote-clean | PASSED | PASSED | block | — | yes | yes | `79db4d5faf56…` |
| listing-no-kids#3 | quote-clean | PASSED | PASSED | block | — | yes | yes | `a04eaeb97543…` |

### mcp

| case | profile | expected | observed | Cerata decision | failed buyer checks | match | `verify` | bundle |
|---|---|---|---|---|---|---|---|---|
| call-national-dnc | mcp_check.py | PASSED | PASSED |  | — | yes | — | `mcp.json` |

### mcp steps

- ✓ initialize
- ✓ tools/list
- ✓ tools/call preflight
- ✓ tools/call preflight (same idempotency key)
- ✓ tools/call verify
- ✓ refused without a valid key
