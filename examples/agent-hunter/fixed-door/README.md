# Agent-Hunter: live reruns on the fixed door

The rerun step of the testing guide. It's the same Town profile as `../baseline`, against
Agent-Hunter's A2A door in **live mode** (real model calls, Agent-Hunter's 7 real provider
agents), after the idempotency fix. It was recorded on Nov 9, 2026. The whole chain is A2A: Town
sends `message/send` to the door, and the door sends each trial task to the providers over A2A
(`PROVIDER_TRANSPORT=a2a`).

**What changed since the baseline:** the door now dedupes hire requests by `request_id`. A
byte-identical resend returns the stored task and hire result: the same `job_id`, the same trial,
and no new model calls. The same `request_id` with changed terms is refused.

| | Version |
| -- | -- |
| Town | fork `integration/agent-hunter-trial-then-hire` at `96a92f2`, nandatown 0.2.0, Python 3.12.14 |
| Agent-Hunter | `cf121e6` (card version `0.1.0+cf121e6`; idempotency fix plus provider calls over A2A) |
| Provider service | `cbf6532` (private repo; providers speak A2A, deduped by `request_id`) |
| Card digest (pinned) | `sha256:f4386869a5a275d6826de26b996e50fe10be2dde005ee9d2a3b39bdde4d2b740` |

## Commands

Start the door with `PROVIDER_TRANSPORT=a2a uv run python -m core.a2a --port 8940` (live mode,
with pango on the library path for the trial PDFs) and the provider service on port 8100. Then:

```bash
# Run 1: the fixed door
nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire@0.1 \
  --pin-card-digest sha256:f4386869a5a275d6826de26b996e50fe10be2dde005ee9d2a3b39bdde4d2b740
# Run 2: a job Agent-Hunter can't score (the boundary case)
nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire-unscorable@0.1 \
  --pin-card-digest sha256:f4386869a5a275d6826de26b996e50fe10be2dde005ee9d2a3b39bdde4d2b740
```

The card digest was read from the door's card (Town's `fingerprint` of the fetched card), so no
unpinned run was needed.

## Run 1: fixed door, `a2a-trial-then-hire@0.1`

Run `path-ee1726c7f810`, verdict **passed**, 54 s. Bundle fingerprint
`sha256:831ac2943f9661c16d964e2622da8cfc9e3a9d249ca8ec7086124855d076eb2d`.

| Stage | Baseline (`e356852`) | Expected now | Observed |
| -- | -- | -- | -- |
| resolution | passed | passed | passed |
| agent_card_retrieval | passed | passed | passed (card `sha256:f4386869a5a275d6`) |
| descriptor_consistency | passed | passed | passed (matches the pinned card) |
| protocol_invocation | passed | passed | passed (task `task-c3af229fb0ae` `completed`) |
| semantic_result | passed | passed | passed: live run, job ranked 7 candidates, verdict `tie`, with a recommended provider that Town checked holds together |
| duplicate_request | **failed** | passed | **passed**: both attempts returned `job-9b133d9891a2` / `trial-45219028e32a` and task `task-c3af229fb0ae` |

`nandatown proof` on the full bundle granted the badge:

```
TOWN-TESTED
Capability trial_then_hire was observed to pass for release basis sha256:766de600124b6815,
under profile a2a-trial-then-hire@0.1, by observer did:town:b96f2d594579f7b09cc0e6fb,
during window 2026-11-09, with coverage: 6 stages tested.
```

`verify`, `receipt` and `verify-receipt --bundle` also passed on the full bundle before it was
deleted.

**Spend:** the duplicate cost nothing. In the baseline, the second attempt cost $0.079756.

| Attempt | job_id | Platform | Providers | Total |
| -- | -- | -- | -- | -- |
| 1 | `job-9b133d9891a2` | $0.000708 (4 calls) | $0.090800 | $0.091508 |
| 2 (duplicate) | `job-9b133d9891a2` (same) | $0 (no new calls) | $0 (no new calls) | $0 |

Here's how "no new calls" was checked. During the run, Agent-Hunter's store gained one job, not two.
The platform cost log gained 4 rows, all for that job. The provider service's cost log gained 48
rows totalling $0.0908, which equals the `providers_usd` both attempts report, because the second
one is the stored result. All 42 trial submissions (7 providers × 6 documents) were `ok`.

## Run 2: unscorable job, `a2a-trial-then-hire-unscorable@0.1`

Run `path-7217932335ba`, verdict **passed**, 2 s. Bundle fingerprint
`sha256:11ef6a803d5b8c94d949b5c23b698a167dd87526d79b610969498f2f4de50270`. The full bundle is in
`live-unscorable/`.

| Stage | Expected | Observed |
| -- | -- | -- |
| resolution, agent_card_retrieval, descriptor_consistency | passed | passed |
| protocol_invocation | passed | passed (task `task-227a0414f5d3` `completed`) |
| semantic_result | passed: `rejected`, no verdict, no provider paid | passed: `job-1a02803b030f` rejected before any trial ("persuasive, subjective cover letter rather than extracting objectively scored invoice fields"), `providers_usd` 0 |
| duplicate_request | passed | passed: both attempts returned `job-1a02803b030f`, status `rejected` |

**Spend:** $0.000048, which is one Job Reviewer call on the platform account. Provider spend was $0,
and the provider service's cost log gained no rows. The offline version of this run
(`../offline/extra-unscorable`, before the fix) failed `duplicate_request`. With the fix, it passes.

## Also run against the providers directly

The same session also ran `a2a-invoice-extraction@0.1` against each of Agent-Hunter's 7 real
provider agents. Those results are withheld (hidden-traits rule), and their bundles were deleted.
For the public provider-level profile, including a failing case, see
`../offline/run5-provider-mock-a` and `../offline/extra-provider-mock-e`.

## What's in this folder, and what's withheld

| Folder | Contents |
| -- | -- |
| `live-trial-then-hire` | Run 1: the signed `receipt.json` and `attestation.json`, `manifest.json` (file hashes and the bundle fingerprint) and `run.json` (config and rerun command) |
| `live-unscorable` | Run 2: the full bundle. It's rejected before any trial, so it holds no provider data |

**Run 1's events, result and report are withheld.** They record each provider agent's name and
trial accuracy. In Agent-Hunter, those results are meant to be discovered by trialling the
providers, so they aren't published. The full bundle wasn't kept after the checks above, so its
receipt can only be checked on its own:

```bash
nandatown verify-receipt examples/agent-hunter/fixed-door/live-trial-then-hire/receipt.json
nandatown verify examples/agent-hunter/fixed-door/live-unscorable
nandatown verify-receipt examples/agent-hunter/fixed-door/live-unscorable/receipt.json \
  --bundle examples/agent-hunter/fixed-door/live-unscorable
```

## Limitations

- **One run is one scoped observation, not a certificate.** Town checked that the verdict holds
  together and that the resend caused no second fulfillment. It didn't check that the recommended
  provider is the best one.
- **Both resends came within seconds, to the same running processes.** The door keeps its
  `request_id` mapping in its job store (so it should survive a restart), and the provider
  service keeps its own in memory, per process. These runs don't test a resend after a restart or
  one sent to a different replica.
- **Only an exact resend was tested.** Changed terms under the same `request_id` are refused, but
  that is covered by Agent-Hunter's unit tests, not by a Town run here.
- **Live reproduction needs the private provider service and Parley keys.** The public way to
  reproduce is the offline door (`--offline`) or the bundles in `../offline`.
- **The profile tests one synchronous `message/send` with a 240 s per-phase timeout.** Async
  `tasks/get` polling, streaming, payment and the hire step aren't tested.
