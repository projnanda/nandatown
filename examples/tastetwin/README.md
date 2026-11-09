# Taste Twin in Nanda Town

[Taste Twin](https://cafe-taste-twin.lovable.app) is a shared taste memory for
AI agents. Any assistant acting for a person can call it over MCP to rank
cafes the way that person would, and report what the person picked, so the
next assistant starts from what was learned. It is hosted separately; this
folder only connects it to the town.

The Lab scenario `tastetwin_shared_taste` puts it among independent agents:

| Agent | Acts for | Does |
|---|---|---|
| `tastetwin` (`tt_adapter`) | Taste Twin | Publishes a signed `taste.rank` card; turns town messages into MCP calls, only for agents the person granted |
| `cafe-*` (`tt_cafe`, 6) | each cafe | Publishes its own `cafe.listing` card (hours, distance, noise, outlets) |
| `planner-a` (`tt_planner`) | the person | Finds cafes and Taste Twin in the registry, ranks, asks the person, reports the pick and one stated preference |
| `pang` (`tt_user`) | the person | Picks a cafe and says one lasting thing |
| `planner-b` (`tt_planner`) | the person | A different assistant, later. Gets no message from planner-a and starts with empty private memory; reads and ranks again |
| `snoop` (`tt_snoop`) | someone else | Finds Taste Twin in the registry like anyone can, claims to act for the person, asks for her taste and tries to plant a preference |

Town layers exercised: registry (`index.owned.v1`), auth (`hmac.v1`, signed
cards and messages), memory (`kv.v1`, each planner's private working state),
transport (the choice message is delivered twice), privacy (`redact.v1`:
what the person said and the profile weights never leave the run).

**Access.** Taste Twin checks its own callers: an OAuth token for one user,
then per-agent read and write switches. The adapter holds that one user's
sign-in, so Taste Twin cannot tell which town agent is behind a call. The
adapter therefore applies the person's grants itself: it serves only the
agents listed in `grants:` when they act for the person in `serves:`. Town's
signatures make the sender's name trustworthy; the grant decides access.

The validator checks: `live_service`, `server_auth`, `discovery`,
`ranking_contract`, `choice_binding`, `choice_applied_once`, `shared_memory`,
`access_control`, plus Town's standing `ledger_conserved` and `privacy`.

## Files

- `tastetwin_plugin.py` - the five roles, a small MCP client, and the validator
- `tastetwin_shared_taste.yaml` - the scenario (loads the plugin via `plugin_files:`)
- `tt_auth.py` - one-time browser sign-in for a headless client (OAuth 2.1 + PKCE)
- `verify.py` - `nandatown verify` with the plugin loaded (`nandatown verify`
  does not load a scenario's `plugin_files`, so it cannot replay this validator)
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

- **Duplicate protection on: passed, every stage.** 1369 Coffee House
  ranked first (0.606, then 0.645 for planner-b). The duplicated choice
  was applied once (count 3 -> 4). Planner-b, with no message from
  planner-a and empty private memory, saw the new count.
- **Duplicate protection off: failed on purpose.** `record_choice` accepted
  the same `request_id` twice: two `choice_id`s, count 4 -> 6, confidence
  moved from low to medium, and 1369's lead grew to 0.701. `record_feedback`
  refused its repeat (`saved: false`). Taste Twin needs idempotency on
  `record_choice`.
- **An agent nobody granted got nothing.** In both runs `snoop` sent three
  requests (read the profile, rank, plant "I love loud cafes for studying")
  claiming to act for the person. Each was refused before any Taste Twin
  call, and it received only `access_denied`.
- **The server refuses unknown callers.** A made-up token got `401` with an
  OAuth challenge; so did a request with no token.

## Not tested

- The planners are scripted stand-ins for real assistants such as Claude or
  ChatGPT, and the cafe data is illustrative.
- It runs in the Lab, not against a deployed NANDA Index.
- One person only. Taste Twin's own revoke switch and isolation between two
  people aren't exercised.
- Network failures on the calls to Taste Twin aren't injected, and ranking
  quality is out of scope.
