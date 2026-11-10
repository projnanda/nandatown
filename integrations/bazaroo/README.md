# Bazaroo integration for Nanda Town

Bazaroo is an AI agent marketplace. This integration adds a Town `registry`
plugin, `bazaroo.v1`, so Town agents register in Bazaroo and discover each
other through it. **Bazaroo makes the accept/reject decision**; Town passes its
answer through unchanged.

## Files

| File | Purpose |
|---|---|
| `registry_bazaroo_v1.py` | The `registry` plugin (`bazaroo.v1`), a subclass of Town's `TownIndex` |
| `bazaroo_client.py` | Standard-library HTTP client for Bazaroo's register/search API |
| `mock_bazaroo.py` | Offline mock with the same contract (no network, no keys) |
| `test_bazaroo_client.py` | Client tests (offline) + one opt-in live test |
| `test_registry_bazaroo.py` | Plugin tests (offline) |
| `validators_bazaroo.py` | Town validators for the two scenarios |
| `test_validators_bazaroo.py` | Validator tests (offline) |
| `bazaroo-discovery.yaml` | Scenario: seller and buyer find each other through Bazaroo |
| `bazaroo-name-taken.yaml` | Failure scenario: Bazaroo refuses a name another key owns |
| `verify_bazaroo.py` | Runs Town's `verify` with the Bazaroo plugin and validators loaded |

## Bazaroo contract (verified live)

- `POST /api/public/agents/register`: Bearer key, `{name, capabilities[], description}`
  - `201 {agent_id}` new name, or same key re-registering its own name
  - `409 {"error":"name_taken"}` name owned by another key, or a reserved built-in name
  - `401` bad or missing key, `400` bad body
- `GET /api/public/agents/search?q=...`: Bearer key, `200 {agents:[...]}`

Each agent uses its own API key; the key is its identity.

## How the plugin maps Bazaroo's answers

| Bazaroo answer | Plugin does |
|---|---|
| 201 | accepts; emits `card_registered` with `authority: bazaroo` |
| 409 `name_taken` | **refuses**; emits `card_publish_refused` (reason `bazaroo: name_taken`); the existing listing is untouched |
| 401 / 400 | refuses; reason recorded |
| timeout / network / 5xx | does **not** store the card; emits `card_publish_refused` with `outcome: unknown` |

Lookup returns `[]` when Bazaroo cannot be reached and emits `registry_lookup_failed`
(`outcome: unknown`), so "nobody found" and "could not ask" stay distinguishable.

Town's own signature check still runs and is recorded as `town_signature_verified`,
but it does not gate the decision; Bazaroo's API key is the authority for ownership.

## Keys (never commit these)

- One key per publishing agent, read from `BAZAROO_KEY_<PUBLISHER>`
  (upper-cased, non-alphanumerics become `_`): `seller-a` -> `BAZAROO_KEY_SELLER_A`.
- Lookups use `BAZAROO_KEY_LOOKUP`, or the last successful publisher's key.
- Optional: `BAZAROO_BASE_URL` (default `https://bazaaro.ryan-teo.com`), `BAZAROO_TIMEOUT` (seconds, default 10).

PowerShell: `$env:BAZAROO_KEY_SELLER_A = "..."`   bash: `export BAZAROO_KEY_SELLER_A=...`

## Run the tests

From the repo root, after `python -m pip install -e ".[dev]"`:

    python -m unittest discover -s integrations/bazaroo -v

Offline: no network, no keys. The one live test is skipped unless
`BAZAROO_KEY_AGENT1` and `BAZAROO_KEY_AGENT2` are set (it uses a unique name per run).

## Run the end-to-end scenarios

Set one key per role (PowerShell shown; use `export` on bash). Use three different keys
that Bazaroo accepts, with no quote marks or spaces inside the value:

    $env:BAZAROO_KEY_HW8_SELLER = "<key 1>"
    $env:BAZAROO_KEY_HW8_BUYER  = "<key 2>"
    $env:BAZAROO_KEY_LOOKUP     = "<key 3>"

**1. Success path** (seller and buyer register in Bazaroo; the buyer finds the seller there):

    nandatown run integrations/bazaroo/bazaroo-discovery.yaml

Expected verdict PASSED with stages `bazaroo_registration` and `bazaroo_discovery`.

**2. Failure / boundary case.** Bazaroo has no delete, so after run 1 the name `hw8-seller`
belongs to key 1 for good. Run the second scenario with a *different* key for the seller:

    $env:BAZAROO_KEY_HW8_SELLER = "<key 2>"
    nandatown run integrations/bazaroo/bazaroo-name-taken.yaml

Bazaroo answers 409 `name_taken`, the plugin refuses the impostor, and the original
listing is still found. Expected verdict PASSED with stages `bazaroo_refuses_taken_name`
and `real_listing_survives`. (This run passes because Town honored Bazaroo's refusal.)

Then replay and verify each bundle:

    nandatown replay runs/<id> --kind bazaroo_name_taken
    python integrations/bazaroo/verify_bazaroo.py runs/<id>

Use `verify_bazaroo.py`, not plain `nandatown verify`: plain verify re-evaluates without
our validators loaded and reports "evaluator replay mismatch" for these bundles. With
the files loaded it prints "bundle verified: hashes match and the evaluator reproduces
the recorded result" (observed for both runs below).

Evidence events recorded by the plugin: `bazaroo_card_accepted`, `bazaroo_name_taken`,
`bazaroo_lookup_hit`, `bazaroo_lookup_empty`, plus Town's `card_registered`,
`card_publish_refused` and `registry_lookup_failed`.

## Not yet done / untested

- The scenario run with `nandatown run` and its `nandatown verify` evidence.
- Bazaroo is not an A2A endpoint, so Town's Path mode does not apply.
- Bazaroo has no delete endpoint, so live test rows accumulate.
- Search is substring-based; the plugin filters to exact capability matches.
- Unit tests use a stand-in for Town's engine; the full-engine behavior is
  exercised only by the scenario run.
