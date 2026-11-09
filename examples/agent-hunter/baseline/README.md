# Agent-Hunter: baseline live run

The baseline step of the testing guide: Town against Agent-Hunter's A2A door in **live mode**, with
real model calls and Agent-Hunter's 7 real provider agents. This is the pass-through door (no
idempotency yet), recorded Nov 9, 2026.

**Interface tested:** one JSON-RPC `message/send` to Agent-Hunter's A2A door carrying a
trial-then-hire job posting. Agent-Hunter reviews the job, builds a trial, sends it to the
provider agents over HTTP, scores them and returns a ranked verdict in one text artifact. Town
then resends the byte-identical request (`controlled_condition: duplicate_request`).

| | Version |
| -- | -- |
| Town | fork `integration/agent-hunter-trial-then-hire` at `c4c69f2`, nandatown 0.2.0, Python 3.12.14 |
| Agent-Hunter | `e356852` (card version `0.1.0+e356852`) |
| Provider service | `5d9536a` (private repo) |
| Profile / evaluator | `a2a-trial-then-hire@0.1` (`sha256:766de600124b6815…`) / `path-trial-hire-0.1` |
| Card digest (pinned) | `sha256:3620f36b732f51a2b6b15fac7b3b40e714fa89a47209f1821ac2e8452c25c48a` |

## Command

```bash
nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire@0.1 \
  --pin-card-digest sha256:3620f36b732f51a2b6b15fac7b3b40e714fa89a47209f1821ac2e8452c25c48a
```

Before that, the door was started with `uv run python -m core.a2a --port 8940` (live mode, with
pango on the library path for the trial PDFs), and the provider service on port 8100.

## Expected vs. observed

| Stage | Expected | Observed |
| -- | -- | -- |
| resolution | passed | passed |
| agent_card_retrieval | passed | passed (card `sha256:3620f36b732f51a2`) |
| descriptor_consistency | passed | passed (matches the pinned card) |
| protocol_invocation | passed | passed (task `completed`) |
| semantic_result | passed | passed: live run, job ranked 7 candidates, verdict `tie`, with a recommended provider that Town checked holds together |
| duplicate_request | **failed** | **failed**: the resent request started a second job and trial (`job-e2dfc89df301` / `trial-c2ec79bfd96e`, then `job-34587feb1e28` / `trial-74eba7aae101`) |

Run verdict: **failed** (run `path-68c8581c6f5b`, 1 min 42 s). `nandatown proof` refuses, as it
should for a failed run.

The duplicate cost the job twice:

| Attempt | job_id | Platform | Providers | Total |
| -- | -- | -- | -- | -- |
| 1 | `job-e2dfc89df301` | $0.000599 | $0.100492 | $0.101091 |
| 2 (duplicate) | `job-34587feb1e28` | $0.000692 | $0.079064 | $0.079756 |

Both attempts returned the same recommendation and the same ranking. This time the second trial
changed only the cost (it ran on new synthetic documents), not the pick. Town fails the duplicate
either way, because a resent order must not cause a second fulfillment. Agent-Hunter's unit tests
passed on this commit. One of them asserts that a resent request starts a second job, because the
door was left pass-through on purpose so Town could record this failure before the fix. Those tests
check behaviour against Agent-Hunter's own expectations. Town's duplicate check judges it against
the requester's: a resent order must not be paid for twice. This is the failure the follow-up
change fixes.

## What's in this folder, and what's withheld

| Folder | Contents |
| -- | -- |
| `live-trial-then-hire` | The signed `receipt.json` and `attestation.json`, `manifest.json` (file hashes and the bundle fingerprint `sha256:ef3a04e786ed4e26535a70c4dea5ec9aad983e45fa8ad85ee3bcd236fe968a30`) and `run.json` (config and rerun command) |
| `control-capability-fulfillment` | Full bundle. Control: Town's quote profile `a2a-capability-fulfillment@0.3` against a hiring agent. The door answers the quote body with `failed` + `invalid_request`, so `protocol_invocation` fails and the later stages are `not_tested`. No job, no spend |

**The live bundle's events and result are withheld.** They record each provider agent's name and
trial accuracy. In Agent-Hunter, those results are meant to be discovered by trialling the
providers, so they aren't published. The full bundle wasn't kept after the run, so the receipt
can't be checked against it (`verify-receipt --bundle`). The receipt's signature still verifies on
its own, and a live rerun produces a new bundle with its own receipt, not this one.

You can check the receipt's signature without the bundle:

```bash
nandatown verify-receipt examples/agent-hunter/baseline/live-trial-then-hire/receipt.json
nandatown verify examples/agent-hunter/baseline/control-capability-fulfillment
```

To replay the same failure offline, use `../offline/run3-trial-then-hire`. That's the same profile
against the offline door with mock providers, and its full bundle replays with `nandatown verify`.

## Limitations

- **One run is one scoped observation, not a certificate.** Town checked that the verdict holds
  together, not that the recommended provider is the best one.
- **Live reproduction needs the private provider service and Parley keys.** The public way to
  reproduce is the offline bundle above, or rerunning the door with `--offline`.
- **The profile tests one synchronous `message/send` with a 240 s per-phase timeout.** Async
  `tasks/get` polling, streaming, payment and the hire step aren't tested.
