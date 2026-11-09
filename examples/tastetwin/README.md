# Taste Twin in Nanda Town

[Taste Twin](https://cafe-taste-twin.lovable.app) is a shared taste memory for
AI agents. Any assistant acting for a person can call it over MCP to rank
cafes the way that person would, and report what the person picked, so the
next assistant starts from what was learned. It is hosted separately; this
folder only connects it to the town.

The Lab scenario `tastetwin_shared_taste` puts it among independent agents:

| Agent | Acts for | Does |
|---|---|---|
| `tastetwin` (`tt_adapter`) | Taste Twin | Publishes a signed `taste.rank` card; turns town messages into MCP calls |
| `cafe-*` (`tt_cafe`, 6) | each cafe | Publishes its own `cafe.listing` card (hours, distance, noise, outlets) |
| `planner-a` (`tt_planner`) | the person | Finds cafes and Taste Twin in the registry, ranks, asks the person, reports the pick and one stated preference |
| `pang` (`tt_user`) | the person | Picks a cafe and says one lasting thing |
| `planner-b` (`tt_planner`) | the person | A different assistant, later. Gets no message from planner-a and starts with empty private memory; reads and ranks again |

Town layers exercised: registry (`index.owned.v1`), auth (`hmac.v1`, signed
cards and messages), memory (`kv.v1`, each planner's private working state),
transport (the choice message is delivered twice), privacy (`redact.v1`:
what the person said and the profile weights never leave the run).

The validator checks: `live_service`, `discovery`, `ranking_contract`,
`choice_binding`, `choice_applied_once`, `shared_memory`, plus Town's standing
`ledger_conserved` and `privacy`.

## Files

- `tastetwin_plugin.py` - the four roles, a small MCP client, and the validator
- `tastetwin_shared_taste.yaml` - the scenario (loads the plugin via `plugin_files:`)
- `tt_auth.py` - one-time browser sign-in for a headless client (OAuth 2.1 + PKCE)
- `verify.py` - `nandatown verify` with the plugin loaded (see Limitations)
- `fixtures/dedupe_on.json`, `fixtures/dedupe_off.json` - reduced responses
  recorded from the live server, for replay without credentials
- `evidence/live-dedupe-on/`, `evidence/live-dedupe-off/` - the two live bundles

## Run it

From the repository root, after `python -m pip install -e '.[dev]'`:

```bash
# Replay: no account or network needed
TASTETWIN_MODE=replay nandatown run examples/tastetwin/tastetwin_shared_taste.yaml --out runs/tt
python examples/tastetwin/verify.py runs/tt/<run-id>
python -m pytest -q tests/test_tastetwin_example.py
```

Live, with a Taste Twin account (writes one choice and one preference to it):

```bash
python examples/tastetwin/tt_auth.py start        # open the link, sign in, approve
python examples/tastetwin/tt_auth.py finish '<the localhost:6585/... address you land on>'
TASTETWIN_MODE=live nandatown run examples/tastetwin/tastetwin_shared_taste.yaml --out runs/tt-live

# The handshake on its own
nandatown mcp test --cmd "npx -y mcp-remote https://cafe-taste-twin.lovable.app/mcp \
  --header Authorization:Bearer\ $(python examples/tastetwin/tt_auth.py token)"
```

Failure case: `TASTETWIN_DEDUPE=0` makes the adapter forward every delivery of
the duplicated choice message. Add `TASTETWIN_RECORD=<path>` to a live run to
record a new fixture. Tokens live in `~/.tastetwin-town/` (mode 0600); never
commit them.

## What the live runs showed (2026-11-09, server `cafe-taste-twin` 0.1.0)

- **Duplicate protection on: passed.** Planner-a saw Diesel 0.567, 1369 0.564
  and Tatte 0.541 (`close_call` true; Darwin's and Flour dropped). The pick
  (1369) and the statement about Tatte were recorded once. Planner-b saw the
  choice count go 0 -> 1 and the Tatte statement, with no message from
  planner-a.
- **Duplicate protection off: failed on purpose.** `record_choice` accepted
  the same `request_id` twice (two `choice_id`s, count 1 -> 3, weights nudged
  twice). `record_feedback` refused its repeat (`saved: false`,
  `duplicate_of`). Taste Twin needs idempotency on `record_choice`.
- **No sign-in:** the server answers `401` with OAuth resource metadata;
  `nandatown mcp test` waits for a sign-in and reports `ok: false`.

## Limitations

- Planners are scripted stand-ins for assistants such as Claude or ChatGPT.
- Cafe listing facts are illustrative inputs, and the person's pick and
  statement are scenario settings, not a recorded past outing.
- Both planners use one OAuth client, so Taste Twin attributes their calls to
  one connected agent.
- One person, cafes only; isolation between people is not exercised.
- Ranking quality is out of scope. The live score for Tatte fell only from
  0.541 to 0.518 after the statement, so it stayed third.
- `nandatown verify` does not load a scenario's `plugin_files`, so a fresh
  process cannot replay this validator; `verify.py` loads it first. Bundles
  also record the plugin's absolute path.
- Replay checks Town-side behaviour against recorded responses only.
