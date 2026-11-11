# Common Ground in Nanda Town

Common Ground is a neutral room where people's personal agents plan a real-world meetup for them.
Each agent shares only typed fields (free windows, and a travel and fit score for each venue), and
the room picks the time and place whose worst-off person scores highest. In the course project the
three agents are Claude Code, Codex and Hermes, and the room is an MCP server
([javiersgp/common-ground-mvp](https://github.com/javiersgp/common-ground-mvp), private; access on request).

This integration runs the whole planning workflow inside a Town Lab scenario against the real room:

```
jack-agent ─┐                                       ┌─ room: create_plan, join_plan,
javier-agent ┼─ Town messages (signed, faults) ─► common-ground-room ─ MCP ─┤  post_availability, post_scores,
sergio-agent ┘   cg_create_plan, cg_post_scores…    (cg_room_adapter)       └─ wait_for_result (Common Ground)
```

- `cg_room_adapter` publishes a signed `meetup.plan` card and is the only role that talks to the room.
  It maps the Town sender (authenticated by `hmac.v1`) to that person's room token, so no agent ever
  holds another person's token. Its MCP client is a small streamable-HTTP client on `httpx`, so Town
  needs no new dependency
- `cg_person` (three of them) stands in for each person's agent. The organizer finds the room and the
  other members through the registry, creates the plan and invites the others with a Town message. Each
  agent joins, posts its windows, computes travel from its person's home and scores every candidate
  venue, then checks for the result. Home, travel mode and travel limit are in `redact_fields`
- The `common_ground` validator rebuilds the outcome from Town events only

## What is tested

| Stage | Checks |
| --- | --- |
| `room_interface` | the room offers the five planning tools over MCP |
| `live_room` | answers came from a running room (`not_tested` in replay) |
| `discovery` | the organizer finds the room and both other members in the registry |
| `plan_created`, `all_joined`, `posts_accepted` | each room call succeeds for each person |
| `same_plan_for_everyone` | all three agents receive the same plan id, place and time |
| `slot_in_every_window` | the time is inside a window every agent posted |
| `fairness_consistent` | nobody's travel limit vetoes the place, and each agent's score and the lowest score match what was posted |
| `fairest_option` | `not_tested`: the trace has no opening hours, so Town cannot redo the room's search (Common Ground's own `verify.py` does) |
| `duplicate_contained` | duplicate case: the repeated call reaches the room, its answer is ignored, everyone keeps one plan |
| `dropout_reported` | dropout case: the waiting agents stop and name the missing person |
| `privacy` | home, travel mode and travel limit never leave the run |

Three scenarios, all on the team's real Wednesday Nov 4 case (15 OpenStreetMap cafés plus two homes,
2 hours, wifi, outlets, laptop-friendly, a table for three, open the whole session):

- `cg_meetup.yaml`: the clean workflow
- `cg_meetup_duplicate.yaml`: Town delivers the last `cg_post_scores` twice
- `cg_meetup_dropout.yaml`: Town drops the last `cg_post_scores`, and that agent does not retry
  (a person's agent that goes silent). This one fails on purpose

## Reproduce

Nanda Town `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`, Python 3.12. Common Ground
`9fdee5c1efcdc09fefa84b2367c7ed03b8bcbdf8` (tag `hw7`, on `main`). The evidence was recorded on
2026-11-11.

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'

# Replay: no Common Ground checkout needed (answers recorded from the live room)
python examples/integrations/common-ground/run_check.py --out runs/cg
nandatown run examples/integrations/common-ground/cg_meetup.yaml --out runs/cg-one
python -m pytest -q tests/test_common_ground_example.py

# Verify the committed live evidence offline
python examples/integrations/common-ground/run_check.py --verify examples/integrations/common-ground/evidence/*/sim-*

# Live: starts the real room from a checkout, one fresh room per case
git clone https://github.com/javiersgp/common-ground-mvp ../common-ground-mvp
git -C ../common-ground-mvp checkout 9fdee5c1efcdc09fefa84b2367c7ed03b8bcbdf8
python3 -m venv ../common-ground-mvp/.venv && ../common-ground-mvp/.venv/bin/pip install -r ../common-ground-mvp/requirements.txt
python examples/integrations/common-ground/run_check.py --project ../common-ground-mvp --out runs/cg-live
```

`run_check.py` exits 0 only when clean and duplicate pass and dropout fails. `--record` rewrites
`fixtures/` from a live run. `nandatown verify` alone cannot replay these bundles because it does not
load the plugin's validator; `run_check.py --verify` loads it and then calls the same `verify_bundle`.

## Results (live, `evidence/`)

| Case | Expected | Observed | Verdict |
| --- | --- | --- | --- |
| clean | one plan for all three, inside everyone's windows, within everyone's travel limit | Third Time Together, Wed Nov 4 16:30–18:30; Jack 5/10, Javier 5/10, Sergio 7/10 (lowest 5); 4 shared slots, 34 options ranked | passed |
| duplicate | the repeated post changes nothing | the room refused the repeat with an error ("Plan is agreed; scoring is closed"); Sergio's agent ignored that stale answer by its call id; same plan as clean | passed |
| dropout | the plan cannot close; the others say why | the room stayed in `collecting` with Sergio's scores missing and no deadline; Jack's and Javier's agents gave up after 5 checks and both named Sergio | failed (on purpose) |

Each case folder holds the Town bundle (`sim-*/`) and, next to it, the room's own side of the run
(`room/room_log.jsonl`, `room/room_state.json`) and the Common Ground commit it ran.

What this shows about compatibility:

- The room works as a shared Town service: agents find it through the registry, and Town's signed
  sender plus the adapter's seat map stand in for the room's per-agent tokens
- The room's writes are not idempotent. A retried `post_scores` after the plan closed gets an error, not
  the original acknowledgement. Under an at-least-once transport, an agent that does not correlate
  replies would read its own successful post as a failure
- The room has no deadline. One silent agent leaves the plan open forever, and only the other agents'
  own limits end the run. Common Ground needs a plan deadline and a "who is missing" result

## Limitations and what is not tested

- The person agents are scripted stand-ins. In the real project they are Claude Code, Codex and Hermes
  reading a private calendar and profile through a private MCP server; that LLM step is not exercised
  here. Their windows are the ones those agents posted in the HW7 benchmark run for this case
- Town's invitation message replaces the room's `wait_for_invite` long poll
- Fit scores use a simple tag and venue-vibe overlap, and travel is a straight-line estimate
- Homes (the agents' starting points and the two home venues) are blurred to neighborhood level
  (`BLURRED_HOMES` in `run_check.py`), so travel times differ a little from the team's HW7 runs
- `fairest_option` is not checked inside Town (no opening hours in the trace)
- Replay checks the Town side only. It is not live evidence, and `live_room` says so
- Time zones and the video-call path (room v2) are not exercised here; Common Ground's own
  `tests/tz_video_room.py` covers them
- The room runs on localhost; a hosted room and agents on separate machines are not tested
