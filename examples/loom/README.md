# LOOM in Nanda Town

LOOM decides how much of a person's health information an agent that works
for them may receive, for how long, and with how much friction. Depth is set
by the task (L0 raw signal, L1 readings, L2 patterns, L3 constraints),
every grant expires, overreach is blocked, and a publisher's credit changes
friction but never depth. This example connects the LOOM portal, which runs
as its own HTTP service, to the Town Lab and checks the whole request flow
against Town's shared registry, auth, transport, trust, privacy and evidence
layers.

Product concept: https://claude.ai/artifact/8g6LoMQTeJ1iGt9NgZpx1a

## How it connects

```
Town Lab (deterministic, logical time: 1 unit = 1 day)

  loom_requester x5 --registry lookup "health.context"--> loom_gateway --HTTP--> LOOM portal
        |  signed envelopes (auth layer)                     |  overreach -> api.rate(bad)
        |                                                    |  -> data-facts receipt + trust layer
        '-- health data in replies is redacted from the bundle (privacy: redact_fields [data])
```

- `loom_plugin.py` defines two roles and two validators. `loom_gateway`
  publishes `health.context` in the registry and forwards each request to
  the portal, using the Town envelope's sender as the requester identity and
  the Lab clock as the request time. If the portal cannot be reached, it
  denies the request (fail closed). `loom_requester` finds the gateway
  through the registry and runs scripted steps on logical days.
- The decisions come from the external portal, not from this file.
  Portal source: https://github.com/jasonpipi/loom-portal (v0.1.1, commit `480558e`,
  policy `loom-policy-0.1.1`). It uses only the standard library.
- Each run uses its own portal namespace (`/v1/<run_id>/...`), so runs
  never share state.

## Reproduce

- Nanda Town upstream base: `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`
- LOOM integration snapshot from PR #329: `a022a831db4c4b9a4a862f652522e255e268dc0f`
- LOOM portal: `v0.1.1`, commit `480558ee41af23a3f0937cfa207cff1e0ed43d32` (policy `loom-policy-0.1.1`)
- Python 3.12, Town evaluator `lab-0.2.6`

Run from an empty working directory. Fetch the PR ref before checking out the
pinned integration snapshot, which contains `examples/loom/`.

```bash
git clone https://github.com/projnanda/nandatown
cd nandatown
git fetch origin refs/pull/329/head
git checkout --detach a022a831db4c4b9a4a862f652522e255e268dc0f
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pip install "git+https://github.com/jasonpipi/loom-portal@480558ee41af23a3f0937cfa207cff1e0ed43d32"
python -m pytest -q examples/loom
```

The test command starts a local portal, runs both scenarios, and verifies the
evidence bundles with the LOOM validators loaded.

To run the scenarios and verify a bundle manually:

```bash
loom-portal --port 8787 --seed-credit cambridge-clinic=85 &
LOOM_URL=http://127.0.0.1:8787 nandatown run examples/loom/loom_health_access.yaml --out runs/loom
nandatown run examples/loom/loom_health_access_portal_down.yaml --out runs/loom-down
# Set BUNDLE to the directory printed for the main scenario.
BUNDLE=runs/loom/REPLACE_WITH_RUN_ID
python examples/loom/verify_loom.py "$BUNDLE"
```

`--seed-credit cambridge-clinic=85` starts the clinic in the high-credit
band so its grant auto-renews. Every other publisher starts at 60.

## What the main scenario tests

One synthetic glucose record, six agents, eight steps plus one retry. The first
`context_request` on the wire is duplicated by Town's transport fault
injection.

| Day | Agent (publisher) | Asks for | Expected | Observed |
|---|---|---|---|---|
| 0 | grocery-agent (freshcart) | shopping list, L3 constraints | granted L3, single use, confirm on first request | granted L3, `first_request`, credit 60 → 61 |
| 0 | same message, duplicated | | replay, credit unchanged, no second copy of the single-use data | replay, credit stays 61, `data_withheld: expired`, no data |
| 0 | clinic-agent (cambridge-clinic) | clinical visit, L2 patterns + alerts | granted L2, 14 days, auto-renew | granted L2, 14 days, `auto_renew: true` |
| 0 | adco-marketing (adco) | targeted ads, L1 readings | denied by default, no penalty | denied `no_task_basis`, credit 60 → 60 |
| 1 | mealplan-agent (plateplan) | meal plan, L2 patterns + constraints | granted L2, 7 days | granted L2, 7 days |
| 2 | grocery-agent-injected (freshcart) | shopping list, **L1 5-minute readings** | blocked, publisher penalized, Town reputation down | blocked `overreach`, credit 61 → 41, `reputation_updated` outcome bad with receipt `fact-1` |
| 3 | grocery-agent (freshcart) | shopping list again | still L3, now confirm every time | granted L3, `every_time`, credit 41 → 42 |
| 5 | mealplan-agent | fetch under its grant | granted | granted L2 |
| 9 | mealplan-agent | fetch after expiry | denied, expired | denied `expired`, credit 61 → 56 |
| 10 | mealplan-agent | retry of the day-5 fetch (same `request_id`) | replay, credit unchanged, `data_withheld: expired`, no data | replay, credit stays 61, `data_withheld: expired`, no data |

Stages (real output, run `sim-2094eaa8c81d`):

```
Verdict:   PASSED
  loom_reachable            Passed   gateway reached LOOM 0.1.1 (loom-policy-0.1.1)
  discovery                 Passed   5 of 5 requesters found health.context via the town registry
  decisions_match_policy    Passed   all 8 requests and fetches got the declared outcome
  minimum_disclosure        Passed   every grant delivered exactly its granted fields within its tier; denied requests and withheld replays carried no data
  overreach_contained       Passed   overreach was blocked, the publisher lost LOOM credit, and a signed receipt lowered the requester's town reputation
  expiry_enforced           Passed   a grant that served fetches inside its window was refused after expiry
  friction_not_depth        Passed   after a credit drop the same task kept its tier; only the confirmation requirement changed
  idempotent_retry          Passed   a duplicated request was answered as a replay; credit moved once
  replay_rechecks_validity  Passed   a retry after expiry kept its original decision and credit but carried no data
  ledger_conserved          Passed   complete run recorded no money moved
  privacy                   Passed   fields ['data'] never left the run
```

## Failure and boundary cases

1. **Injected agent overreach** (day 2, above). The block shows up in two
   places: LOOM's publisher credit, and Town's trust layer through a signed
   data-facts receipt.
2. **Portal down.** `loom_health_access_portal_down.yaml` runs the same agents
   against a closed port:

   ```
   Verdict:   PASSED
     loom_unreachable_observed  Passed   the gateway recorded the outage
     fail_closed                Passed   every request was denied with no data while LOOM was unreachable
     ledger_conserved           Passed
     privacy                    Passed
   ```
3. **Duplicate delivery (found on the first run).** The first run failed
   `idempotent_retry`: the copy reached the gateway 0.1 days later, the
   gateway stamped that later time into the request, and the portal treated
   it as different content (`conflict`, HTTP 409) instead of a replay. The
   portal now leaves the receive time out of the idempotency digest, with a
   regression test, and the rerun passes. LOOM's own unit tests had missed
   this, because they always resent identical bodies.
4. **Replay after expiry (found in review).** Portal v0.1.0 answered a
   replay from its cache, and the cache held the data delivered the first
   time. A retry sent after the grant expired therefore got the data
   again, and `minimum_disclosure` only looked for extra fields, so it
   would also have passed a grant that delivered nothing. v0.1.1 keeps
   the two questions apart: a replay still returns the original decision
   and never moves credit (`idempotent_retry`), but data is re-sent only
   while the grant is valid at the replay's time; otherwise the reply says
   `data_withheld: expired` (`replay_rechecks_validity`).
   `minimum_disclosure` now requires each grant to deliver exactly its
   granted fields. Both checks were run against the faulty behavior:
   with portal v0.1.0 the main scenario is `FAILED` at
   `replay_rechecks_validity` (`grocery-agent-0: replay after expiry
   carried ['constraints']; mealplan-agent-1: ... ['constraints',
   'glucose_patterns']`), and with a gateway patched to drop the
   clinic's data it is `FAILED` at `minimum_disclosure`
   (`clinic-agent-0: received [], granted ['alerts', 'glucose_patterns']`).
5. **The validators can fail.** Running the main scenario with no portal
   gives `FAILED` at `loom_reachable`, with `decisions_match_policy` and
   `idempotent_retry` also failed.

## Compatibility findings

- **`nandatown verify` cannot replay custom Lab validators.** It runs the
  evaluator in a fresh process without importing the scenario's
  `plugin_files`, so `nandatown verify` fails on these bundles with
  `evaluator replay mismatch`. `nandatown receipt` then refuses them
  (`the bundle does not verify`). Built-in scenarios such as
  `capability_spoofing` verify fine. `verify_loom.py` loads `loom_plugin.py`
  first and calls the same `verify_bundle`; with it, both bundles verify, and
  a tampered copy is rejected on hash mismatch. A `--plugin` option on
  `verify` (as `run` already has) would close the gap. That fix is not part
  of this PR.
- **Town reputation is per agent; LOOM credit is per publisher.** The
  overreach lowered Town reputation only for `grocery-agent-injected`, while
  LOOM lowered credit for all of `freshcart`. Town has no notion of a
  publisher.
- **The publisher is self-declared.** Town authenticates the sender, and the
  gateway uses the envelope's sender, not the body. The `publisher` field is
  still just a claim in the body. An agent could name a different publisher
  to dodge a low score. A registry card with a verified owner would be the
  right anchor.

## Not tested

- The requesters are scripted Town agents, not independently developed
  agents. This shows the integration path; it is not evidence of outside
  adoption.
- The record is synthetic (one person, 14 days of glucose). No device or
  real personal data is involved.
- Confirmation is only the `confirmation` field LOOM returns. No person is
  simulated, and auto-renewal is reported but never exercised.
- Medium credit does not shorten validity yet (v0.1 changes only
  confirmation in that band).
- The doctor's alerts are delivered at grant time. There are no pushed
  updates during the window.
- LOOM sees only requests. What an agent does with data after delivery is
  invisible, and expiry cannot recall it.
- Overreach is a static check of the task's tier and fields. There is no
  semantic purpose check yet.
- Credit numbers are a draft and have not been calibrated.
