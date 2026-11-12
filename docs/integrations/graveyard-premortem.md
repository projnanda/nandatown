# A participant with an outside safety dependency

[`examples/graveyard_seller.py`](../../examples/graveyard_seller.py) is a Town
seller that consults an external MCP service before it applies work. The
service is the [Agentic Graveyard](https://agentic-graveyard.vercel.app), a
record of agent failures that returns the check which would have prevented
each one.

The quote does not change. What this exercises is custody when a participant
depends on something Town does not control, which is the ordinary condition of
a real agent. Three questions, each with a recorded answer:

1. Does an outside lookup inside the claim break the protocol?
2. When the service is down, does the participant refuse or answer unguarded?
3. Can Town tell a refusal apart from a crash?

The third one turned out to be the interesting question.

## Versions

| Component | Version |
| --- | --- |
| Town commit | `6f9a7c064486` |
| `nandatown` | 0.2.0, evaluator 0.5.0 |
| Python | 3.12.10, Windows 11 |
| Service | `agentic-graveyard` 1.1.0, MCP protocol `2025-06-18` |
| Endpoint | `https://agentic-graveyard.vercel.app/api/mcp` |

The participant is standard library only, like `examples/byoa_seller.py`. It
needs no model credentials and no third-party package, so the runs below
reproduce without an API key. The service is a public deployment; the author
of this integration also maintains it, so this is a compatibility check, not
an independent adoption result.

## Reproduce

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv/Scripts/activate
python -m pip install -e .

SELLER="python examples/graveyard_seller.py"

# 1. baseline: service reachable
nandatown test-agent --role seller --cmd "$SELLER" --profile quote-clean

# 2. idempotency: the same work offered twice
nandatown test-agent --role seller --cmd "$SELLER" \
    --profile quote-duplicate-delivery

# 3. service down, 5 s lease, fail closed (the default)
GRAVEYARD_URL=http://127.0.0.1:9/mcp \
nandatown test-agent --role seller --cmd "$SELLER" --profile quote-clean

# 4. service down, 5 s lease, fail open
GRAVEYARD_URL=http://127.0.0.1:9/mcp GRAVEYARD_POLICY=fail-open \
nandatown test-agent --role seller --cmd "$SELLER" --profile quote-clean

# 5. service down, 1.5 s lease, lookup timeout OVER the lease
GRAVEYARD_URL=http://127.0.0.1:9/mcp \
nandatown test-agent --role seller --cmd "$SELLER" \
    --profile quote-crash-restart

# 6. the same, with the lookup timeout UNDER the lease
GRAVEYARD_URL=http://127.0.0.1:9/mcp GRAVEYARD_TIMEOUT=0.8 \
nandatown test-agent --role seller --cmd "$SELLER" \
    --profile quote-crash-restart
```

Port 9 refuses connections, so case 3 onward need no network. Verify any
bundle offline with `nandatown verify <bundle>`.

## Expected and observed

| # | Profile, lease | Service | Policy | Expected | Observed | Run |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `quote-clean`, 5 s | up | closed | passes, guard recorded | **PASSED**, 6/7 stages, lookup 140 ms | `run-858c721e0fb1` |
| 2 | `quote-duplicate-delivery`, 5 s | up | closed | applied once | **PASSED**, 7/8 stages, one application, replayed response | `run-3fd83f83a406` |
| 3 | `quote-clean`, 5 s | down | closed | refuses, no response | **INCOMPLETE**, 2/7, 20 × `retryable`, `processed` inconclusive | `run-74eb120c5f33` |
| 4 | `quote-clean`, 5 s | down | open | answers, marked degraded | **PASSED**, 6/7, note carries `degraded: true` | `run-2c14062e8ec9` |
| 5 | `quote-crash-restart`, 1.5 s | down | closed | refusal recorded | **no acknowledgement at all**: 20 claims, 17 `claim_expired`, 20 `stale_fence_rejected`, 0 `ack_recorded` | `run-eeae9274465e` |
| 6 | `quote-crash-restart`, 1.5 s | down | closed | refusal recorded | 38 claims, 38 `ack_recorded` all `retryable`, lookup 797–829 ms | `run-4cbd86fe3588` |

Bundles for 1, 5 and 6 are under [`evidence/`](evidence), with `state/`
removed as the testing guide requires. All three still verify.

## What cases 5 and 6 show

A lookup against the live service costs 140–188 ms. A refused connection costs
about 2.0 s on this platform, whatever the configured timeout, because the
connect attempt itself is what takes the time. The crash and amnesia profiles
run a 1.5 s lease.

So in case 5 the lease expired while the participant was still waiting to find
out that its dependency was unreachable. Its `retryable` acknowledgement then
arrived against a dead fence and Town rejected it, correctly: custody had
already moved on. Its twenty claims produced seventeen `claim_expired`
events, twenty `stale_fence_rejected` events, and **not one `ack_recorded`
event**. The participant was refusing on every single attempt and the evidence
contains no trace of a refusal. A reviewer reading that bundle sees a
participant that died.

Case 6 changes one value, `GRAVEYARD_TIMEOUT=0.8`, putting the lookup ceiling
under the lease. Its thirty-eight claims produced thirty-eight
`ack_recorded` events, each with status `retryable`, the reason `pre-mortem
unavailable, failing closed`, and a measured latency of 797–829 ms. Same
outage, same policy, same refusal — but now it is attributable.

The general rule, which is not specific to this service: **a participant's
timeout for an outside dependency must be strictly under its lease, or its
refusal is indistinguishable from its death.** Failing closed is not free. It
costs a round trip that has to fit in the window, and a fail-closed design
that does not budget for that silently degrades into looking crashed. Nothing
in Town's contract is wrong here; the lesson is for whoever wires the
dependency in.

A first version of the participant also exited non-zero on the rejected stale
fence, because it let the `HTTPError` propagate. That is the same failure
wearing a worse disguise: an unhandled exception is read by the runner as a
crashed participant. It now treats a refused acknowledgement as a lost lease
and waits for redelivery. See `lost_lease` in the participant.

## A note on the service

Two paraphrases of the same benign task returned opposite answers:

```
"quote 2 widgets at 1995 cents for a buyer in a marketplace"
  -> 10 past agent failures match (-)

"respond to a quote request for 2 x widget at 1995 cents
 as an autonomous selling agent in a marketplace"
  -> no known graves for this task (622 searched). Proceed normally.
```

The participant sends the second phrasing, so every run above recorded
`graves: 0, abstained: true`. That is the correct answer for a quote request:
the task is not destructive. But the instability under paraphrase is a
retrieval property worth knowing if you depend on such a service, and it is
why the acknowledgement note records the grave count rather than only that the
service was reached. `consulted` is not `warned`.

## Limitations

- The guarded path is never exercised with an actual warning, because Town's
  Track task is a commerce quote and the service correctly abstains on it.
  What is demonstrated is dependency behaviour, not guard quality.
- Track runs a fixed coordinator contract, so a destructive task cannot be
  expressed here. Testing whether such a service improves an agent's plan
  needs a task Town does not currently offer.
- The participant does not act on `FAULT`, which only Town's own scripted
  participants do. In the crash profiles its crash-specific stages therefore
  stay inconclusive, and the verdicts in cases 5 and 6 are `incomplete` for
  that reason as well as for the dependency outage.
- `portable_identity` is untested throughout: these are token runs. The
  participant cannot produce an Ed25519 session proof, so `--identity` runs
  are out of scope, exactly as for `examples/byoa_seller.py`.
- No run renders a Town Proof badge, and none should: case 1 leaves a stage
  untested and the rest are incomplete.
- The ~2.0 s connect cost is one platform's measurement, not a constant.
  Measure it where you deploy before choosing a timeout.
