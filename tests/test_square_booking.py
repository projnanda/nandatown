from datetime import datetime

import pytest

from nandatown.square.booking import BookingError, MoonRestaurant
from nandatown.square.slots import NegotiationError, SlotNegotiation

DAY = "2026-10-10"


def at(hhmm):
    return datetime.fromisoformat(f"{DAY}T{hhmm}")


def iso(hhmm):
    return f"{DAY}T{hhmm}"


class RecordingEngine:
    def __init__(self):
        self.events = []

    def emit(self, observer, kind, subject, detail=None):
        self.events.append({"observer": observer, "kind": kind,
                            "subject": subject, "detail": detail or {}})

    def kinds(self):
        return [e["kind"] for e in self.events]


# Made-up schedule: t1 seats 2 and is taken 19:00-20:30; t2 seats 4 and is
# taken 18:30-20:00. Party-of-two starts: 17:00, 17:30, 20:00, 20:30.
CONFIG = {
    "name": "moon-restaurant",
    "opens": "17:00", "closes": "22:00",
    "seating_minutes": 90, "step_minutes": 30,
    "tables": [{"table_id": "t2", "seats": 4}, {"table_id": "t1", "seats": 2}],
    "reservations": [
        {"table_id": "t1", "start": iso("19:00"), "party": 2},
        {"table_id": "t2", "start": iso("18:30"), "party": 3},
    ],
}


@pytest.fixture()
def moon():
    engine = RecordingEngine()
    return MoonRestaurant.from_config(engine, CONFIG), engine


# -- the restaurant's schedule ------------------------------------------------


def test_available_starts_follow_the_made_up_schedule(moon):
    r, _ = moon
    assert r.available_starts(DAY, party=2) == [
        at("17:00"), at("17:30"), at("20:00"), at("20:30")]
    assert r.available_starts(DAY, party=4) == [
        at("17:00"), at("20:00"), at("20:30")]
    assert r.available_starts(DAY, party=5) == []


def test_the_smallest_table_that_fits_is_chosen(moon):
    r, _ = moon
    assert r.table_for(at("17:00"), party=2).table_id == "t1"
    assert r.table_for(at("17:00"), party=3).table_id == "t2"
    assert r.table_for(at("19:00"), party=2) is None


@pytest.mark.parametrize("hhmm", ["16:30", "21:00", "17:15"])
def test_no_seating_outside_hours_or_off_the_grid(moon, hhmm):
    r, _ = moon
    assert r.table_for(at(hhmm), party=2) is None


def test_a_hold_takes_the_table_until_released(moon):
    r, engine = moon
    first = r.hold("did:town:a", at("17:00"), party=2)
    second = r.hold("did:town:b", at("17:00"), party=2)
    assert (first.table_id, second.table_id) == ("t1", "t2")
    assert r.table_for(at("17:00"), party=2) is None
    with pytest.raises(BookingError, match="no table"):
        r.hold("did:town:c", at("17:00"), party=2)
    r.release(first.ref)
    assert r.table_for(at("17:00"), party=2).table_id == "t1"
    assert engine.kinds() == ["table_held", "table_held",
                              "table_released"]


def test_a_held_table_is_confirmed_once_with_a_payment(moon):
    r, engine = moon
    held = r.hold("did:town:a", at("20:00"), party=2)
    confirmed = r.confirm(held.ref, payment_ref="pay-1")
    assert confirmed.state == "confirmed"
    assert confirmed.payment_ref == "pay-1"
    assert held.state == "held"
    with pytest.raises(BookingError, match="not held"):
        r.confirm(held.ref, payment_ref="pay-2")
    assert engine.events[-1]["detail"]["payment_ref"] == "pay-1"


def test_unknown_references_are_refused(moon):
    r, _ = moon
    with pytest.raises(BookingError, match="unknown"):
        r.confirm("res-404", payment_ref="pay")
    with pytest.raises(BookingError, match="unknown"):
        r.release("res-404")


@pytest.mark.parametrize("change, message", [
    ({"tables": [{"table_id": "t1", "seats": 0}]}, "seats"),
    ({"tables": [{"table_id": "t1", "seats": True}]}, "seats"),
    ({"tables": [{"table_id": "t1", "seats": 2},
                 {"table_id": "t1", "seats": 4}]}, "twice"),
    ({"reservations": [{"table_id": "t9", "start": iso("19:00"),
                        "party": 2}]}, "unknown table"),
    ({"opens": "22:00", "closes": "17:00"}, "close after"),
    ({"seating_minutes": 0}, "positive"),
])
def test_bad_restaurant_config_is_refused(change, message):
    with pytest.raises(BookingError, match=message):
        MoonRestaurant.from_config(RecordingEngine(), {**CONFIG, **change})


# -- negotiation: Agent A with booking agent C ---------------------------------


@pytest.fixture()
def talks(moon):
    r, engine = moon
    return SlotNegotiation(engine, r, max_rounds=2), r, engine


def test_an_available_proposal_is_accepted_and_held(talks):
    n, r, engine = talks
    nid = n.start("did:town:a", party=2, day=DAY)
    outcome = n.propose(nid, by="did:town:a",
                        slots=[iso("19:00"), iso("20:00")])
    assert outcome.state == "agreed"
    assert outcome.slot == iso("20:00")
    assert r.reservation(outcome.reservation_ref).state == "held"
    assert engine.kinds()[:3] == ["slot_negotiation_started",
                                  "slots_proposed", "table_held"]
    assert engine.kinds()[-1] == "slot_agreed"


def test_a_booked_proposal_gets_the_nearest_counters(talks):
    n, _, engine = talks
    nid = n.start("did:town:a", party=2, day=DAY)
    outcome = n.propose(nid, by="did:town:a", slots=[iso("19:00")])
    assert outcome.state == "countered"
    assert outcome.counters == [iso("20:00"), iso("17:30"), iso("20:30")]
    assert engine.kinds()[-1] == "slots_countered"


def test_the_guest_can_accept_a_counter(talks):
    n, r, _ = talks
    nid = n.start("did:town:a", party=2, day=DAY)
    n.propose(nid, by="did:town:a", slots=[iso("19:00")])
    outcome = n.accept(nid, by="did:town:a", slot=iso("17:30"))
    assert outcome.state == "agreed"
    assert r.reservation(outcome.reservation_ref).table_id == "t1"


def test_only_a_countered_slot_can_be_accepted(talks):
    n, _, _ = talks
    nid = n.start("did:town:a", party=2, day=DAY)
    n.propose(nid, by="did:town:a", slots=[iso("19:00")])
    with pytest.raises(NegotiationError, match="not offered"):
        n.accept(nid, by="did:town:a", slot=iso("17:00"))


def test_only_the_guest_negotiates(talks):
    n, _, _ = talks
    nid = n.start("did:town:a", party=2, day=DAY)
    with pytest.raises(NegotiationError, match="guest"):
        n.propose(nid, by="did:town:b", slots=[iso("20:00")])


def test_slots_on_another_day_are_refused(talks):
    n, _, _ = talks
    nid = n.start("did:town:a", party=2, day=DAY)
    with pytest.raises(NegotiationError, match="day"):
        n.propose(nid, by="did:town:a", slots=["2026-10-11T20:00"])


def test_rounds_are_bounded_and_the_last_counter_can_be_accepted(talks):
    n, _, engine = talks
    nid = n.start("did:town:a", party=2, day=DAY)
    n.propose(nid, by="did:town:a", slots=[iso("19:00")])
    last = n.propose(nid, by="did:town:a", slots=[iso("19:30")])
    assert last.state == "countered"
    outcome = n.propose(nid, by="did:town:a", slots=[iso("19:00")])
    assert outcome.state == "failed"
    assert outcome.reason == "out_of_rounds"
    assert engine.kinds()[-1] == "slot_negotiation_failed"
    with pytest.raises(NegotiationError, match="failed"):
        n.propose(nid, by="did:town:a", slots=[iso("20:00")])


def test_a_fully_booked_day_fails_without_counters(talks):
    n, _, _ = talks
    nid = n.start("did:town:a", party=6, day=DAY)
    outcome = n.propose(nid, by="did:town:a", slots=[iso("20:00")])
    assert (outcome.state, outcome.reason) == ("failed", "fully_booked")


def test_an_agreed_negotiation_is_closed(talks):
    n, _, _ = talks
    nid = n.start("did:town:a", party=2, day=DAY)
    n.propose(nid, by="did:town:a", slots=[iso("20:00")])
    with pytest.raises(NegotiationError, match="agreed"):
        n.propose(nid, by="did:town:a", slots=[iso("20:30")])


def test_unknown_negotiations_are_refused(talks):
    n, _, _ = talks
    with pytest.raises(NegotiationError, match="unknown"):
        n.propose("slots-9", by="did:town:a", slots=[iso("20:00")])


# -- review hardening -------------------------------------------------------


def test_a_time_zone_slot_is_refused_and_the_restaurant_keeps_working(talks):
    n, r, _ = talks
    nid = n.start("did:town:a", party=2, day=DAY)
    with pytest.raises(NegotiationError, match="time zone"):
        n.propose(nid, by="did:town:a", slots=[f"{DAY}T20:00+00:00"])
    assert r.available_starts(DAY, party=2)
    assert n.propose(nid, by="did:town:a",
                     slots=[iso("20:00")]).state == "agreed"


def test_a_time_zone_reservation_in_config_is_refused():
    raw = [{"table_id": "t1", "start": f"{DAY}T19:00+02:00", "party": 2}]
    with pytest.raises(BookingError, match="time zone"):
        MoonRestaurant.from_config(RecordingEngine(),
                                   {**CONFIG, "reservations": raw})


def test_a_guest_holds_one_table_at_a_time(talks):
    n, r, _ = talks
    first = n.start("did:town:evil", party=2, day=DAY)
    agreed = n.propose(first, by="did:town:evil", slots=[iso("17:00")])
    with pytest.raises(NegotiationError, match="already holds"):
        n.start("did:town:evil", party=2, day=DAY)
    with pytest.raises(BookingError, match="already holds"):
        r.hold("did:town:evil", at("20:00"), party=2)
    r.release(agreed.reservation_ref)
    assert n.start("did:town:evil", party=2, day=DAY)


@pytest.mark.parametrize("party, day, message", [
    (0, DAY, "party"), ("2", DAY, "party"), (2, "tomorrow", "day")])
def test_start_validates_party_and_day(talks, party, day, message):
    n, _, _ = talks
    with pytest.raises(NegotiationError, match=message):
        n.start("did:town:a", party=party, day=day)


def test_accept_compares_times_not_spellings(talks):
    n, _, _ = talks
    nid = n.start("did:town:a", party=2, day=DAY)
    n.propose(nid, by="did:town:a", slots=[iso("19:00")])
    outcome = n.accept(nid, by="did:town:a", slot=f"{DAY}T20:00:00")
    assert outcome.state == "agreed" and outcome.slot == iso("20:00")
