"""Made-up calendars, and reading them as authorized data requests.

Each personal agent holds a calendar of busy entries. Another agent may
read it only through the CalendarDesk, which asks the RelationshipBook
first: `calendar.freebusy` returns busy intervals, `calendar.details`
also returns titles. A refused read is an unauthorized_data_request
event, recorded by the book, and raises here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .relationships import RelationshipBook

SCOPES = {"calendar.freebusy", "calendar.details"}


class CalendarError(Exception):
    pass


def parse_time(value: Any, label: str) -> datetime:
    """An ISO local time. The square uses one convention, the scenario's
    wall clock, so a time with a time zone is refused rather than
    compared with local ones."""
    if not isinstance(value, str):
        raise CalendarError(f"{label} must be an ISO time string")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CalendarError(
            f"{label} {value!r} is not an ISO time") from exc
    if moment.tzinfo is not None:
        raise CalendarError(
            f"{label} {value!r} has a time zone; use local times")
    return moment


@dataclass(frozen=True)
class Entry:
    start: datetime
    end: datetime
    title: str = ""

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end

    def busy(self) -> dict[str, str]:
        return {"start": self.start.isoformat(timespec="minutes"),
                "end": self.end.isoformat(timespec="minutes")}


def _entry(raw: dict[str, Any]) -> Entry:
    if "start" not in raw or "end" not in raw:
        raise CalendarError("an entry needs a start and an end")
    start = parse_time(raw["start"], "start")
    end = parse_time(raw["end"], "end")
    if end <= start:
        raise CalendarError("an entry must end after it starts")
    return Entry(start, end, str(raw.get("title", "")))


@dataclass(frozen=True)
class Calendar:
    owner: str
    entries: tuple[Entry, ...]

    @classmethod
    def from_entries(cls, owner: str,
                     raw: list[dict[str, Any]]) -> Calendar:
        entries = sorted((_entry(r) for r in raw),
                         key=lambda e: (e.start, e.end, e.title))
        return cls(owner, tuple(entries))

    def _within(self, start: datetime, end: datetime) -> list[Entry]:
        return [e for e in self.entries if e.overlaps(start, end)]

    def freebusy(self, start: datetime, end: datetime) -> list[dict]:
        return [e.busy() for e in self._within(start, end)]

    def details(self, start: datetime, end: datetime) -> list[dict]:
        return [{**e.busy(), "title": e.title}
                for e in self._within(start, end)]

    def is_free(self, start: datetime, end: datetime) -> bool:
        return not self._within(start, end)


def slot_starts(start: datetime, end: datetime, minutes: int,
                step: int) -> list[datetime]:
    """Every start in [start, end) at step minutes whose slot fits."""
    if step <= 0 or minutes <= 0:
        raise CalendarError("minutes and step must be positive")
    length = timedelta(minutes=minutes)
    starts, cursor = [], start
    while cursor + length <= end:
        starts.append(cursor)
        cursor += timedelta(minutes=step)
    return starts


def common_free_starts(calendars: list[Calendar], start: datetime,
                       end: datetime, minutes: int,
                       step: int = 30) -> list[datetime]:
    """Starts at which every calendar is free for the whole slot."""
    length = timedelta(minutes=minutes)
    return [s for s in slot_starts(start, end, minutes, step)
            if all(c.is_free(s, s + length) for c in calendars)]


class CalendarDesk:
    """The only way one agent reads another agent's calendar."""

    def __init__(self, engine, book: RelationshipBook,
                 calendars: dict[str, Calendar]):
        self.engine = engine
        self.book = book
        self.calendars = dict(calendars)

    def read(self, requester: str, owner: str, scope: str, start: str,
             end: str, now: float) -> list[dict]:
        if scope not in SCOPES:
            raise CalendarError(
                f"{scope!r} is not a calendar scope; use one of"
                f" {sorted(SCOPES)}")
        # Authorization comes first, so a stranger's request is recorded
        # and learns nothing, not even whether the owner has a calendar.
        decision = self.book.authorize(requester=requester, owner=owner,
                                       scope=scope, now=now)
        if not decision.allowed:
            raise CalendarError(
                f"{requester} may not read {owner}'s calendar under"
                f" {scope}: {decision.reason}")
        if owner not in self.calendars:
            raise CalendarError(f"{owner} has no calendar in the square")
        window = (parse_time(start, "start"), parse_time(end, "end"))
        if window[1] <= window[0]:
            raise CalendarError("a read window must end after it starts")
        calendar = self.calendars[owner]
        if scope == "calendar.details":
            return calendar.details(*window)
        return calendar.freebusy(*window)
