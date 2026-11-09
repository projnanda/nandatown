# Agent-Hunter: recorded offline runs

Evidence bundles from Town Path tests against Agent-Hunter in **offline mode**: the stub agents
and the mock providers, with no model calls and no keys (Agent-Hunter `main` at `5997003`,
`uv run python -m core.a2a --offline --port 8940`). Every hire result in them says
`"mode": "offline"`. They are not live results. This is a new path: Town had no convention for
third-party bundles.

| Folder | Profile | Run verdict | What it shows |
| -- | -- | -- | -- |
| `run3-trial-then-hire` | `a2a-trial-then-hire@0.1`, card pinned | failed | 5 of 6 stages passed. The verdict holds together, and the always-slow mock provider is ranked with 3 timeouts. `duplicate_request` failed: the door starts a second job and trial for a resent request (pass-through door, before the idempotency fix) |
| `run4-stock-timeout` | `a2a-trial-then-hire-stock-timeout@0.1` (15 s), card pinned | failed | `protocol_invocation` failed with `a2a_timeout` after 15 s, and the later stages are `not_tested`. One synchronous call at the stock limit can't fit a hire (provider deadline 20 s) |
| `run5-provider-mock-a` | `a2a-invoice-extraction@0.1`, card pinned | passed | All 6 stages passed for a mock provider agent |
| `extra-unscorable` | `a2a-trial-then-hire-unscorable@0.1`, card pinned | failed | `semantic_result` passed (rejected before any trial, no provider spend), and `duplicate_request` failed as in run 3 |
| `extra-provider-mock-e` | `a2a-invoice-extraction@0.1`, card pinned | failed | Negative control: `semantic_result` failed, because the mock's output isn't an invoice |

Check any of them without running anything:

```bash
nandatown verify examples/agent-hunter/offline/run3-trial-then-hire
nandatown verify-receipt examples/agent-hunter/offline/run3-trial-then-hire/receipt.json \
  --bundle examples/agent-hunter/offline/run3-trial-then-hire
```

`verify` replays the evaluator over the recorded observations. It doesn't call Agent-Hunter
again. The receipts are signed with a key made for these runs, which says who recorded them,
not that they are true.
