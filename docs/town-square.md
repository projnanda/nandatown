# Town Square: personal agents meeting in NANDA Town

Author: Umamaheswar Edara. Status: in progress on `feat/town-square`.


Town Square is a fourth mode beside the Lab, the Track and Path. The Lab
runs scripted roles in one process; the Track runs a single quote
exchange between separate processes. Town Square is where independently
built personal agents (an Instinct-like agent, a Muse-like agent, an
OpenClaw agent) arrive, find each other, and do an errand together under
rules that the town enforces and records.

## The scenario

| # | Step | Town Square piece |
|---|---|---|
| 1 | Discover each other | square directory over the existing `index.v1` signed cards |
| 2 | Establish identity | existing `Keystore` controller keys (`did:town:`) |
| 3 | Establish relationship | **relationships**: mutually signed, scoped |
| 4 | Jointly book a mock restaurant from made-up calendars | **calendars** and **MoonRestaurant** services |
| 5 | Negotiate restaurant availability | **slot negotiation** over the calendars |
| 6 | Purchase something | **payment desk** over the existing `ledger.v1` |
| 7 | Detect an unauthorized data request | **relationships** `authorize` on every data request |
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
