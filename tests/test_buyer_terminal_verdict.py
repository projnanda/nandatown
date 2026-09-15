"""Only the buyer's terminal acknowledgement decides `correct`.

A `retryable` acknowledgement is provisional: the response goes back to the
buyer's inbox and the buyer has not settled. What it asserted then must not
outweigh what it asserts when it does settle, and an assertion it never
committed to cannot decide the stage on its own.
"""

import pathlib
import shlex
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

from nandatown.bundle import verify_bundle
from nandatown.client import TownClient
from nandatown.evaluator import evaluate
from nandatown.records import TownEvent
from nandatown.runner import run_town

from test_evaluator import clean_events, ev, profile, stage
from test_participants import ADMIN, make_town, quote_profile

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def claim_one(client):
    for _ in range(60):
        client.notify(wait=0.05)
        got = client.claim()
        if got:
            return got


def through_the_coordinator(sequence):
    """The buyer acknowledges the one quote response in each (status, note)."""
    app, admin, run_id, tokens = make_town(pathlib.Path(tempfile.mkdtemp()))
    buyer = TownClient("http://testserver", run_id, http=TestClient(app))
    buyer.join("buyer", tokens["buyer"])
    seller = TownClient("http://testserver", run_id, http=TestClient(app))
    seller.join("seller", tokens["seller"])
    buyer.send(message_id="q-1", to="seller", kind="quote_request",
               body={"sku": "widget", "quantity": 2, "unit_price_cents": 1995})
    request = claim_one(seller)
    seller.send(message_id="r-1", to="buyer", kind="quote_response",
                body={"request_id": "q-1", "total_cents": 3990})
    seller.ack("q-1", request["fence"], "processed",
               {"applied": True, "total_cents": 3990})
    for status, note in sequence:
        response = claim_one(buyer)
        assert response is not None, f"no response to acknowledge {status}"
        buyer.ack(response["message_id"], response["fence"], status, note)
    events = admin.get(f"/runs/{run_id}/events", headers=ADMIN).json()["events"]
    return evaluate(quote_profile(), run_id,
                    [TownEvent.model_validate(e) for e in events])


RIGHT, WRONG = {"correct": True, "total_cents": 3990}, {"correct": False,
                                                         "total_cents": 3990}


def test_a_terminal_false_is_not_outweighed_by_a_provisional_true():
    result = through_the_coordinator([("retryable", RIGHT),
                                      ("processed", WRONG)])

    assert stage(result, "correct").status == "failed"
    assert result.verdict == "failed"


def test_a_terminal_true_is_not_outweighed_by_a_provisional_false():
    result = through_the_coordinator([("retryable", WRONG),
                                      ("processed", RIGHT)])

    assert stage(result, "correct").status == "passed"


def test_a_provisional_assertion_alone_decides_nothing():
    result = through_the_coordinator([("retryable", RIGHT)])

    correct = stage(result, "correct")
    assert correct.status == "not_enough_evidence", correct
    assert "provisional" in correct.note
    assert result.verdict != "passed"


def test_a_terminal_acknowledgement_that_asserts_nothing_is_not_filled_in():
    """The provisional assertion must not stand in for a silent decision."""
    result = through_the_coordinator([("retryable", RIGHT),
                                      ("processed", {"total_cents": 3990})])

    correct = stage(result, "correct")
    assert correct.status == "not_enough_evidence", correct
    assert "terminal" in correct.note
    assert result.verdict != "passed"


@pytest.mark.parametrize("status, note, expected", [
    ("processed", RIGHT, "passed"),
    ("received", RIGHT, "passed"),
    ("rejected", WRONG, "failed"),
    ("failed", WRONG, "failed"),
])
def test_every_status_but_retryable_is_terminal(status, note, expected):
    """The same line the runner draws when it decides the buyer settled."""
    result = through_the_coordinator([(status, note)])

    assert stage(result, "correct").status == expected


def test_disagreeing_terminal_assertions_on_one_response_decide_nothing():
    """The coordinator settles a response at its first terminal
    acknowledgement, so only a crafted record carries two."""
    events = clean_events()[:-1] + [
        ev(20, "ack_recorded", "r-1", observer="buyer", status="processed",
           note=WRONG, attempt=2),
        ev(21, "run_finished", "run-1"),
    ]

    correct = stage(evaluate(profile(), "run-1", events), "correct")

    assert correct.status == "not_enough_evidence", correct


@pytest.mark.parametrize("version", ["0.2.0", "0.3.0", "0.4.0"])
def test_earlier_versions_still_read_the_first_assertion(version):
    events = clean_events()
    provisional = ev(9, "ack_recorded", "r-1", observer="buyer",
                     status="retryable", note=RIGHT, attempt=1)
    terminal = ev(12, "ack_recorded", "r-1", observer="buyer",
                  status="processed", note=WRONG, attempt=2)
    events = events[:8] + [provisional,
                           ev(11, "message_claimed", "r-1", claimant="buyer",
                              attempt=2),
                           terminal, events[-1]]

    replayed = evaluate(profile(), "run-1", events, version=version)

    assert stage(replayed, "correct").status == "passed"


def test_a_buyer_process_that_changes_its_mind_fails_the_run(tmp_path):
    """Real coordinator, stock seller, and a buyer process of its own."""
    spec = "cmd:" + shlex.join([sys.executable,
                                str(FIXTURES / "second_thoughts_buyer.py"),
                                "true", "false"])

    bundle_dir, result = run_town("quote-clean", str(tmp_path),
                                  harnesses={"buyer": spec}, wait_timeout=30)

    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert stage(result, "correct").status == "failed", detail
    assert result.verdict == "failed", detail
    assert verify_bundle(bundle_dir) == []


@pytest.mark.parametrize("order", ["true-then-false", "false-then-true"])
def test_disagreeing_terminal_assertions_decide_nothing_in_either_order(
        order):
    """Even where the response names no request, order must not decide."""
    first, second = (RIGHT, WRONG) if order == "true-then-false" else (
        WRONG, RIGHT)
    base = clean_events()
    # The response names no request, so it cannot be shown to answer one.
    base[5] = base[5].model_copy(update={"detail": {
        k: v for k, v in base[5].detail.items() if k != "request_id"}})
    events = base[:8] + [
        ev(9, "ack_recorded", "r-1", observer="buyer", status="processed",
           note=first, attempt=1),
        ev(10, "ack_recorded", "r-1", observer="buyer", status="processed",
           note=second, attempt=2),
        ev(11, "run_finished", "run-1"),
    ]

    correct = stage(evaluate(profile(), "run-1", events), "correct")

    assert correct.status == "not_enough_evidence", correct
