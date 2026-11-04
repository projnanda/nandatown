"""What the seller's evidence says when no ack of its work was accepted.

A stale fence is correct fencing, and a killed process is a real
failure, but the work behind the missing acknowledgement really
happened. These tests pin which redeliveries carry an application
record and which must not.
"""

import time

import pytest
from fastapi.testclient import TestClient

from nandatown import coordinator
from nandatown.client import TownClient
from nandatown.participants import seller
from nandatown.participants.base import Journal

from test_participants import ADMIN, make_town, quote_profile


class Killed(Exception):
    """Stand-in for the process dying before its acknowledgement lands."""


class LeaseClock:
    def __init__(self):
        self.now = time.time()

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def lease_clock(monkeypatch):
    clock = LeaseClock()
    # Isolate these modules instead of replacing time.time process-wide.
    # HTTP handling and journal I/O may take any amount of wall-clock time.
    monkeypatch.setattr(coordinator, "time", clock)
    monkeypatch.setattr(seller, "time", clock)
    return clock


def send_quote_request(app, run_id, tokens):
    buyer_client = TownClient("http://testserver", run_id,
                              http=TestClient(app))
    buyer_client.join("buyer", tokens["buyer"])
    buyer_client.send(message_id="q-1", to="seller", kind="quote_request",
                      body={"sku": "widget", "quantity": 2,
                            "unit_price_cents": 1995})


def seller_notes(admin, run_id):
    events = admin.get(f"/runs/{run_id}/events", headers=ADMIN).json()["events"]
    notes = [e["detail"]["note"] for e in events
             if e["kind"] == "ack_recorded" and e["observer"] == "seller"]
    return events, notes


def run_seller(app, run_id, tokens, state_dir, ack=None, deadline=3.0,
               clock=None):
    client = TownClient("http://testserver", run_id, http=TestClient(app))
    if ack is not None:
        client.ack = ack(client.ack)
    if clock is not None:
        notify, acknowledge = client.notify, client.ack

        def poll(wait):
            # Advance on every pass so a broken retry still reaches the
            # deadline. Keep the real inbox API, without a wall-clock wait.
            clock.advance(0.1)
            return notify(wait=0)

        def finish_after_ack(*args, **kwargs):
            result = acknowledge(*args, **kwargs)
            # The real loop still runs on_ack_accepted before checking its
            # deadline. A stale ack raises, leaving time for redelivery.
            clock.advance(deadline + 1)
            return result

        client.notify = poll
        client.ack = finish_after_ack
    seller.run(client, "seller", tokens["seller"], str(state_dir), "none",
               deadline_seconds=deadline)


def test_fenced_application_is_reported_on_redelivery(tmp_path, lease_clock):
    """A fenced applied acknowledgement plus a journal-backed
    redelivery still yields exactly one application record.

    The seller applied the work once; its acknowledgement lost the race
    with the lease. The record must carry the application it really
    performed, not blame the seller for the town's timing.
    """
    app, admin, run_id, tokens = make_town(tmp_path, lease=0.5)
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    def stall_once(inner):
        stalled = []

        def slow_ack(message_id, fence, status, note=None):
            # Expire only the first lease, independently of runner speed.
            if not stalled and (note or {}).get("applied"):
                stalled.append(True)
                lease_clock.advance(0.7)
            return inner(message_id, fence, status, note)

        return slow_ack

    run_seller(app, run_id, tokens, seller_dir, ack=stall_once,
               clock=lease_clock)

    events, notes = seller_notes(admin, run_id)
    assert [e for e in events if e["kind"] == "stale_fence_rejected"], events
    applied = [n for n in notes if n.get("applied")]
    assert len(applied) == 1, notes
    assert applied[0]["duplicate"] is True
    assert applied[0]["total_cents"] == 3990


def test_crash_between_apply_and_ack_is_reported(tmp_path, lease_clock):
    """A seller killed after applying but before its acknowledgement
    lands still reports the application on redelivery.

    Nothing observed the failure, so nothing could have written the mark
    afterwards: it has to have been committed with the application.
    """
    app, admin, run_id, tokens = make_town(tmp_path, lease=0.5)
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    def die(inner):
        def dying_ack(message_id, fence, status, note=None):
            raise Killed(message_id)

        return dying_ack

    with pytest.raises(Killed):
        run_seller(app, run_id, tokens, seller_dir, ack=die, clock=lease_clock)

    # The dead process still holds the lease; expire it so the
    # town redelivers the request to the restarted seller.
    lease_clock.advance(0.6)
    run_seller(app, run_id, tokens, seller_dir, clock=lease_clock)

    events, notes = seller_notes(admin, run_id)
    applied = [n for n in notes if n.get("applied")]
    assert len(applied) == 1, notes
    assert applied[0]["duplicate"] is True
    assert applied[0]["total_cents"] == 3990


def test_repeated_fences_still_yield_one_application_record(tmp_path, lease_clock):
    """Two acknowledgements refused in a row: the seller keeps carrying
    the application until one is accepted, and the record ends with
    exactly one.
    """
    app, admin, run_id, tokens = make_town(tmp_path, lease=0.5)
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    def stall_twice(inner):
        stalls = []

        def slow_ack(message_id, fence, status, note=None):
            if len(stalls) < 2 and (note or {}).get("applied"):
                stalls.append(True)
                lease_clock.advance(0.7)
            return inner(message_id, fence, status, note)

        return slow_ack

    run_seller(app, run_id, tokens, seller_dir, ack=stall_twice, deadline=4.0,
               clock=lease_clock)

    events, notes = seller_notes(admin, run_id)
    fences = [e for e in events if e["kind"] == "stale_fence_rejected"]
    assert len(fences) >= 2, events
    applied = [n for n in notes if n.get("applied")]
    assert len(applied) == 1, notes


def test_accepted_application_is_not_reported_twice(tmp_path):
    """When the applied acknowledgement was accepted, a later duplicate
    delivery is only a duplicate. Re-reporting the application there
    would read as two applications of the same work.
    """
    app, admin, run_id, tokens = make_town(tmp_path,
                                           fault="duplicate_delivery")
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    run_seller(app, run_id, tokens, seller_dir)

    events, notes = seller_notes(admin, run_id)
    assert [e for e in events if e["kind"] == "duplicate_offered"], events
    applied = [n for n in notes if n.get("applied")]
    duplicates = [n for n in notes if n.get("duplicate")]
    assert len(applied) == 1, notes
    assert len(duplicates) == 1, notes
    assert "applied" not in duplicates[0]


def test_crash_after_an_accepted_ack_is_not_two_applications(tmp_path):
    """The town recorded the application; only the seller forgot.

    An unreported mark means this seller never saw an acknowledgement
    accepted, which is not the same as the town holding no record. Here
    the acknowledgement was accepted and the process died before the
    mark could be cleared, so re-reporting on the duplicate delivery
    would make one application read as two.
    """
    app, admin, run_id, tokens = make_town(tmp_path,
                                           fault="duplicate_delivery")
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    def die_after_accept(inner):
        def ack(message_id, fence, status, note=None):
            result = inner(message_id, fence, status, note)
            assert result["recorded"] is True
            raise Killed("the acknowledgement landed; the mark did not")

        return ack

    with pytest.raises(Killed):
        run_seller(app, run_id, tokens, seller_dir, ack=die_after_accept)
    run_seller(app, run_id, tokens, seller_dir)

    events, notes = seller_notes(admin, run_id)
    assert [e for e in events if e["kind"] == "duplicate_offered"], events
    applied = [n for n in notes if n.get("applied")]
    assert len(applied) == 1, notes
    duplicates = [n for n in notes if n.get("duplicate")]
    assert len(duplicates) == 1, notes
    assert "applied" not in duplicates[0], notes


def test_a_run_ending_that_way_still_passes(tmp_path):
    """The evidence the evaluator reads, not just the notes.

    A false second application shows up as a failed run, so the buyer's
    side is completed here and the recorded events are evaluated.
    """
    from nandatown.evaluator import evaluate
    from nandatown.records import TownEvent

    app, admin, run_id, tokens = make_town(tmp_path,
                                           fault="duplicate_delivery")
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    def die_after_accept(inner):
        def ack(message_id, fence, status, note=None):
            result = inner(message_id, fence, status, note)
            raise Killed("the acknowledgement landed; the mark did not")

        return ack

    with pytest.raises(Killed):
        run_seller(app, run_id, tokens, seller_dir, ack=die_after_accept)
    run_seller(app, run_id, tokens, seller_dir)

    buyer = TownClient("http://testserver", run_id, http=TestClient(app))
    buyer.join("buyer", tokens["buyer"])
    claim, deadline = None, time.time() + 5
    while claim is None and time.time() < deadline:
        buyer.notify(wait=0.2)
        claim = buyer.claim()
    assert claim is not None
    buyer.ack(claim["message_id"], claim["fence"], "processed",
              {"correct": claim["body"]["total_cents"] == 3990,
               "total_cents": claim["body"]["total_cents"]})

    events, _ = seller_notes(admin, run_id)
    result = evaluate(quote_profile("duplicate_delivery"), run_id,
                      [TownEvent.model_validate(e) for e in events])
    detail = [(s.name, s.status, s.note) for s in result.stages]
    processed = next(s for s in result.stages if s.name == "processed")
    assert "applied 2 times" not in (processed.note or ""), detail
    assert processed.status == "passed", detail


def test_a_duplicate_delivery_means_the_town_recorded_an_ack(tmp_path):
    """The invariant the seller's suppression rests on.

    The seller stops reporting its application when a delivery is marked
    duplicate, because the town re-offers only work it has completed. If
    that ever stopped being true, a correct seller would be recorded as
    having no application record.
    """
    from nandatown.db import TownDB
    from nandatown.records import fingerprint

    body = {"sku": "widget", "quantity": 2, "unit_price_cents": 1995}
    db = TownDB(str(tmp_path / "town.db"))
    run_id = db.create_run(quote_profile().model_dump_json(), now=100.0)
    db.add_participant(run_id, "buyer", "buyer", [], "tok-b")
    db.add_participant(run_id, "seller", "seller", ["quote.read"], "tok-s")
    db.accept_message(run_id, "buyer", "q-1", "seller", "quote_request",
                      body, fingerprint(body), now=100.0)

    # Accepted, and then claimed, is not completed.
    assert db.reoffer(run_id, "q-1", "seller", 5.0, 101.0) is None
    claim = db.claim_next(run_id, "seller", 5.0, 102.0)
    assert db.reoffer(run_id, "q-1", "seller", 5.0, 103.0) is None

    db.ack(run_id, "seller", "q-1", claim["fence"], "processed",
           {"applied": True}, 104.0)
    offered = db.reoffer(run_id, "q-1", "seller", 5.0, 105.0)
    assert offered is not None and offered["duplicate"] is True


def test_a_settled_duplicate_is_not_reported_on_a_later_delivery(tmp_path):
    """Once suppressed, the decision stays made.

    The seller concluded from the duplicate delivery that the town holds
    its application. Nothing later should reopen that question, whatever
    brings the message back.
    """
    app, admin, run_id, tokens = make_town(tmp_path,
                                           fault="duplicate_delivery")
    send_quote_request(app, run_id, tokens)
    state = tmp_path / "seller"
    state.mkdir()

    def die_after_accept(inner):
        def ack(message_id, fence, status, note=None):
            inner(message_id, fence, status, note)
            raise Killed("the acknowledgement landed; the mark did not")

        return ack

    with pytest.raises(Killed):
        run_seller(app, run_id, tokens, state, ack=die_after_accept)
    run_seller(app, run_id, tokens, state)

    journal = Journal(str(state / "journal.db"))
    assert journal.seen("q-1")
    assert journal.unreported("q-1") is False
