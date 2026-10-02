# Town Square: personal agents meeting in NANDA Town

Author: Umamaheswar Edara. Status: in progress on `feat/town-square`.


Town Square is a fourth mode beside the Lab, the Track and Path. The Lab
runs scripted roles in one process; the Track runs a single quote
exchange between separate processes. Town Square is where independently
built personal agents (an Instinct-like agent, a Muse-like agent, an
OpenClaw agent) arrive, find each other, and do an errand together under
rules that the town enforces and records.

## The scenario

Three agents take part. Roles are configuration, not code; the default
cast is:

- **Agent A**, Instinct-like personal agent: plans a dinner for its user
  and negotiates with the restaurant.
- **Agent B**, Muse-like personal agent: the other diner's agent; shares
  free/busy time with A under a relationship.
- **Agent C**, OpenClaw agent: the booking agent for MoonRestaurant,
  holding the restaurant's own made-up table schedule.

| # | Step | Town Square piece |
|---|---|---|
| 1 | Discover each other, using NANDA Index | **index client**: register by agent email, search the index |
| 2 | Establish identity | existing `Keystore` controller keys (`did:town:`) |
| 3 | Establish relationship | **relationships**: mutually signed, scoped |
| 4 | Jointly book from made-up calendars | **calendars** for A and B; **MoonRestaurant** schedule held by C |
| 5 | Negotiate availability, A with booking agent C | **slot negotiation**: propose, counter, accept, hold |
| 6 | Purchase something | **payment desk** over the existing `ledger.v1` |
| 7 | Detect an unauthorized data request | **relationships** `authorize` on every calendar read |
| 8 | Revoke a relationship | **relationships** unilateral, signed revocation |
| 9 | Generate signed receipts | **per-action receipts**, signed by the acting agent |

## What is reused and what is new

Reused unchanged from NANDA Town: the `ledger.v1` payments layer, the
`haggle.v1` negotiation layer, the `index.v1` registry layer, the
portable identity `Keystore` and `verify_signature`, canonical JSON and
fingerprints from `records.py`, and the evidence bundle format.

New in Town Square (`src/nandatown/square/`): relationships with scoped
consent and revocation, the data-request authorization check, the mock
calendar and restaurant services, slot negotiation, the payment desk,
per-action receipts, the personal agent gateway (MCP and HTTP), and the
nine-step scenario with its evaluator.

## Relationships

A relationship is a set of terms both agents sign with their controller
keys: the two parties, the scopes each grants the other (for example
`calendar.freebusy`), when it was issued and when it expires. Its id is
the fingerprint of the terms, so either party can cite it exactly.

- Established only when both signatures verify against the controller
  keys the town resolves for the two agent ids.
- Every data request names a requester, an owner and a scope. It is
  allowed only under an active, unexpired relationship in which the owner
  granted that scope to the requester. Anything else is recorded as an
  `unauthorized_data_request` with the reason, and refused.
- Either party may revoke alone, by signing a revocation of the
  relationship id. Revocation takes effect on the very next request; it is
  checked on every request, not only when the relationship is formed.
- Time is passed in, never read from the wall clock, so runs replay.

## Discovery through NANDA Index

Agents register on NANDA Index (`api.nandaindex.org`, source
`nanda-index-v2`) the way a person would: an account for the agent's
email (`POST /auth/register`, or `/auth/login` if it exists), then a
personal index record (`POST /api/v1/orgs`, `hosting_path: personal`,
`contact_email` the agent email, `registry_url` its agent card). Peers
are found with `GET /api/v1/search` and `GET /api/v1/agentic-search`.

Activation: the index emails a verification link to the contact email;
following it (`GET /api/v1/verify-email?token=`) activates a personal,
no-domain record (nanda-index-v2 `5b8b0d8`). Until then the record is
`pending` and search does not return it, so an agent must be able to read
its own inbox to finish registering. Town Square reports a pending
registration as not yet discoverable rather than failing silently. Tests
use an in-process stand-in with the same endpoints and the same rule.

## Calendars and the booking agent

A and B each hold a made-up calendar. Reading another agent's calendar
is a data request: `calendar.freebusy` returns busy intervals only,
`calendar.details` also returns titles, and each is authorized against
the relationships in force. C keeps MoonRestaurant's tables, opening
hours and existing reservations; its availability is public.

Slot negotiation alternates, like `haggle.v1` but over times: A proposes
slots both diners are free for; C accepts the first one a table can seat
and holds it, or counters with the nearest available slots; A may accept
a countered slot. Rounds are bounded, and every step is an event.

Rules the square enforces here:

- Times are local wall-clock times of the scenario; a time with a time
  zone is refused rather than compared with local ones.
- A guest holds at most one table at a time, so no guest can take every
  table. Holds do not yet expire; a hold lifetime is still open.
- A countered round always shows its counters, and they stay acceptable;
  only a proposal beyond `max_rounds` fails as `out_of_rounds`.
- A calendar read is authorized before anything else is looked at, so a
  stranger's request is recorded and learns nothing.
