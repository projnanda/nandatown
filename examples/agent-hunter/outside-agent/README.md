# Agent-Hunter: Town's own agent as an outside candidate

Two recorded runs where Town's reference A2A agent (`nandatown a2a serve --port 8941`, the
`nandatown-quote-seller` quote fixture) sits in Agent-Hunter's hiring pool next to the mock
providers. Agent-Hunter didn't build it, and its card isn't in Agent-Hunter's card format. Both
runs use Agent-Hunter's **offline mode** (stub agents and mock providers, no model calls, no keys),
so every hire result says `"mode": "offline"`. They are not live results. They were recorded on
Nov 9, 2026.

**What this shows, and what it doesn't.** It shows that Agent-Hunter handles an A2A agent it
didn't build and whose card doesn't match its own format: it lists it from its standard A2A card,
filters it with a stated reason, and reports that reason to the requester. The quote seller is
Town's own fixture, so this is **not** evidence that an independently developed agent is
compatible with Agent-Hunter.

| | Version |
| -- | -- |
| Town | fork `integration/agent-hunter-trial-then-hire` at `ec84869` plus this branch's `path_runner` change (below), nandatown 0.2.0, Python 3.12.14 |
| Agent-Hunter | `45eaf26` for run 1, `9c3634a` for run 2 (branch `chris333/age-60-towns-own-agent-as-an-outside-candidate`; run 2 adds only the test-only card proxy) |
| Town's agent | `nandatown a2a serve --port 8941` (card `name: nandatown-quote-seller`, version `0.2.0`, one skill `quote`, no price, no region) |

**Town change on this branch.** The trial-hire runner (`_hire_observation` in
`src/nandatown/path_runner.py`) now copies the hire result's `candidates` into the recorded
`fulfillment_observed` event as sent. Nothing judges it, and stage outcomes don't change.
Without it, the bundle would hold only the digest of the reply that names the excluded agent.

| Folder | Profile | Run verdict | What it shows |
| -- | -- | -- | -- |
| `town-agent-excluded` | `a2a-trial-then-hire@0.1`, card pinned | passed (6/6) | Town's agent is listed from its plain A2A card, and the Candidate Finder excludes it with a reason before any trial. The hire goes ahead with the 5 mocks |
| `town-agent-misleading-card` | `a2a-trial-then-hire@0.1`, card pinned | passed (6/6) | Stretch: the same agent behind a test-only card that *claims* `invoice_extraction` at $0.01/doc. It passes the filter and is trialed. Every reply is a Town error, so it scores 0, ranks last, and isn't recommended |

## Step 1: baseline, before Agent-Hunter's A2A registry (AGE-59)

On Agent-Hunter `294bfd7` (before AGE-59), the registry knows only its own layout,
`{base}/p/{slug}/.well-known/agent-card.json`, and its own card format. With
`PROVIDERS_BASE_URL=http://127.0.0.1:8941` and `PROVIDER_SLUGS=nandatown-quote-seller`:

```
WARNING core.runner.registry: provider nandatown-quote-seller left out: card at
http://127.0.0.1:8941/p/nandatown-quote-seller/.well-known/agent-card.json unavailable
(Client error '404 Not Found' for url '...')
listed: []
```

Even at Town's own card path, the card fails Agent-Hunter's `AgentCard` on 4 missing fields
(`slug`, `job_types`, `price_per_doc_usd`, `region`). The old registry had no way to list Town's
agent at all. No bundle was recorded for this step, because nothing reaches Town.

## Run 1: `town-agent-excluded`

```bash
nandatown a2a serve --port 8941
# in agent-hunter:
PROVIDER_TRANSPORT=a2a PROVIDER_A2A_URLS=http://127.0.0.1:8941 \
  uv run python -m core.a2a --offline --port 8940
nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire@0.1 \
  --pin-card-digest sha256:aaa71d5db13797303980ac5ae6d1b310a6fc8cda3afafec61e3dd07fe328d9e3
```

Run `path-39b97ff3a07a`, verdict **passed**. Bundle fingerprint
`sha256:a7a6e15ee4f4c4e964fef463fb602e595bb04885b211f3d31dcb498819c98a43`.

| Stage | Expected | Observed |
| -- | -- | -- |
| resolution | passed | passed |
| agent_card_retrieval | passed | passed (card `sha256:aaa71d5db1379730`, version `0.1.0+45eaf26`) |
| descriptor_consistency | passed | passed (matches the pinned card) |
| protocol_invocation | passed | passed (task `task-788d34b6921d` `completed`) |
| semantic_result | passed, with Town's agent in `excluded` | passed: job `job-5aaf110c7129` ranked 5 candidates, verdict `tie`, recommended `mock-b@1` |
| duplicate_request | passed | passed (same task, same job, same content digest) |

The `candidates` field recorded in both `fulfillment_observed` events (`ev-5`, `ev-7`):

```json
{"considered": ["mock-a@1", "mock-b@1", "mock-c@1", "mock-d@1", "mock-e@1"],
 "excluded": [{"id": "nandatown-quote-seller@0.2.0",
               "reason": "doesn't declare job type 'invoice_extraction' (declares ['quote'])"}]}
```

The reason is the first failing check. The card also has no price, which would fail the budget
filter next. Town's agent received no trial task.

## Run 2 (stretch): `town-agent-misleading-card`

A test-only proxy (`mocks/misleading_card.py` in agent-hunter) serves Town's card rewritten to
declare `invoice_extraction` at $0.01/doc, region `global`. It forwards every `message/send` to
Town's agent unchanged. Without a price, the budget filter would exclude the agent before any
trial.

```bash
# in agent-hunter:
uv run python -m mocks.misleading_card --upstream http://127.0.0.1:8941 --port 8942
PROVIDER_TRANSPORT=a2a PROVIDER_A2A_URLS=http://127.0.0.1:8942 \
  uv run python -m core.a2a --offline --port 8940
nandatown test-agent --url http://127.0.0.1:8940 --path-profile a2a-trial-then-hire@0.1 \
  --pin-card-digest sha256:3d7bf882d39b5428ba7d87d53c8656a1e05204481c8479823c6861833cd7aaa7
```

Run `path-cc2bc79f9a7e`, verdict **passed**. Bundle fingerprint
`sha256:8d1735889324ca9bd39aa5190699c78bcda8ae440f5c23bf1750dee0ca301a2f`.

| Stage | Observed |
| -- | -- |
| resolution, agent_card_retrieval, descriptor_consistency | passed (card `sha256:3d7bf882d39b5428`, version `0.1.0+9c3634a`) |
| protocol_invocation | passed (task `task-fe94f30c517b` `completed`) |
| semantic_result | passed: job `job-b422e2e8e51a` ranked 6 candidates, verdict `recommended`, recommended `mock-a@1` |
| duplicate_request | passed |

The recorded hire result has `candidates.considered` with all 6 (the 5 mocks and
`nandatown-quote-seller@0.2.0`), `excluded: []`, and this ranking:

| Provider | Accuracy | 95% CI |
| -- | -- | -- |
| mock-a@1 | 1.0 | [1.0, 1.0] |
| mock-b@1 | 0.867 | [0.80, 0.95] |
| mock-c@1 | 0.6 | [0.40, 0.70] |
| mock-d@1 | 0.0 | [0.0, 0.0] |
| mock-e@1 | 0.0 | [0.0, 0.0] |
| nandatown-quote-seller@0.2.0 | 0.0 | [0.0, 0.0] |

In Agent-Hunter's job record, all 3 of the quote seller's trial submissions are `error`. Town's
agent answers a provider task with a `failed` task (`bad request: 'quantity'`), because the body
isn't a quote request. So the card said it could do the job, and the trial showed it couldn't.

## Check the bundles

```bash
nandatown verify examples/agent-hunter/outside-agent/town-agent-excluded
nandatown verify examples/agent-hunter/outside-agent/town-agent-misleading-card
nandatown verify-receipt examples/agent-hunter/outside-agent/town-agent-excluded/receipt.json \
  --bundle examples/agent-hunter/outside-agent/town-agent-excluded
```

`verify` replays the evaluator over the recorded observations without calling Agent-Hunter.
`nandatown proof` granted `TOWN-TESTED` on both bundles (6 stages tested). The receipts are
signed with a key made for these runs. That says who recorded them, not that they are true.
