"""Town Square walkthrough: three personal agents, one dinner.

Runs every Town Square step built so far, in one process, and narrates
it. Nothing touches the network: NANDA Index is the in-process stand-in
and keys go to a temporary directory.

    python examples/town_square_demo.py
    python examples/town_square_demo.py --events events.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from functools import partial

from nandatown.identity_portable import Keystore, resolve_file
from nandatown.square.booking import MoonRestaurant
from nandatown.square.calendar import (
    Calendar,
    CalendarDesk,
    CalendarError,
    common_free_starts,
)
from nandatown.square.local_index import LocalNandaIndex
from nandatown.square.relationships import (
    RelationshipBook,
    relationship_terms,
    revocation_payload,
)
from nandatown.square.slots import SlotNegotiation

DAY = "2026-10-10"
PASSWORD = "town-square-demo"

AGENTS = [
    {"key": "instinct", "org_id": "instinct-a", "label": "Agent A",
     "display_name": "Square Instinct", "email": "instinct-a@agents.test"},
    {"key": "muse", "org_id": "muse-b", "label": "Agent B",
     "display_name": "Square Muse", "email": "muse-b@agents.test"},
    {"key": "openclaw", "org_id": "openclaw-c", "label": "Agent C",
     "display_name": "Square OpenClaw (MoonRestaurant booking)",
     "email": "openclaw-c@agents.test"},
]

CALENDAR_A = [{"start": f"{DAY}T17:00", "end": f"{DAY}T18:00",
               "title": "Dentist"},
              {"start": f"{DAY}T21:30", "end": f"{DAY}T22:00",
               "title": "Call with mom"}]
CALENDAR_B = [{"start": f"{DAY}T18:00", "end": f"{DAY}T18:30",
               "title": "Gym"}]

MOON = {
    "name": "moon-restaurant", "opens": "17:00", "closes": "22:00",
    "seating_minutes": 90, "step_minutes": 30,
    "tables": [{"table_id": "t1", "seats": 2}, {"table_id": "t2", "seats": 4}],
    "reservations": [
        {"table_id": "t1", "start": f"{DAY}T19:00", "party": 2},
        {"table_id": "t2", "start": f"{DAY}T18:30", "party": 4},
    ],
}


class EventLog:
    """Collects every event the square emits, in order."""

    def __init__(self):
        self.events: list[dict] = []

    def emit(self, observer, kind, subject, detail=None):
        self.events = [*self.events, {"seq": len(self.events) + 1,
                                      "observer": observer, "kind": kind,
                                      "subject": subject,
                                      "detail": detail or {}}]


def step(number, title):
    print(f"\n== Step {number}: {title}")


def say(text):
    print(f"   {text}")


def hhmm(iso):
    return iso[11:16]


def discover(index):
    step(1, "discover each other through NANDA Index")
    client = index.client()
    for agent in AGENTS:
        token = client.account_token(agent["email"], PASSWORD)
        reg = client.register_agent(
            token, org_id=agent["org_id"],
            display_name=agent["display_name"],
            contact_email=agent["email"],
            card_url=f"https://agents.test/{agent['org_id']}/card.json",
            tags=["town-square"])
        say(f"{agent['label']} registered {reg.org_id} by"
            f" {agent['email']}: {reg.status}")
    say(f"search before verification: {client.search('square')}")
    for agent in AGENTS:
        client.verify_email(index.outbox[agent["email"]])
    peers = client.find_peers("square", me="instinct-a")
    say("emails verified; Agent A finds: "
        + ", ".join(p["org_id"] for p in peers))


def identities(ks):
    step(2, "establish identity")
    ids = {}
    for agent in AGENTS:
        identity = ks.new_identity(agent["key"])
        ids = {**ids, agent["key"]: identity["agent_id"]}
        say(f"{agent['label']} -> {identity['agent_id']}")
    return ids


def relate(ks, book, a, b):
    step(3, "establish a relationship (A and B share free/busy)")
    terms = relationship_terms(a, b, grants={a: ["calendar.freebusy"],
                                             b: ["calendar.freebusy"]},
                               issued_at=0.0, expires_at=3600.0,
                               nonce="dinner")
    rid = book.establish(terms, {a: ks.sign("instinct", terms),
                                 b: ks.sign("muse", terms)}, now=1.0)
    say(f"both signed; relationship {rid}")
    return rid


def plan(desk, calendars, a, b):
    step(4, "jointly plan from made-up calendars")
    window = {"start": f"{DAY}T17:00", "end": f"{DAY}T22:00"}
    busy_b = desk.read(requester=a, owner=b, scope="calendar.freebusy",
                       now=2.0, **window)
    say("A reads B's free/busy: "
        + ", ".join(f"{hhmm(x['start'])}-{hhmm(x['end'])}" for x in busy_b))
    starts = common_free_starts(
        [calendars[a], calendars[b]],
        datetime.fromisoformat(window["start"]),
        datetime.fromisoformat(window["end"]), minutes=90)
    free = [s.isoformat(timespec="minutes") for s in starts]
    say("both free for 90 minutes at: " + ", ".join(hhmm(s) for s in free))
    return free


def negotiate(talks, a, free):
    step(5, "A negotiates a table with booking agent C (MoonRestaurant)")
    nid = talks.start(a, party=2, day=DAY)
    first = free[1:2]
    outcome = talks.propose(nid, by=a, slots=first)
    say(f"A proposes {hhmm(first[0])}: {outcome.state}, C counters "
        + ", ".join(hhmm(s) for s in outcome.counters))
    pick = next(s for s in outcome.counters if s in free)
    outcome = talks.accept(nid, by=a, slot=pick)
    say(f"A accepts {hhmm(pick)} (fits both calendars): {outcome.state},"
        f" table held as {outcome.reservation_ref}")


def unauthorized(desk, ids):
    step(7, "detect unauthorized data requests")
    window = {"start": f"{DAY}T17:00", "end": f"{DAY}T22:00"}
    for requester, owner, scope, who in [
            (ids["openclaw"], ids["muse"], "calendar.freebusy",
             "C asks for B's calendar"),
            (ids["instinct"], ids["muse"], "calendar.details",
             "A asks for B's event titles")]:
        try:
            desk.read(requester=requester, owner=owner, scope=scope,
                      now=3.0, **window)
        except CalendarError as exc:
            say(f"{who}: REFUSED ({str(exc).rsplit(': ', 1)[-1]})")


def revoke(ks, book, desk, rid, ids):
    step(8, "revoke the relationship")
    payload = revocation_payload(rid, ids["muse"], 4.0)
    book.revoke(rid, by=ids["muse"], signature=ks.sign("muse", payload),
                at=4.0)
    say("B revokes, signed")
    try:
        desk.read(requester=ids["instinct"], owner=ids["muse"],
                  scope="calendar.freebusy", now=5.0,
                  start=f"{DAY}T17:00", end=f"{DAY}T22:00")
    except CalendarError as exc:
        say(f"A reads B's free/busy again: REFUSED"
            f" ({str(exc).rsplit(': ', 1)[-1]})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--events", help="write the event log as JSON lines")
    args = parser.parse_args(argv)

    log = EventLog()
    with tempfile.TemporaryDirectory() as home:
        ks = Keystore(home)
        discover(LocalNandaIndex())
        ids = identities(ks)
        a, b = ids["instinct"], ids["muse"]
        book = RelationshipBook(log, partial(resolve_file, ks.registry_path))
        rid = relate(ks, book, a, b)
        calendars = {a: Calendar.from_entries(a, CALENDAR_A),
                     b: Calendar.from_entries(b, CALENDAR_B)}
        desk = CalendarDesk(log, book, calendars)
        free = plan(desk, calendars, a, b)
        restaurant = MoonRestaurant.from_config(log, MOON)
        negotiate(SlotNegotiation(log, restaurant), a, free)
        step(6, "purchase something - not built yet")
        unauthorized(desk, ids)
        revoke(ks, book, desk, rid, ids)
        step(9, "signed receipts - not built yet")

    print(f"\n{len(log.events)} events recorded: "
          + ", ".join(sorted({e['kind'] for e in log.events})))
    if args.events:
        with open(args.events, "w") as f:
            f.writelines(json.dumps(event) + "\n" for event in log.events)
        print(f"event log written to {args.events}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
