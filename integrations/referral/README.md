# Referral in Town: pay only for checked work, trust only checked receipts

[Referral](https://github.com/louis1mx/referral) keeps verified track records
for agents that hire agents. Before a job, the worker locks a forecast of
success, cost and time with Referral's Notary. After it delivers, Referral's
Checker verifies every fact against a permitted, dated corpus: the cited
document must exist and be about that company, the quote must appear in it
verbatim, and the value must appear in the quote. The buyer gets `accept` or
`reject_and_rematch`. Referral runs as its own MCP server and is not copied
into this repository.

This integration puts Referral behind Town's **trust layer** and runs a whole
hire through Town's shared infrastructure: registry discovery, signed
messages, escrow, receipts, reputation and the registry ranking that every
other agent in the town reads.

| File | What it is |
| --- | --- |
| `plugin.py` | trust plugin `referral.v1`; roles `referral_service`, `research_worker`, `research_buyer`; validators `referral_fabricated_citation`, `referral_unlocked_forecast`, `referral_fail_closed` |
| `mcp_stdio.py` | a small synchronous MCP stdio client (JSON-RPC 2.0, standard library only) |
| `scenarios/` | the four runs below |
| `evidence/` | the recorded bundle of each run (offline-verifiable) |
| `verify.py` | `nandatown verify` with this plugin loaded first (see Limitations) |

Tests: `tests/test_referral_integration.py`.

## How the sandbox connects to Referral

```
 Town Lab (this repo)                                   Referral (separate repo, separate process)
 ┌──────────────────────────────────────────┐   MCP     ┌──────────────────────────────┐
 │ referral_service  ── starts + handshakes ─┼──stdio──▶ │ referral serve --history none│
 │ research_worker x2 ─ register, read docs, │           │  Notary (hash-chained log)   │
 │                      lock forecast, deliver│          │  Matcher, Checker, Issuer    │
 │ research_buyer ───── match, hire, record,  │          └──────────────────────────────┘
 │                      credential             │
 │ Town layers: registry, auth, transport,    │
 │   payments (escrow), data_facts, trust=referral.v1
 └──────────────────────────────────────────┘
```

- `referral_service` is a declared Town participant that starts the Referral
  server as a child process and opens one MCP session for the run. The
  command comes from the participant's `config.command`, else
  `NANDATOWN_REFERRAL_CMD`, else `referral serve --history none`.
  `--history none` starts Referral with no simulated workers, so it can match
  only this town's agents.
- Each Town agent registers its own Referral identity and calls Referral's
  tools itself (15 tools listed at the handshake; the workflow uses 11).
  Agent keys stay in agent memory: intents record only the tool name, and the
  scenarios declare `redact_fields: [key]`.
- `referral.v1` is Town's trust layer: a trade receipt moves a score only if
  the trust layer relayed a Referral verdict for that buyer and worker, and
  the verdict, not the buyer's claim, sets the sign (+1 passed, -1 failed,
  0 when no checked job backs the receipt). Town's registry ranks
  `lookup` results by this score.

## What a run does

1. Two research workers publish `research.competitor_facts` cards in Town's
   registry. `worker-cheap` (40 cents) fabricates: it never reads the corpus
   and cites real document ids with quotes that are not in them.
   `worker-honest` (90 cents) reads each document through Referral and
   quotes it verbatim.
2. `buyer-1` looks the capability up, asks both for quotes over Town
   messages, and asks Referral's Matcher for a worker for a four-fact task
   about Acme Robotics' competitors (medium band: one fact needs the 2026
   update, not the stale 2024 profile).
3. Referral picks the cheaper worker (both are unproven, so price decides) as
   a bounded trial. The buyer hires it in Referral, holds the price in
   Town's escrow, and sends the order.
4. The worker locks its forecast, then delivers. Referral's Checker fails all
   four facts: `quote does not appear in the cited document`.
5. The buyer reads the record (`reject_and_rematch`), is refunded from
   escrow, files a receipt, and rematches with the fabricator excluded.
6. The honest worker passes all four facts. The buyer releases escrow, files
   a receipt, fetches the worker's signed Referral credential and verifies it
   twice: offline in Town (Ed25519 over canonical JSON, Referral's published
   key) and with Referral's `verify_credential`.
7. The buyer reads the registry ranking that any other Town agent would get.

The control run has the same agents and differs only in `layers.trust`
(`test_the_two_runs_differ_only_in_the_trust_layer`). With `reputation.v1`
nothing routes work through Referral, so the buyer behaves like Town's
reference buyer: it takes the cheapest quote, accepts the delivery on
arrival and rates it good.

## Reproduce

Tested with Nanda Town base commit `6f9a7c0` (`main`, 2026-11-11) plus this
PR, nandatown 0.2.0, evaluator `lab-0.2.6`, Python 3.13.18 (Linux); Referral
commit [`d41126c`](https://github.com/louis1mx/referral/commit/d41126c3091ebe71c87054b5eac0a94bddbbe108)
(package `referral` 0.1.0, MCP SDK from its `requirements-lock.txt`).

```bash
# 1. Referral, in its own checkout and virtualenv
git clone https://github.com/louis1mx/referral.git && cd referral
git checkout d41126c3091ebe71c87054b5eac0a94bddbbe108
python3 -m venv .venv && .venv/bin/pip install -r requirements-lock.txt -e .
export NANDATOWN_REFERRAL_CMD="$PWD/.venv/bin/referral serve --history none"
cd ..

# 2. Town, from this branch
cd nandatown
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'
export NANDATOWN_HOME="$(mktemp -d)"

python -m pytest -q tests/test_referral_integration.py          # 13 passed

nandatown run integrations/referral/scenarios/referral_fabricated_citation.yaml
nandatown run integrations/referral/scenarios/referral_fabricated_citation_reputation_control.yaml
nandatown run integrations/referral/scenarios/referral_unlocked_forecast.yaml
nandatown run integrations/referral/scenarios/referral_unreachable_fail_closed.yaml
nandatown compare integrations/referral/scenarios/referral_fabricated_citation.yaml --swap trust=reputation.v1

python integrations/referral/verify.py runs/<id>                 # offline replay
python integrations/referral/verify.py integrations/referral/evidence/referral_fabricated_citation
```

No model, API key or network is needed: Referral's Checker runs its rule
layer, and the workers are scripted. Without `NANDATOWN_REFERRAL_CMD` (and no
`referral` on `PATH`) the live tests skip; the fail-closed run, the MCP client
tests and the offline verification of `evidence/` still run.

## Results

| Run | Expected | Observed |
| --- | --- | --- |
| `referral_fabricated_citation` | fabricator caught and refunded, rematch to the honest worker, paid only for checked work, registry ranks honest first | **PASSED** 9/9 stages. `job-0001` (worker-cheap) failed 4/4 facts, `quote does not appear in the cited document`; 40 cents refunded; `job-0002` (worker-honest) passed 4/4; 90 cents released; scores honest +1, cheap -1; credential verified in Town and by Referral |
| `..._reputation_control` | `reputation.v1` credits the fabricator | **FAILED on purpose**: 40 cents released to worker-cheap for fabricated facts, `reputation_updated` +1 with no check, registry ranks worker-cheap first; `fabrication_rejected` and `reputation_matches_checks` fail, Referral stages not enough evidence |
| `referral_unlocked_forecast` (boundary) | delivery before a locked forecast refused and notarized; same work passes after locking | **PASSED** 7/7: `lock a forecast with submit_prediction before delivering`, then passed 4/4 and paid; the job's notary log holds the refused attempt |
| `referral_unreachable_fail_closed` (failure) | Referral exits before the handshake: outage recorded, nothing paid or credited | **PASSED** 6/6: `connect failed: initialize: server exited`; buyer stops with `not registered with Referral`; no escrow, no score |

`nandatown compare ... --swap trust=reputation.v1` reports baseline PASSED,
swapped FAILED, with `credential_verified`, `fabrication_rejected`,
`forecast_locked_before_delivery`, `rematched_and_accepted` and
`reputation_matches_checks` differing. Two runs of the main scenario produce
identical traces apart from the run id (`test_runs_are_deterministic`).

## What this shows about compatibility

- Referral's MCP server works as a participant in Town without changes to
  its tool surface: handshake (protocol `2025-06-18`), registration, the
  Matcher, the Notary's forecast lock, the Checker and the credential issuer
  are all exercised over real MCP stdio by Town agents.
- Referral's verdicts compose with Town's escrow (refund on reject, release
  on accept, money conserved) and with Town's receipts and reputation.
- Swapping one layer is enough: the same buyer and workers, with Town's
  default trust, reward fabrication; with `referral.v1`, Town's shared
  reputation and registry ranking follow checked work.

## Limitations and what remains untested

- **Scripted agents.** Workers and buyer are Town fixtures. The Claude Code
  buyer and worker from Referral's HW6 MVP (`referral mvp`) are not run here,
  so no LLM agent's behaviour, cost or latency is measured.
- **Rule layer only.** Referral's semantic layer (`--semantic-checker`,
  which needs the Claude CLI) is off; stale-but-cited and wrong-attribute
  facts are therefore not exercised, only fabricated quotes.
- **One corpus, one domain.** The checks hold for Referral's fictional
  competitor corpus; real documents and other domains are not tested.
- **Tier movement.** One checked job per worker leaves both at `trial`; tier
  promotion, forecast calibration and subcontract failure attribution are not
  exercised in Town.
- **Referral release.** Town records the server name, protocol version and
  tool list, not an immutable Referral release; the commit is pinned above.
  Referral uses a wall clock and draws its hidden check slot at random; the
  outcome does not depend on either here.
- **Trust layer scope.** `referral.v1` trusts a verdict it relayed itself in
  this run. It does not import verdicts from another run or another town,
  and a receipt from a buyer that never used Referral counts 0.
- **`nandatown verify`** on current main does not load `plugin_files`, so a
  fresh process reports a replay mismatch; `verify.py` loads the plugin and
  then runs Town's verify unchanged.
- **Full suite.** `python -m pytest -q` passes except two read-only-home
  tests in `tests/test_url_credentials.py` that fail when run as root
  (permissions are not enforced for root); they fail the same way without
  this PR.
- Town's own guide applies: this is a course-project integration with
  scripted participants, not independently adopted compatibility.
