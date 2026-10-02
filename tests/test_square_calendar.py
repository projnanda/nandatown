from datetime import datetime
from functools import partial

import pytest

from nandatown.identity_portable import Keystore, resolve_file
from nandatown.square.calendar import (
    Calendar,
    CalendarDesk,
    CalendarError,
    common_free_starts,
)
from nandatown.square.relationships import (
    RelationshipBook,
    relationship_terms,
)

DAY = "2026-10-10"


def at(hhmm):
    return datetime.fromisoformat(f"{DAY}T{hhmm}")


class RecordingEngine:
    def __init__(self):
        self.events = []

    def emit(self, observer, kind, subject, detail=None):
        self.events.append({"observer": observer, "kind": kind,
                            "subject": subject, "detail": detail or {}})


ALICE = [{"start": f"{DAY}T17:00", "end": f"{DAY}T18:30",
          "title": "Dentist"},
         {"start": f"{DAY}T20:30", "end": f"{DAY}T21:00",
          "title": "Call with mom"}]
BOB = [{"start": f"{DAY}T18:00", "end": f"{DAY}T19:00",
        "title": "Gym"}]


# -- the calendar itself ------------------------------------------------------


def test_freebusy_hides_titles_and_details_keep_them():
    cal = Calendar.from_entries("a", ALICE)
    window = (at("16:00"), at("22:00"))
    assert cal.freebusy(*window) == [
        {"start": f"{DAY}T17:00", "end": f"{DAY}T18:30"},
        {"start": f"{DAY}T20:30", "end": f"{DAY}T21:00"}]
    assert [e["title"] for e in cal.details(*window)] == [
        "Dentist", "Call with mom"]


def test_only_entries_overlapping_the_window_are_returned():
    cal = Calendar.from_entries("a", ALICE)
    assert cal.freebusy(at("19:00"), at("20:00")) == []
    assert len(cal.freebusy(at("18:00"), at("18:15"))) == 1


def test_is_free_treats_touching_intervals_as_free():
    cal = Calendar.from_entries("a", ALICE)
    assert cal.is_free(at("18:30"), at("20:30"))
    assert not cal.is_free(at("18:00"), at("19:00"))


def test_entries_are_sorted_and_immutable():
    cal = Calendar.from_entries("a", list(reversed(ALICE)))
    assert [e.title for e in cal.entries] == ["Dentist", "Call with mom"]
    with pytest.raises(AttributeError):
        cal.entries.append(None)


@pytest.mark.parametrize("entry, message", [
    ({"start": f"{DAY}T19:00", "end": f"{DAY}T18:00"}, "end after"),
    ({"start": "tonight", "end": f"{DAY}T18:00"}, "ISO"),
    ({"end": f"{DAY}T18:00"}, "start"),
])
def test_bad_entries_are_refused(entry, message):
    with pytest.raises(CalendarError, match=message):
        Calendar.from_entries("a", [entry])


def test_common_free_starts_respect_every_calendar():
    cals = [Calendar.from_entries("a", ALICE),
            Calendar.from_entries("b", BOB)]
    starts = common_free_starts(cals, at("17:00"), at("22:00"),
                                minutes=90, step=30)
    assert starts == [at("19:00")]


def test_common_free_starts_rejects_a_bad_step():
    with pytest.raises(CalendarError, match="step"):
        common_free_starts([], at("17:00"), at("22:00"), minutes=90, step=0)


# -- the desk: calendar reads are data requests ---------------------------


@pytest.fixture()
def desk(tmp_path):
    ks = Keystore(str(tmp_path))
    a = ks.new_identity("instinct")["agent_id"]
    b = ks.new_identity("muse")["agent_id"]
    c = ks.new_identity("openclaw")["agent_id"]
    engine = RecordingEngine()
    book = RelationshipBook(engine, partial(resolve_file, ks.registry_path))
    terms = relationship_terms(a, b, grants={a: ["calendar.freebusy"],
                                             b: ["calendar.freebusy"]},
                               issued_at=0.0, expires_at=100.0, nonce="d")
    book.establish(terms, {a: ks.sign("instinct", terms),
                           b: ks.sign("muse", terms)}, now=1.0)
    calendars = {a: Calendar.from_entries(a, ALICE),
                 b: Calendar.from_entries(b, BOB)}
    return CalendarDesk(engine, book, calendars), engine, a, b, c


WINDOW = {"start": f"{DAY}T16:00", "end": f"{DAY}T22:00"}


def test_a_related_agent_reads_freebusy(desk):
    d, _, a, b, _ = desk
    busy = d.read(requester=a, owner=b, scope="calendar.freebusy",
                  now=2.0, **WINDOW)
    assert busy == [{"start": f"{DAY}T18:00", "end": f"{DAY}T19:00"}]


def test_asking_for_details_without_the_grant_is_refused(desk):
    d, engine, a, b, _ = desk
    with pytest.raises(CalendarError, match="scope_not_granted"):
        d.read(requester=a, owner=b, scope="calendar.details", now=2.0,
               **WINDOW)
    assert engine.events[-1]["kind"] == "unauthorized_data_request"


def test_a_stranger_cannot_read_a_calendar(desk):
    d, _, _, b, c = desk
    with pytest.raises(CalendarError, match="no_relationship"):
        d.read(requester=c, owner=b, scope="calendar.freebusy", now=2.0,
               **WINDOW)


def test_an_unknown_scope_is_refused_before_authorizing(desk):
    d, engine, a, b, _ = desk
    before = len(engine.events)
    with pytest.raises(CalendarError, match="calendar scope"):
        d.read(requester=a, owner=b, scope="payment.card", now=2.0,
               **WINDOW)
    assert len(engine.events) == before


def test_an_agent_without_a_calendar_has_nothing_to_read(desk):
    d, engine, a, b, _ = desk
    bare = CalendarDesk(engine, d.book, {a: d.calendars[a]})
    with pytest.raises(CalendarError, match="no calendar"):
        bare.read(requester=a, owner=b, scope="calendar.freebusy", now=2.0,
                  **WINDOW)


def test_a_stranger_learns_nothing_and_is_recorded_first(desk):
    d, engine, _, _, c = desk
    for owner in ["did:town:nobody", c]:
        with pytest.raises(CalendarError, match="no_relationship"):
            d.read(requester=c if owner != c else "did:town:x", owner=owner,
                   scope="calendar.freebusy", now=2.0, start="garbage",
                   end="garbage")
    kinds = [e["kind"] for e in engine.events]
    assert kinds[-2:] == ["unauthorized_data_request"] * 2


def test_an_inverted_window_is_refused(desk):
    d, _, a, b, _ = desk
    with pytest.raises(CalendarError, match="end after"):
        d.read(requester=a, owner=b, scope="calendar.freebusy", now=2.0,
               start=f"{DAY}T22:00", end=f"{DAY}T16:00")


@pytest.mark.parametrize("entry", [
    {"start": f"{DAY}T17:00+00:00", "end": f"{DAY}T18:00+00:00"},
    {"start": f"{DAY}T17:00", "end": f"{DAY}T18:00Z"}])
def test_time_zone_entries_are_refused(entry):
    with pytest.raises(CalendarError, match="time zone"):
        Calendar.from_entries("a", [entry])
