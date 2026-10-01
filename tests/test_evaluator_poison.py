"""The custody stages under poison_request, judged from events only."""

import pytest

from nandatown.evaluator import evaluate
from nandatown.records import TestProfile, TownEvent

TASK = {"kind": "quote", "sku": "widget", "quantity": 2,
        "unit_price_cents": 1995, "expected_total_cents": 3990}
REQUEST = ("town", "message_accepted", "q-1",
           {"sender": "buyer", "to": "seller", "kind": "quote_request"})
FINISH = ("town", "run_finished", "r", {})


def delivery(attempt, status="retryable"):
    return [("town", "message_claimed", "q-1", {"attempt": attempt}),
            ("seller", "ack_recorded", "q-1", {"status": status})]


def dead(attempts=3, outcome="retryable"):
    return ("town", "message_dead_lettered", "q-1",
            {"attempts": attempts, "last_outcome": outcome, "notice": "n-1"})


def notice(to="buyer", sender="town"):
    return ("town", "message_accepted", "n-1", {
        "sender": sender, "to": to, "kind": "dead_letter",
        "request_id": "q-1"})


def settled(status="processed"):
    return ("buyer", "ack_recorded", "n-1", {"status": status})


def run(deliveries=3, *tail):
    return [REQUEST, *[e for a in range(1, deliveries + 1)
                       for e in delivery(a)], *tail, FINISH]


def judge(spec, max_attempts=3, fault="poison_request"):
    events = [TownEvent(event_id=f"ev-{i}", run_id="r", at=float(i),
                        observer=o, kind=k, subject=s, detail=d)
              for i, (o, k, s, d) in enumerate(spec, 1)]
    result = evaluate(TestProfile(
        name="p", task=TASK, roles={"buyer": "buyer", "seller": "seller"},
        capabilities={"buyer": [], "seller": ["quote.read"]}, fault=fault,
        lease_seconds=5.0, evaluator="stage-evaluator",
        max_attempts=max_attempts), "r", events)
    return result.verdict, {s.name: s.status for s in result.stages}


def test_a_dead_letter_the_sender_took_in_passes():
    verdict, stages = judge(run(3, dead(), notice(), settled()))
    assert verdict == "passed"
    assert stages["custody_ended"] == stages["sender_notified"] == "passed"
    assert stages["processed"] == "not_tested"
    assert "custody_ended" not in judge(run(3, dead()), fault="none")[1]


def test_the_unbounded_control_fails_where_custody_never_ends():
    verdict, stages = judge(run(12), max_attempts=None)
    assert (verdict, stages["custody_ended"]) == ("failed", "failed")
    assert stages["sender_notified"] == "not_tested"


# Tampered or partial traces: each breaks one stage, and missing evidence
# never passes.
@pytest.mark.parametrize("spec,stage,status", [
    (run(4, dead(4), notice(), settled()), "custody_ended", "failed"),
    (run(2, dead(2), notice(), settled()), "custody_ended", "failed"),
    (run(3, dead(), dead(), notice(), settled()), "custody_ended", "failed"),
    (run(3, dead(), notice(to="seller"), settled()), "sender_notified",
     "failed"),
    (run(3, dead(), notice(sender="seller"), settled()), "sender_notified",
     "failed"),
    (run(3, dead(), notice(), settled("rejected")), "sender_notified",
     "failed"),
    (run(3, dead(), notice()), "sender_notified", "not_enough_evidence"),
    ([REQUEST, *delivery(1, "processed"), FINISH], "custody_ended",
     "not_enough_evidence"),
    ([REQUEST, *delivery(1, "failed"), dead(1, "failed"), notice(),
      settled(), FINISH], "custody_ended", "passed"),
], ids=["past budget", "before budget", "two dead letters", "wrong sender",
        "not from town", "notice refused", "notice unsettled",
        "seller did the work", "refusal ends custody"])
def test_each_trace_is_judged_where_it_breaks(spec, stage, status):
    assert judge(spec)[1][stage] == status
