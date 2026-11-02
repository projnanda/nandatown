"""A `correct` assertion its own run contradicts does not pass.

Under 0.5.0 the `correct` stage passed on the buyer's boolean alone. A
seller that quoted 3999 cents for a 3990-cent task, and a buyer that said
`correct: true` without comparing, produced a run whose every stage
passed, whose bundle verified, and whose two acknowledgements both
recorded the 3999-cent total beside that verdict.

0.6.0 reads the totals the participants recorded with their
acknowledgements. A buyer whose own terminal acknowledgement records a
total, or an expected total, other than the task's cannot pass `correct`
with it: that record is the buyer's, and it contradicts the assertion.
Where only the seller's acknowledgement records a different total, the
participants disagree and the town did not record the response's total,
so `correct` is inconclusive rather than a choice between them.
"""

import pathlib
import sys

import pytest

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.evaluator import evaluate
from nandatown.runner import run_town

from test_evaluator import clean_events, ev, profile, stage

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SELLER_VARIANT = str(FIXTURES / "track_seller_variant.py")
CREDULOUS_BUYER = str(FIXTURES / "credulous_buyer.py")


def with_notes(buyer_note=None, seller_note=None):
    events = clean_events()
    if seller_note is not None:
        events[6] = ev(7, "ack_recorded", "q-1", observer="seller",
                       status="processed", note=seller_note, attempt=1)
    if buyer_note is not None:
        events[8] = ev(9, "ack_recorded", "r-1", observer="buyer",
                       status="processed", note=buyer_note, attempt=1)
    return events


WRONG_SELLER = {"applied": True, "total_cents": 3999}


def test_a_buyer_whose_own_record_shows_the_wrong_total_fails():
    events = with_notes(
        buyer_note={"correct": True, "total_cents": 3999,
                    "expected_total_cents": 3990},
        seller_note=WRONG_SELLER)

    result = evaluate(profile(), "run-1", events)

    correct = stage(result, "correct")
    assert correct.status == "failed", correct
    assert correct.evidence == ["ev-9"]
    assert "3999" in correct.note and "3990" in correct.note
    assert result.verdict == "failed"


def test_a_buyer_that_checked_against_another_expectation_fails():
    """The total is right, so only the expectation can fail the stage."""
    events = with_notes(buyer_note={"correct": True, "total_cents": 3990,
                                    "expected_total_cents": 3000})

    correct = stage(evaluate(profile(), "run-1", events), "correct")

    assert correct.status == "failed", correct
    assert correct.evidence == ["ev-9"]
    assert "expected_total_cents 3000" in correct.note
    assert "the task expects 3990" in correct.note


def test_a_seller_record_alone_leaves_correct_inconclusive():
    """The participants disagree; the town holds no total to decide."""
    events = with_notes(buyer_note={"correct": True},
                        seller_note=WRONG_SELLER)

    result = evaluate(profile(), "run-1", events)

    correct = stage(result, "correct")
    assert correct.status == "not_enough_evidence", correct
    assert "seller" in correct.note and "3999" in correct.note
    assert result.verdict != "passed"


@pytest.mark.parametrize("total", ["3999", 3999.5, True, None, [3999]])
def test_a_total_that_is_not_an_integer_states_nothing(total):
    """Read the way counts are read: only a JSON integer is a total."""
    events = with_notes(
        buyer_note={"correct": True, "total_cents": total},
        seller_note={"applied": True, "total_cents": total})

    correct = stage(evaluate(profile(), "run-1", events), "correct")

    assert correct.status == "passed", correct


def test_a_consistent_run_still_passes():
    result = evaluate(profile(), "run-1", with_notes(
        buyer_note={"correct": True, "total_cents": 3990,
                    "expected_total_cents": 3990}))

    assert stage(result, "correct").status == "passed"
    assert result.verdict == "passed"


def test_a_buyer_that_reports_the_wrong_total_as_wrong_still_fails():
    events = with_notes(buyer_note={"correct": False, "total_cents": 3999},
                        seller_note=WRONG_SELLER)

    correct = stage(evaluate(profile(), "run-1", events), "correct")

    assert correct.status == "failed"
    assert "buyer observed total 3999" in correct.note


@pytest.mark.parametrize("version", ["0.2.0", "0.3.0", "0.4.0", "0.5.0"])
def test_earlier_versions_replay_the_assertion_alone(version):
    """A recorded bundle keeps the judgment its version gave."""
    events = with_notes(
        buyer_note={"correct": True, "total_cents": 3999,
                    "expected_total_cents": 3990},
        seller_note=WRONG_SELLER)

    replayed = evaluate(profile(), "run-1", events, version=version)

    assert stage(replayed, "correct").status == "passed"


def test_a_wrong_seller_and_a_credulous_buyer_fail_through_the_runner(
        tmp_path):
    """Real coordinator, a fixture seller quoting 3999, a fixture buyer
    that asserts correct without comparing."""
    bundle_dir, result = run_town(
        "quote-clean", str(tmp_path),
        external={"seller": [sys.executable, SELLER_VARIANT, "wrongtotal"],
                  "buyer": [sys.executable, CREDULOUS_BUYER]},
        wait_timeout=30)

    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert stage(result, "correct").status == "failed", detail
    assert result.verdict == "failed", detail
    assert verify_bundle(bundle_dir) == []
    acks = [e.detail["note"] for e in load_bundle(bundle_dir)["events"]
            if e.kind == "ack_recorded" and e.observer == "buyer"]
    assert acks == [{"correct": True, "total_cents": 3999,
                     "expected_total_cents": 3990}]


def test_a_wrong_seller_and_the_stock_buyer_still_fail(tmp_path):
    bundle_dir, result = run_town(
        "quote-clean", str(tmp_path),
        external={"seller": [sys.executable, SELLER_VARIANT, "wrongtotal"]},
        wait_timeout=30)

    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert stage(result, "correct").status == "failed", detail
    assert verify_bundle(bundle_dir) == []
