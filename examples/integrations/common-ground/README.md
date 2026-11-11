# Common Ground in Nanda Town

Common Ground is a neutral room where people's personal agents plan a real-world meetup for them.
Each agent reads its person's private calendar and profile through that person's own MCP server,
shares only typed fields with the room (free windows, and a travel and fit score for each venue), and
the room picks the time and place whose worst-off person scores highest. Project:
[javiersgp/common-ground-mvp](https://github.com/javiersgp/common-ground-mvp) (private; access on request).

This integration runs the whole system inside a Town Lab scenario: three real Claude Code agents, each
person's private MCP server, and the shared room.

```
                  me (private MCP: profile, calendar, free blocks, travel)
                   ▲  one per person, read only by that person's agent
                   │
Claude Code agent ─┤ decides windows, tags, scores
                   │
jack-agent  ─┐     ▼
javier-agent ┼─ Town messages (signed, faults) ─► common-ground-room ─ MCP ─► room: create_plan, join_plan,
sergio-agent ┘   cg_create_plan, cg_post_scores…    (cg_room_adapter)          post_availability, post_scores,
                                                                               wait_for_result
```

- `cg_person` is one person's agent in Town. The organizer finds the room and the other members
  through the registry, creates the plan and invites the others with a Town message. After joining,
  each agent hands the brief, the room's rules and the public venue list to a **Claude Code agent**
  (`claude -p`, model `sonnet`, no built-in tools) connected only to that person's private Common
  Ground MCP server (`personal/server.py`). That agent reads the profile and calendar, applies the
  person's notes, gets travel estimates, and answers with the windows, 3-4 taste tags and a score for
  every venue. The Town agent posts them and polls for the result. If the room refuses a post, the
  Claude Code agent sees the room's error once and can correct its answer, as the project's agents do
- `cg_room_adapter` publishes a signed `meetup.plan` card and is the only role that talks to the room.
  It maps the Town sender (authenticated by `hmac.v1`) to that person's room token, so no agent ever
  holds another person's token. Its MCP client is a small streamable-HTTP client on `httpx`, so Town
  needs no new dependency
- The `common_ground` validator rebuilds the outcome from Town events only. `run_check.py` also scans
  every output file for the private data (calendar titles, notes, preferences, home details)

## What is tested

| Stage | Checks |
| --- | --- |
| `room_interface` | the room offers the five planning tools over MCP |
| `live_room` | answers came from a running room (`not_tested` in replay) |
| `live_agents` | each person's windows, tags and scores came from its own Claude Code agent (`not_tested` in replay) |
| `discovery` | the organizer finds the room and both other members in the registry |
| `plan_created`, `all_joined`, `posts_accepted` | each room call succeeds for each person (after at most one repair) |
| `same_plan_for_everyone` | all three agents receive the same plan id, place and time |
| `slot_in_every_window` | the time is inside a window every agent posted |
| `fairness_consistent` | nobody's travel limit vetoes the place, and each agent's score and the lowest score match what was posted |
| `fairest_option` | `not_tested`: the trace has no opening hours, so Town cannot redo the room's search (Common Ground's own `verify.py` does) |
| `duplicate_contained` | duplicate case: the repeated call reaches the room, its answer is ignored, everyone keeps one plan |
| `dropout_reported` | dropout case: the waiting agents stop and name the missing person |
| `privacy` | the private server's keys (`title`, `notes_from_owner`, `home_area`, `preferences_in_their_words`) never appear in Town's records |

Three scenarios, all on the team's real Wednesday Nov 4 case (the team's calendars and notes, 15
OpenStreetMap cafés plus two homes, 2 hours, wifi, outlets, laptop-friendly, a table for three, open
the whole session):

- `cg_meetup.yaml`: the clean workflow
- `cg_meetup_duplicate.yaml`: Town delivers the third `cg_post_scores` twice
- `cg_meetup_dropout.yaml`: Town drops the third `cg_post_scores`, and that agent does not retry
  (a person's agent that goes silent). This one fails on purpose

## Reproduce

Nanda Town `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`, Python 3.12. Common Ground
`9fdee5c1efcdc09fefa84b2367c7ed03b8bcbdf8` (tag `hw7`, on `main`). Claude Code 2.1.319, model
`sonnet`. The evidence was recorded on 2026-11-11.

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'

# Replay: no checkout, model or login needed (room answers and agent decisions recorded live)
python examples/integrations/common-ground/run_check.py --out runs/cg
nandatown run examples/integrations/common-ground/cg_meetup.yaml --out runs/cg-one
python -m pytest -q tests/test_common_ground_example.py

# Verify the committed live evidence offline
python examples/integrations/common-ground/run_check.py --verify examples/integrations/common-ground/evidence/*/sim-*

# Live: a fresh room per case, three Claude Code agents (needs `claude` installed and logged in;
# about 1 minute and US$0.15-0.20 of model use per case)
git clone https://github.com/javiersgp/common-ground-mvp ../common-ground-mvp
git -C ../common-ground-mvp checkout 9fdee5c1efcdc09fefa84b2367c7ed03b8bcbdf8
python3 -m venv ../common-ground-mvp/.venv && ../common-ground-mvp/.venv/bin/pip install -r ../common-ground-mvp/requirements.txt
python examples/integrations/common-ground/run_check.py --project ../common-ground-mvp --out runs/cg-live
```

`run_check.py` exits 0 only when clean and duplicate pass, dropout fails, and the private-data scan
finds nothing. `--record` rewrites `fixtures/` from a live run. `nandatown verify` alone cannot replay
these bundles because it does not load the plugin's validator; `run_check.py --verify` loads it and
then calls the same `verify_bundle`.

## Results (live, `evidence/`)

| Case | Expected | Observed | Verdict |
| --- | --- | --- | --- |
| clean | one plan for all three, inside everyone's windows, within everyone's travel limit | Third Time Together, Wed Nov 4 16:30–18:30; Jack 7/10, Javier 6/10, Sergio 9/10 (lowest 6); 4 shared slots, 34 options ranked. The same place and time the team's HW7 agents chose | passed |
| duplicate | the repeated post changes nothing | the room refused the repeat with an error ("Plan is agreed; scoring is closed"); Sergio's agent ignored that stale answer by its call id; same plan | passed |
| dropout | the plan cannot close; the others say why | the room stayed open with Sergio's scores missing and no deadline; Jack's and Javier's agents gave up after 5 checks and both named Sergio | failed (on purpose) |

In every case each agent applied its person's notes: Javier's and Sergio's agents started at 16:30
because their notes say they need 20-30 minutes to get home after a 4 PM class, which the calendar
does not show. Each decision took 11-15 s (the one repair 9 s); the three agents cost about
US$0.15-0.19 per case.

Each case folder holds the Town bundle (`sim-*/`) and, next to it, what happened outside Town: the
room's log and final state, each private server's call log (summaries only), and the Common Ground
commit it ran. The private-data scan was clean for all three.

What this shows about compatibility:

- The whole system works as a shared Town service: agents find the room through the registry, Town's
  signed sender plus the adapter's seat map stand in for the room's per-agent tokens, and each
  person's private data stays with that person's agent
- **A real agent read the interface differently.** In the dropout run (and in one earlier live run),
  Sergio's agent posted every possible 2-hour start (16:30, 17:00, 17:30, 18:00) as four overlapping
  windows. The room refused them ("Windows overlap; merge them"); after seeing that error the agent
  merged them into one window, 16:30–20:00. The room's rules never say windows must not overlap, so
  they should
- The room's writes are not idempotent. A retried `post_scores` after the plan closed gets an error, not
  the original acknowledgement. Under an at-least-once transport, an agent that does not correlate
  replies would read its own successful post as a failure
- The room has no deadline. One silent agent leaves the plan open forever, and only the other agents'
  own limits end the run. Common Ground needs a plan deadline and a "who is missing" result

## Limitations and what is not tested

- All three agents run on Claude Code here. In the project, Sergio's agent runs on Codex and Jack's on
  Hermes; those harnesses were not installed on the machine that recorded this
- The agents decide what to share; the Town side (invitations, posting, polling) is scripted. In the
  project the agents call the room themselves. Town's invitation message replaces the room's
  `wait_for_invite` long poll
- LLM agents vary between runs (scores, sometimes the windows), so a live rerun can give different
  numbers. Replay repeats the recorded answers exactly
- Homes (in each private profile and the two home venues) are blurred to neighborhood level before the
  run, so travel times differ a little from the team's HW7 runs
- `fairest_option` is not checked inside Town (no opening hours in the trace)
- Replay checks the Town side only. It is not live evidence, and `live_room` and `live_agents` say so
- Time zones and the video-call path (room v2) are not exercised here; Common Ground's own
  `tests/tz_video_room.py` covers them
- The room runs on localhost; a hosted room and agents on separate machines are not tested
