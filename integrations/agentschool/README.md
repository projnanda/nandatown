# Checking AgentSchool credentials before Town delegates work

[AgentSchool](https://github.com/justinspar/agentschool) trains AI agents in a
skill, examines them on held-out work, and issues a signed, expiring credential
that other agents verify before they delegate. This directory puts that
credential in Town's trust layer, so a Town buyer hires only an agent that can
present one and prove it holds the key the credential names.

AgentSchool runs as its own service elsewhere, and nothing here is a copy of
it: Town reads the credential format over the wire and calls AgentSchool's HTTP
tools, so this directory holds only the plugin, the scenarios, the captured
fixture, the tests and this document.

## What one run does, start to finish

```
 Town registry                 Town's trust layer          AgentSchool
 (sellers advertise)           (agentschool.v1)            (separate service)
       │                              │                           │
   1. lookup ──────────────────────────┤                           │
   2. credential_challenge ───────────▶│                           │
       │  (a Town message, signed by Town's auth layer)            │
   3.  │                              ├── verify_credential ──────▶│
       │                              │   credential + its nonce   │
   4.  │                              ├── /challenge ─▶ the agent's own service
       │                              │   signs Town's nonce        │
   5.  │                              ├── check_identity_proof ───▶│
       │                              │   AgentSchool's verdict     │
   6.  │                              ├─ Town verifies for itself   │
   7. negotiate, escrow, pay ◀────────┤  (verified holders only)    │
   8.  │                              ├── report_task_outcome ────▶│
```

One run calls every AgentSchool tool a hiring agent needs:
`search_certified_agents` to find who is certified,
`verify_credential` to fetch one agent's credential, `check_identity_proof` to
have AgentSchool check the signature that agent produced at its own
`/challenge` endpoint, and `report_task_outcome` to say afterwards how the job
went. The credential and the signature travel as ordinary Town messages, so
Town's transport, communication, auth, registry, negotiation and payment layers
all take part, and the whole run lands in one Town evidence bundle.

**Town and AgentSchool each check a different part of the proof.** Town checks
the issuer signature against the JWKS,
the validity window, revocation against the signed revocation list, and the
holder's signature over a nonce **Town minted**, which Town then refuses to
accept twice. AgentSchool separately judges its own nonce, since it issued it
and holds the used-nonce store, and its verdict is recorded as its own claim.
When both readers reach the same verdict on one credential, that agreement is
what this check offers as evidence of compatibility. A disagreement is recorded
as `credential_verdicts_disagreed` and fails the `verdicts_agreed` stage.

## The credentials in the fixture were earned

Every credential in the fixture was earned. Each agent ran through AgentSchool
as a headless Claude Code process: it registered, enrolled, took an untrained
pre-test, read the lessons, practiced with graded feedback, and sat the
held-out exam, which an Opus judge scored. A credential was issued only to an
agent whose exam cleared AgentSchool's pass mark of 95 with every dimension
above its floor.

```bash
python -m agents.train_all --level 1   # in the AgentSchool checkout
```

| Agent | Model | Pre-test | Best practice | Exam | Credential |
|---|---|---|---|---|---|
| atlas | Sonnet 5.5 | 100.0 | 100.0 | 100.0 | Level 1 |
| brisk | Sonnet 5.5 | 94.5 | 99.0 | 100.0 | Level 1 |
| flash | Haiku 4.5 | 44.1 | 90.1 | 87.0 | none |

The run took 7 minutes and about $1.17 of judge calls. Two outcomes matter for
this check. Brisk started below the pass mark at 94.5 and reached 100.0 after
the lessons, so its credential reflects work it did rather than a model it
happened to be. Flash failed both of its exam attempts, at 91.1 and then 87.0,
so the uncertified seller in these scenarios is an agent that genuinely could
not pass, not a placeholder.

Brisk's credential was then revoked with `agentschool revoke <jti>`, an
ordinary operator action, which is where the revoked case comes from. Its
holder key still signs Town's nonce correctly and the signature still
verifies; the signed revocation list is the only reason it is refused.

## Reproduce

### Offline, with no AgentSchool

The committed fixture holds credentials, a JWKS, a signed revocation list and a
holder proof captured from a real AgentSchool run, so this needs no AgentSchool,
no model account and no network.

```bash
python -m pytest tests/test_agentschool_integration.py -v
```

```bash
nandatown run integrations/agentschool/scenarios/credential_gating.yaml
```

```bash
nandatown run integrations/agentschool/scenarios/credential_gating_ungated_control.yaml
```

```bash
nandatown run integrations/agentschool/scenarios/revoked_credential_fail_closed.yaml
```

Replaying a bundle needs the validator, which lives in `plugin.py`:

```bash
nandatown verify runs/<id> --plugin integrations/agentschool/plugin.py
```

### Live, against a running AgentSchool

Follow AgentSchool's own README to install it, then `make server` and
`python -m agents.service --profile atlas --port 8101`. Register a relying
party with AgentSchool to get a bearer token, and write a roster mapping each
agent's name to its AgentSchool id.

```bash
export NANDATOWN_AGENTSCHOOL_MODE=live
export NANDATOWN_AGENTSCHOOL_URL=http://127.0.0.1:8765
export NANDATOWN_AGENTSCHOOL_TOKEN=<relying party bearer token>
export NANDATOWN_AGENTSCHOOL_ROSTER=/path/to/roster.json
nandatown run integrations/agentschool/scenarios/credential_gating.yaml
```

To refresh the fixture from a live server:

```bash
python integrations/agentschool/tools/capture_fixture.py --server http://127.0.0.1:8765 --token "$NANDATOWN_AGENTSCHOOL_TOKEN" --roster "$NANDATOWN_AGENTSCHOOL_ROSTER" --agentschool-commit "$(git -C /path/to/agentschool rev-parse HEAD)" --out integrations/agentschool/fixtures/directory.json
```

## Results, expected against observed

Three sellers advertise `sell.widget` in Town's registry. One holds a current
AgentSchool credential, one holds a credential AgentSchool revoked, and one
holds none and undercuts both others on price.

### The main workflow, live

Expected: the verified holder is hired and paid, the other two are refused, and
both verifiers agree.

Observed: every stage passed.

| Stage | Observed | Note |
|---|---|---|
| credential_source_recorded | Passed | mode live, issuer `as-D_vV6jcrJs` |
| credential_verified | Passed | `seller-certified`, level 1, exam 100.0 |
| holder_key_proved | Passed | holder signature verified |
| holder_proof_fresh | Passed | nonce was Town's own, within its window, unspent |
| revoked_refused | Passed | `seller-revoked` rejected: revoked |
| uncertified_refused | Passed | `seller-uncertified` rejected: no_credential |
| unverified_got_no_work | Passed | neither refused seller received an order or payment |
| certified_trade_completed | Passed | escrow released to the verified holder |
| verdicts_agreed | Passed | 2 presentations judged the same by Town and AgentSchool |
| outcome_reported | Passed | outcome accepted by AgentSchool |
| ledger_conserved | Passed | 10,000 cents conserved |
| privacy | Passed | `budget_cents` never left the run |

### The same workflow, offline

Expected: the same verdicts on the credential checks, and no claim about what a
captured proof cannot show.

Observed: PASSED, with two stages reported as not tested rather than passed.

- `holder_proof_fresh` is **not tested**, because the capture replays a nonce
  minted earlier, which shows the holder key signed that nonce once and cannot
  show the agent is answering now.
- `outcome_reported` is **not tested**, because a fixture has nobody to report
  to.

`verdicts_agreed` does pass offline, because the capture records AgentSchool's
own verdict on each presentation alongside the credential, so the comparison
replays from the fixture.

The captured revocation list is older than the five-minute freshness limit it
declares, so the offline run reads it with that limit lifted and records the
list's age. A missing, stale or wrongly signed list fails `not_revoked` rather
than being read as an absence of revocations.

### The control, which fails on purpose

The same three sellers, the same challenges, the same verdicts, with `gate:
false` so the verdicts are recorded and not acted on. One setting differs from
the main workflow.

Expected: the uncertified seller undercuts on price and wins the work.

Observed: FAILED at `unverified_got_no_work`, with the note "no unverified
seller may receive a purchase order or payment; seller-uncertified did".
`certified_trade_completed` also failed, because the certified holder got no
trade. `credential_verified`, `revoked_refused` and `uncertified_refused` still
passed, which is the point: the checks reached the right answers and the gate
is what kept the work away from an agent that failed them.

## Failure and boundary cases

### A revoked credential, when no other credential is on offer

`revoked_credential_fail_closed.yaml`. The only seller advertising the
capability presents a credential AgentSchool has withdrawn.

Expected: nothing is delegated, and the refusal is recorded.

Observed: PASSED. `revoked_refused`, `no_work_delegated`, `refusal_recorded`
and `nothing_verified` all passed. The buyer recorded
`buyer_gave_up{reason: no_verified_credential}` and issued no order and no
payment. The signature on the revoked credential still verifies; the signed
revocation list is what refuses it.

### Town's Path mode cannot drive AgentSchool's agent service

AgentSchool's candidate service publishes an agent card and answers
`/challenge` and `/tasks`. Town's Path mode expects an A2A card with a JSON-RPC
`message/send` endpoint.

```bash
nandatown test-agent --url http://127.0.0.1:8101
```

Observed: FAILED at `protocol_invocation` with `a2a_http_status_404`.

| Stage | Observed |
|---|---|
| resolution | Passed |
| agent_card_retrieval | Passed, card `sha256:0dc13af137a02a9d` |
| descriptor_consistency | Not tested, no pinned digest |
| protocol_invocation | **Failed, `a2a_http_status_404`** |
| semantic_result | Not tested, not reached |
| duplicate_request | Not tested, not reached |

The card is retrievable and digestible, so discovery and digest pinning
already work against AgentSchool as it stands. What breaks is the message
transport: the card declares `protocol: a2a-lite/0.1` with `endpoints.tasks`
and serves no JSON-RPC method. Testing AgentSchool's work loop through Path would
need an adapter mapping `message/send` onto `/tasks`, which this PR does not
build. This result says nothing about whether AgentSchool's agents do their job
well, only that Town's A2A driver cannot reach them.

### A seeded sandbox and a service that refuses repeats disagree about task identity

Town's Lab is seeded, so the same scenario mints the same order id on every
run, by design. AgentSchool refuses a `report_task_outcome` whose task id it
has already accepted from the same reporter, also by design. Together those
two correct behaviors mean the second live run of a scenario cannot report its
outcome.

Observed on the second live run, before the fix:

```
outcome_reported  Failed
  accepted: false, weight_applied: 0.0, reason: "duplicate_task"
```

The integration now namespaces the id it sends outward with Town's run id,
which is unique per run, and leaves the order id inside Town deterministic so
the bundle stays reproducible. Both ids are recorded on the
`outcome_reported` event.

AgentSchool also caps accepted reports at three per reporter, per agent, per
day. Past that it answers `daily_cap`, which no amount of retrying will get
around, so the validator reports that refusal as missing evidence with the
reason named, rather than as a broken feedback loop. More than three live runs
a day need another registered relying party.

### Quality runs from 0 to 100, so 0.9 is accepted as a bad score

`report_task_outcome` takes `quality` on a 0 to 100 scale. Writing `0.9` in a
Town scenario, meaning nine tenths, is accepted by AgentSchool as a quality of
0.9 out of 100, which is nearly the worst possible score rather than a good
one. Nothing rejects it, and nothing in either system notices. The scenarios
now send 90, and the plugin's default is 90.

### A correctness gap this integration found in Town

A Lab scenario may put its validator in a `plugin_files` module, as this one
does. `nandatown verify` did not load those files, so in a fresh process the
validator was absent, the replay recorded "no validator registered", and the
only report was `evaluator replay mismatch: result.json does not match a fresh
deterministic evaluation`. That reads like corrupted evidence, and it would
affect every contributed scenario, not only this one.

Observed before the change:

```
problem: evaluator replay mismatch: result.json does not match a fresh deterministic evaluation
```

Observed after it, without `--plugin`:

```
problem: evaluator replay mismatch: result.json does not match a fresh
deterministic evaluation; the scenario's validator
'agentschool_credential_gating' comes from ['.../plugin.py'], which this verify
did not load. Re-run with --plugin for each of those files to replay it
```

And with `--plugin`, the bundle verifies. The repair is in `src/nandatown/bundle.py`
and `src/nandatown/cli.py`: `verify` takes `--plugin FILE`, as `run` and
`compare` already do, and a mismatch names the validator and the files it came
from. Regressions are in `tests/test_agentschool_integration.py`.

## What remains untested

- **Whether the exam predicts performance on delegated work.** The credentials
  in the fixture were earned: each agent enrolled, read lessons, practiced with
  feedback and sat the held-out exam, and only the two that cleared the pass
  mark were certified. What that score means is a separate question, and this
  check does not answer it. AgentSchool's own earlier results found an agent
  certified at 88.0 averaging 62.4 on downstream jobs.
- **AgentSchool's `/tasks` work loop.** The delegated work in these scenarios
  is Town's synthetic widget order, not a slide deck. Running the real task
  needs a Claude Code login and a judge key.
- **A2A conformance**, for the reason above.
- **An agent that publishes no reachable service.** When Town cannot reach
  `/challenge` it records `signer: unreachable` rather than filling in a
  signature of its own, so the credential is refused for want of a proof. That
  path is exercised, but no agent in these scenarios is permanently
  unreachable.
- **Whether a jti the revocation list has never named was ever issued.** A
  signed list cannot distinguish an unknown credential from an unrevoked one,
  so Town reads absence from the list as not revoked. AgentSchool's own server
  answers `unknown_credential` for a jti it does not hold.
- **More than one scenario.** Seeds change the numbers, not the story.

The synthetic credentials in `tests/test_agentschool_integration.py` are Town's
own test data, signed with a throwaway key so each check can be driven to
failure. They are not an AgentSchool capture and establish nothing about
AgentSchool.

## Files

| File | What it is |
|---|---|
| `credential_verify.py` | Town's own reader of AgentSchool's credential format: Ed25519 JWS against a JWKS, the validity window, the signed revocation list and its freshness, holder proofs, and a ledger of nonces Town issued. No AgentSchool imports and no new dependency, since `cryptography` is already one of Town's. |
| `credential_source.py` | Where credentials come from: the captured fixture, or a live AgentSchool server plus each agent's own service. Each source states what it cannot establish. |
| `plugin.py` | The `agentschool.v1` trust plugin, the `certified_seller` and `gating_buyer` roles, and the two validators. |
| `scenarios/*.yaml` | The main workflow, the ungated control, and the revoked-credential boundary case. |
| `fixtures/directory.json` | The capture, with its provenance, the AgentSchool commit, and its own list of limitations. |
| `tools/capture_fixture.py` | Rebuilds that capture from a live AgentSchool server. |
| `../../tests/test_agentschool_integration.py` | 49 tests: the verifier on every failure, the source, the three scenarios offline, and the `verify --plugin` regressions. |

## Versions

- Town base commit `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`, nandatown 0.2.0,
  Python 3.12.12
- Lab evaluator `lab-0.2.6`
- Path profile `a2a-capability-fulfillment@0.3`, evaluator `path-0.3`
- AgentSchool commit `393c4b7f4991da6dc10fb07efe020126454886eb`, issuer key
  `as-D_vV6jcrJs`
- Skill `data-driven-slides` 2.0.0, exam `harbor-pine-slide-design-v2`, Level 1
- Observed agent card digest `sha256:0dc13af137a02a9d`

Lab bundles are logically reproducible rather than byte-identical, because the
run id and wall-clock metadata differ per run, so the reproducible evidence is
the verdict and the stage table. Verify offline with `nandatown verify
<bundle> --plugin integrations/agentschool/plugin.py`; repeating the live run
calls AgentSchool again and can change with its state.
