"""MoonRestaurant: the mock restaurant its booking agent speaks for.

The booking agent (Agent C by default) holds the restaurant's own made-up
schedule: tables, opening hours, a seating length, a time grid and the
reservations already on the books. Availability is public. A table is
held for a guest, then confirmed against a payment reference, or
released. Records are immutable; each change replaces one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any

from .calendar import CalendarError, parse_time

TAKEN = {"existing", "held", "confirmed"}


class BookingError(Exception):
    pass


@dataclass(frozen=True)
class Table:
    table_id: str
    seats: int


@dataclass(frozen=True)
class Reservation:
    ref: str
    table_id: str
    start: datetime
    end: datetime
    party: int
    guest: str | None
    state: str
    payment_ref: str | None = None

    def public(self) -> dict[str, Any]:
        return {"ref": self.ref, "table_id": self.table_id,
                "start": self.start.isoformat(timespec="minutes"),
                "party": self.party, "state": self.state}


def _positive(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise BookingError(f"{label} must be a positive integer")
    return value


def _clock(value: Any, label: str) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise BookingError(f"{label} must be HH:MM") from exc


def _tables(raw: list[dict[str, Any]]) -> tuple[Table, ...]:
    tables = [Table(str(t["table_id"]), _positive(t["seats"], "seats"))
              for t in raw]
    ids = [t.table_id for t in tables]
    if len(set(ids)) != len(ids):
        raise BookingError("a table id is listed twice")
    return tuple(sorted(tables, key=lambda t: (t.seats, t.table_id)))


class MoonRestaurant:
    def __init__(self, engine, name: str, tables: tuple[Table, ...],
                 opens: time, closes: time, seating_minutes: int,
                 step_minutes: int):
        if closes <= opens:
            raise BookingError("the restaurant must close after it opens")
        self.engine = engine
        self.name = name
        self.tables = tables
        self.opens, self.closes = opens, closes
        self.seating = timedelta(minutes=_positive(seating_minutes,
                                                   "seating_minutes"))
        self.step = _positive(step_minutes, "step_minutes")
        self._reservations: dict[str, Reservation] = {}

    @classmethod
    def from_config(cls, engine, config: dict[str, Any]) -> MoonRestaurant:
        try:
            restaurant = cls(
                engine, config["name"], _tables(config["tables"]),
                _clock(config["opens"], "opens"),
                _clock(config["closes"], "closes"),
                config["seating_minutes"], config["step_minutes"])
        except KeyError as exc:
            raise BookingError(f"restaurant config needs {exc}") from exc
        for raw in config.get("reservations", []):
            restaurant._book_existing(raw)
        return restaurant

    def _book_existing(self, raw: dict[str, Any]) -> None:
        table_id = raw.get("table_id")
        if table_id not in {t.table_id for t in self.tables}:
            raise BookingError(f"reservation names unknown table {table_id}")
        try:
            start = parse_time(raw.get("start"), "start")
        except CalendarError as exc:
            raise BookingError(str(exc)) from exc
        self._put(Reservation(self._next_ref(), table_id, start,
                              start + self.seating,
                              _positive(raw.get("party"), "party"),
                              None, "existing"))

    # -- schedule ---------------------------------------------------------

    def _next_ref(self) -> str:
        return f"res-{len(self._reservations) + 1}"

    def _put(self, reservation: Reservation) -> None:
        self._reservations = {**self._reservations,
                              reservation.ref: reservation}

    def reservation(self, ref: str) -> Reservation:
        if ref not in self._reservations:
            raise BookingError(f"unknown reservation {ref}")
        return self._reservations[ref]

    def _on_grid(self, start: datetime) -> bool:
        opening = datetime.combine(start.date(), self.opens, start.tzinfo)
        closing = datetime.combine(start.date(), self.closes, start.tzinfo)
        offset = (start - opening).total_seconds() / 60
        return (opening <= start and start + self.seating <= closing
                and offset % self.step == 0)

    def _table_free(self, table_id: str, start: datetime) -> bool:
        end = start + self.seating
        return not any(r.table_id == table_id and r.state in TAKEN
                       and r.start < end and start < r.end
                       for r in self._reservations.values())

    def table_for(self, start: datetime, party: int) -> Table | None:
        """The smallest free table seating party at start, if any."""
        if not self._on_grid(start):
            return None
        return next((t for t in self.tables if t.seats >= party
                     and self._table_free(t.table_id, start)), None)

    def available_starts(self, day: str, party: int) -> list[datetime]:
        opening = datetime.combine(date.fromisoformat(day), self.opens)
        closing = datetime.combine(opening.date(), self.closes)
        starts, cursor = [], opening
        while cursor + self.seating <= closing:
            if self.table_for(cursor, party) is not None:
                starts.append(cursor)
            cursor += timedelta(minutes=self.step)
        return starts

    # -- holds ------------------------------------------------------------

    def holds_for(self, guest: str) -> list[Reservation]:
        return [r for r in self._reservations.values()
                if r.guest == guest and r.state == "held"]

    def hold(self, guest: str, start: datetime, party: int) -> Reservation:
        """One open hold per guest, so no guest can take every table."""
        if self.holds_for(guest):
            raise BookingError(f"{guest} already holds a table")
        table = self.table_for(start, _positive(party, "party"))
        if table is None:
            raise BookingError(
                f"no table for {party} at {start.isoformat()}")
        held = Reservation(self._next_ref(), table.table_id, start,
                           start + self.seating, party, guest, "held")
        self._put(held)
        self.engine.emit(self.name, "table_held", held.ref,
                         {**held.public(), "guest": guest})
        return held

    def _change(self, ref: str, state: str, **fields: Any) -> Reservation:
        current = self.reservation(ref)
        if current.state != "held":
            raise BookingError(f"reservation {ref} is not held")
        changed = replace(current, state=state, **fields)
        self._put(changed)
        return changed

    def confirm(self, ref: str, payment_ref: str) -> Reservation:
        confirmed = self._change(ref, "confirmed", payment_ref=payment_ref)
        self.engine.emit(self.name, "reservation_confirmed", ref,
                         {**confirmed.public(), "payment_ref": payment_ref})
        return confirmed

    def release(self, ref: str) -> Reservation:
        released = self._change(ref, "released")
        self.engine.emit(self.name, "table_released", ref,
                         released.public())
        return released
