"""Slot negotiation: a guest agent and a restaurant's booking agent.

The same alternating shape as the negotiation layer's haggle.v1, over
times instead of prices. The guest proposes slots in preference order;
the booking agent holds a table at the first one it can seat, or counters
with the available slots nearest the guest's first choice. The guest may
accept a countered slot or propose again. Rounds are bounded, the outcome
is recorded, and only the guest who started the session may speak in it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime

from .booking import MoonRestaurant
from .calendar import CalendarError, parse_time


class NegotiationError(Exception):
    pass


@dataclass(frozen=True)
class Session:
    nid: str
    guest: str
    party: int
    day: str
    state: str = "open"
    rounds: int = 0
    counters: tuple[str, ...] = ()


@dataclass(frozen=True)
class Outcome:
    state: str
    slot: str | None = None
    reservation_ref: str | None = None
    counters: list[str] = field(default_factory=list)
    reason: str | None = None


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="minutes")


class SlotNegotiation:
    def __init__(self, engine, restaurant: MoonRestaurant,
                 max_rounds: int = 3, counter_limit: int = 3):
        self.engine = engine
        self.restaurant = restaurant
        self.max_rounds = max_rounds
        self.counter_limit = counter_limit
        self._sessions: dict[str, Session] = {}

    def _put(self, session: Session) -> Session:
        self._sessions = {**self._sessions, session.nid: session}
        return session

    def _no_open_hold(self, guest: str) -> None:
        if self.restaurant.holds_for(guest):
            raise NegotiationError(f"{guest} already holds a table")

    def start(self, guest: str, party: int, day: str) -> str:
        if type(party) is not int or party <= 0:
            raise NegotiationError("party must be a positive integer")
        try:
            date.fromisoformat(day)
        except (TypeError, ValueError) as exc:
            raise NegotiationError("day must be an ISO date") from exc
        self._no_open_hold(guest)
        nid = f"slots-{len(self._sessions) + 1}"
        self._put(Session(nid, guest, party, day))
        self.engine.emit(guest, "slot_negotiation_started", nid,
                         {"restaurant": self.restaurant.name,
                          "party": party, "day": day})
        return nid

    def _open(self, nid: str, by: str) -> Session:
        if nid not in self._sessions:
            raise NegotiationError(f"unknown negotiation {nid}")
        session = self._sessions[nid]
        if session.state != "open":
            raise NegotiationError(f"negotiation {nid} is {session.state}")
        if by != session.guest:
            raise NegotiationError(
                f"only the guest {session.guest} negotiates in {nid}")
        return session

    def _slots(self, session: Session, slots: list[str]) -> list[datetime]:
        try:
            parsed = [parse_time(s, "slot") for s in slots]
        except CalendarError as exc:
            raise NegotiationError(str(exc)) from exc
        if not parsed:
            raise NegotiationError("propose at least one slot")
        if any(p.date().isoformat() != session.day for p in parsed):
            raise NegotiationError(
                f"every slot must fall on the negotiated day {session.day}")
        return parsed

    def _agree(self, session: Session, start: datetime) -> Outcome:
        held = self.restaurant.hold(session.guest, start, session.party)
        self._put(replace(session, state="agreed"))
        self.engine.emit(self.restaurant.name, "slot_agreed", session.nid,
                         {"slot": _iso(start), "reservation": held.ref})
        return Outcome("agreed", slot=_iso(start), reservation_ref=held.ref)

    def _fail(self, session: Session, reason: str) -> Outcome:
        self._put(replace(session, state="failed"))
        self.engine.emit(self.restaurant.name, "slot_negotiation_failed",
                         session.nid, {"reason": reason})
        return Outcome("failed", reason=reason)

    def _nearest(self, session: Session, wanted: datetime) -> list[str]:
        free = self.restaurant.available_starts(session.day, session.party)
        ranked = sorted(free, key=lambda s: (abs(s - wanted), s))
        return [_iso(s) for s in ranked[:self.counter_limit]]

    def propose(self, nid: str, by: str, slots: list[str]) -> Outcome:
        session = self._open(nid, by)
        wanted = self._slots(session, slots)
        self._no_open_hold(by)
        if session.rounds >= self.max_rounds:
            return self._fail(session, "out_of_rounds")
        session = self._put(replace(session, rounds=session.rounds + 1))
        self.engine.emit(by, "slots_proposed", nid,
                         {"slots": [_iso(w) for w in wanted],
                          "round": session.rounds})
        for start in wanted:
            if self.restaurant.table_for(start, session.party) is not None:
                return self._agree(session, start)
        counters = self._nearest(session, wanted[0])
        if not counters:
            return self._fail(session, "fully_booked")
        self._put(replace(session, counters=tuple(counters)))
        self.engine.emit(self.restaurant.name, "slots_countered", nid,
                         {"slots": counters, "round": session.rounds})
        return Outcome("countered", counters=counters)

    def accept(self, nid: str, by: str, slot: str) -> Outcome:
        session = self._open(nid, by)
        start = self._slots(session, [slot])[0]
        if _iso(start) not in session.counters:
            raise NegotiationError(f"{slot} was not offered in {nid}")
        self._no_open_hold(by)
        if self.restaurant.table_for(start, session.party) is None:
            return self._fail(session, "slot_taken")
        return self._agree(session, start)
