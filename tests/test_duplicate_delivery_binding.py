"""The duplicate this profile injects, told apart from a redelivery.

A seller that loses its lease is redelivered the request and, since #278,
acknowledges that redelivery as a duplicate that also carries the
application it performed. That acknowledgement is not the injected
duplicate: the town has not offered it yet. Completion and evaluation
must both wait for, and bind to, an acknowledgement of the injected
delivery itself.
"""

import shlex
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nandatown.bundle import verify_bundle
from nandatown.client import TownClient
from nandatown.evaluator import EVALUATOR_VERSION, evaluate
from nandatown.participants import seller
from nandatown.profiles import PROFILES
from nandatown.records import TownEvent
from nandatown.runner import _quiescent, run_town

from test_evaluator import clean_events, ev, profile, stage
from test_participants import ADMIN, make_town, quote_profile

FIXTURES = Path(__file__).parent / "fixtures"


class Left(Exception):
    """The seller process going away after an acknowledgement."""


def events_of(admin, run_id):
    return admin.get(f"/runs/{run_id}/events", headers=ADMIN).json()["events"]


def lease_losing_town(tmp_path):
    (tmp_path / "seller").mkdir(exist_ok=True)
    app, admin, run_id, tokens = make_town(tmp_path,
                                           fault="duplicate_delivery",
                                           lease=0.5)
    buyer = TownClient("http://testserver", run_id, http=TestClient(app))
    buyer.join("buyer", tokens["buyer"])
    buyer.send(message_id="q-1", to="seller", kind="quote_request",
               body={"sku": "widget", "quantity": 2,
                     "unit_price_cents": 1995})
    return app, admin, run_id, tokens, buyer


def slow_once(inner, after=None):
    stalled = []

    def ack(message_id, fence, status, note=None):
        if not stalled and (note or {}).get("applied"):
            stalled.append(True)
            time.sleep(0.7)
        result = inner(message_id, fence, status, note)
        if after:
            after(message_id, fence, status, note or {})
        return result

    return ack


def settle_buyer(buyer):
    claim = None
    for _ in range(60):
        buyer.notify(wait=0.05)
        claim = buyer.claim()
        if claim:
            break
    buyer.ack(claim["message_id"], claim["fence"], "processed",
              {"correct": True, "total_cents": 3990})


def test_the_town_records_which_delivery_it_injected(tmp_path):
    app, admin, run_id, tokens, buyer = lease_losing_town(tmp_path)
    client = TownClient("http://testserver", run_id, http=TestClient(app))
    client.join("seller", tokens["seller"])
    first = None
    for _ in range(40):
        client.notify(wait=0.05)
        first = client.claim()
        if first:
            break
    client.ack("q-1", first["fence"], "processed", {"applied": True})

    offer = client.claim()

    assert offer["duplicate"] is True
    offered = [e for e in events_of(admin, run_id)
               if e["kind"] == "duplicate_offered"]
    assert [(e["detail"]["fence"], e["detail"]["attempt"])
            for e in offered] == [(offer["fence"], offer["attempt"])]


def test_a_redelivery_acknowledged_as_duplicate_does_not_end_the_run(
        tmp_path):
    """The runner would stop here, before the duplicate was ever offered."""
    app, admin, run_id, tokens, buyer = lease_losing_town(tmp_path)
    profile_ = quote_profile("duplicate_delivery", 0.5)
    seen = []

    def record(message_id, fence, status, note):
        events = events_of(admin, run_id)
        seen.append((dict(note), _quiescent(profile_, events),
                     any(e["kind"] == "duplicate_offered" for e in events)))

    client = TownClient("http://testserver", run_id, http=TestClient(app))
    client.ack = slow_once(client.ack, after=record)
    seller.run(client, "seller", tokens["seller"],
               str(tmp_path / "seller"), "none", deadline_seconds=3.0)

    combined = [s for s in seen if s[0].get("applied") and s[0].get("duplicate")]
    bound = [s for s in seen if s[0] == {"duplicate": True}]
    assert combined and bound, seen
    assert combined[0][1:] == (False, False), seen
    assert bound[-1][1:] == (True, True), seen


def test_an_injected_duplicate_nobody_acknowledged_is_not_recognized(
        tmp_path):
    """The earlier acknowledgement must not stand in for the later offer."""
    app, admin, run_id, tokens, buyer = lease_losing_town(tmp_path)

    def leave_after_the_combined_ack(message_id, fence, status, note):
        if note.get("applied") and note.get("duplicate"):
            raise Left()

    client = TownClient("http://testserver", run_id, http=TestClient(app))
    client.ack = slow_once(client.ack, after=leave_after_the_combined_ack)
    with pytest.raises(Left):
        seller.run(client, "seller", tokens["seller"],
                   str(tmp_path / "seller"), "none", deadline_seconds=3.0)
    restarted = TownClient("http://testserver", run_id, http=TestClient(app))
    restarted.join("seller", tokens["seller"])
    offer = None
    for _ in range(40):
        restarted.notify(wait=0.05)
        offer = restarted.claim()
        if offer:
            break
    assert offer is not None and offer["duplicate"] is True
    settle_buyer(buyer)

    events = [TownEvent.model_validate(e) for e in events_of(admin, run_id)]
    result = evaluate(quote_profile("duplicate_delivery", 0.5), run_id, events)

    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert stage(result, "duplicate_recognized").status != "passed", detail
    assert result.verdict != "passed", detail


def test_a_seller_that_acknowledges_the_injected_duplicate_passes(tmp_path):
    app, admin, run_id, tokens, buyer = lease_losing_town(tmp_path)
    client = TownClient("http://testserver", run_id, http=TestClient(app))
    client.ack = slow_once(client.ack)
    seller.run(client, "seller", tokens["seller"],
               str(tmp_path / "seller"), "none", deadline_seconds=3.0)
    settle_buyer(buyer)

    raw = events_of(admin, run_id)
    result = evaluate(quote_profile("duplicate_delivery", 0.5), run_id,
                      [TownEvent.model_validate(e) for e in raw])

    detail = [(s.name, s.status, s.note) for s in result.stages]
    recognized = stage(result, "duplicate_recognized")
    assert recognized.status == "passed", detail
    offer = next(e for e in raw if e["kind"] == "duplicate_offered")
    bound = next(e for e in raw if e["kind"] == "ack_recorded"
                 and e["detail"].get("fence") == offer["detail"]["fence"])
    assert recognized.evidence == [offer["event_id"], bound["event_id"]]
    assert result.verdict == "passed", detail


def test_the_runner_waits_for_the_injected_duplicate_after_a_lost_lease(
        tmp_path, monkeypatch):
    """Real coordinator, real seller and buyer processes."""
    short = PROFILES["quote-duplicate-delivery"].model_copy(update={
        "name": "quote-duplicate-short-lease", "lease_seconds": 0.5})
    monkeypatch.setitem(PROFILES, short.name, short)
    spec = "cmd:" + shlex.join([sys.executable,
                                str(FIXTURES / "lease_losing_seller.py")])

    bundle_dir, result = run_town(short.name, str(tmp_path),
                                  harnesses={"seller": spec},
                                  wait_timeout=30)

    detail = [(s.name, s.status, s.note) for s in result.stages]
    from nandatown.bundle import load_bundle
    kinds = [e.kind for e in load_bundle(bundle_dir)["events"]]
    assert "stale_fence_rejected" in kinds, kinds
    assert stage(result, "duplicate_recognized").status == "passed", detail
    assert result.verdict == "passed", detail
    assert result.evaluator_version == EVALUATOR_VERSION
    assert verify_bundle(bundle_dir) == []


def offered(fence="fence-offer"):
    detail = {"fault": "duplicate_delivery"}
    if fence is not None:
        detail.update(fence=fence, attempt=2)
    return ev(11, "duplicate_offered", "q-1", **detail)


def acknowledged(fence):
    return ev(12, "ack_recorded", "q-1", observer="seller",
              status="processed", note={"duplicate": True}, fence=fence,
              attempt=2)


def test_a_duplicate_acknowledgement_under_another_fence_is_not_bound():
    events = clean_events() + [offered(), acknowledged("fence-redelivery")]

    result = evaluate(profile("duplicate_delivery"), "run-1", events)

    assert stage(result, "duplicate_recognized").status != "passed"


def test_an_offer_a_bundle_did_not_record_a_fence_for_cannot_be_bound():
    """Only a coordinator before 0.5.0 wrote such an offer, and its bundle
    replays under its own evaluator; 0.5.0 will not guess."""
    events = clean_events() + [offered(fence=None), acknowledged(None)]

    current = evaluate(profile("duplicate_delivery"), "run-1", events)
    replayed = evaluate(profile("duplicate_delivery"), "run-1", events,
                        version="0.4.0")

    assert stage(current, "duplicate_recognized").status != "passed"
    assert stage(replayed, "duplicate_recognized").status == "passed"


@pytest.mark.parametrize("status", ["retryable", "received", "rejected",
                                    "failed"])
def test_only_a_processed_acknowledgement_of_the_offer_recognizes_it(status):
    """The runner counts a duplicate as handled only when it is processed,
    and the protocol asks sellers to acknowledge one that way. A retryable
    acknowledgement is provisional besides; the evaluator must not pass a
    run the runner is still waiting on."""
    events = clean_events() + [offered(), ev(
        12, "ack_recorded", "q-1", observer="seller", status=status,
        note={"duplicate": True}, fence="fence-offer", attempt=2)]

    result = evaluate(profile("duplicate_delivery"), "run-1", events)

    assert stage(result, "duplicate_recognized").status != "passed"
    assert _quiescent(profile("duplicate_delivery"),
                      [e.model_dump() for e in events]) is False


@pytest.mark.parametrize("status", ["retryable", "received"])
def test_earlier_versions_still_count_any_status(status):
    events = clean_events() + [offered(fence=None), ev(
        12, "ack_recorded", "q-1", observer="seller", status=status,
        note={"duplicate": True}, attempt=2)]

    replayed = evaluate(profile("duplicate_delivery"), "run-1", events,
                        version="0.4.0")

    assert stage(replayed, "duplicate_recognized").status == "passed"


def test_a_run_the_town_never_offered_a_duplicate_in_says_so():
    """A seller that left before the offer was never offered anything."""
    events = clean_events() + [acknowledged("fence-redelivery")]

    note = stage(evaluate(profile("duplicate_delivery"), "run-1", events),
                 "duplicate_recognized").note

    assert "never offered" in note, note
    assert "never acknowledged" not in note


@pytest.mark.parametrize("fence", [["fence-offer"], {"f": 1}, 7])
def test_a_crafted_acknowledgement_fence_cannot_break_evaluation(fence):
    events = clean_events() + [offered(), ev(
        12, "ack_recorded", "q-1", observer="seller", status="processed",
        note={"duplicate": True}, fence=fence, attempt=2)]

    result = evaluate(profile("duplicate_delivery"), "run-1", events)

    assert stage(result, "duplicate_recognized").status != "passed"
