import pytest

from nandatown.evaluator import EVALUATOR_VERSION, evaluate
from nandatown.records import (
    TestProfile,
    TownEvent,
    canonical_json,
    fingerprint,
)


def profile(fault="none") -> TestProfile:
    return TestProfile(
        name=f"quote-{fault}",
        task={"kind": "quote", "sku": "widget", "quantity": 2,
              "unit_price_cents": 1995, "expected_total_cents": 3990},
        roles={"buyer": "buyer", "seller": "seller"},
        capabilities={"buyer": [], "seller": ["quote.read"]},
        fault=fault, lease_seconds=5.0, evaluator="stage-evaluator",
    )


def ev(i, ekind, subject, observer="town", **detail):
    return TownEvent(event_id=f"ev-{i}", run_id="run-1", at=float(i),
                     observer=observer, kind=ekind, subject=subject,
                     detail=detail)


def clean_events():
    return [
        ev(1, "run_created", "run-1"),
        ev(2, "participant_joined", "buyer"),
        ev(3, "participant_joined", "seller"),
        ev(4, "message_accepted", "q-1", kind="quote_request", sender="buyer",
           to="seller"),
        ev(5, "message_claimed", "q-1", claimant="seller", attempt=1),
        ev(6, "message_accepted", "r-1", kind="quote_response",
           sender="seller", to="buyer", request_id="q-1"),
        ev(7, "ack_recorded", "q-1", observer="seller", status="processed",
           note={"applied": True, "total_cents": 3990}, attempt=1),
        ev(8, "message_claimed", "r-1", claimant="buyer", attempt=1),
        ev(9, "ack_recorded", "r-1", observer="buyer", status="processed",
           note={"correct": True, "total_cents": 3990}, attempt=1),
        ev(10, "run_finished", "run-1"),
    ]


def stage(result, name):
    return next(s for s in result.stages if s.name == name)


def test_clean_run_passes_every_stage():
    result = evaluate(profile(), "run-1", clean_events())
    assert result.evaluator_version == EVALUATOR_VERSION
    for name in ["accepted", "claimed", "received", "processed", "response",
                 "correct"]:
        assert stage(result, name).status == "passed", name
    assert stage(result, "portable_identity").status == "not_tested"
    assert result.verdict == "passed"
    assert "ev-4" in stage(result, "accepted").evidence


def test_missing_buyer_ack_is_not_enough_evidence():
    events = [e for e in clean_events() if e.event_id != "ev-9"]
    result = evaluate(profile(), "run-1", events)
    assert stage(result, "correct").status == "not_enough_evidence"
    assert result.verdict == "incomplete"


def test_wrong_total_fails_correct_stage():
    events = clean_events()
    events[8] = ev(9, "ack_recorded", "r-1", observer="buyer",
                   status="processed",
                   note={"correct": False, "total_cents": 100}, attempt=1)
    result = evaluate(profile(), "run-1", events)
    assert stage(result, "correct").status == "failed"
    assert result.verdict == "failed"


def test_double_application_fails_processed_stage():
    events = clean_events() + [
        ev(11, "ack_recorded", "q-1", observer="seller", status="processed",
           note={"applied": True, "total_cents": 3990}, attempt=2),
    ]
    result = evaluate(profile(), "run-1", events)
    assert stage(result, "processed").status == "failed"
    assert result.verdict == "failed"


def test_crash_profile_fault_checks():
    events = [
        ev(1, "run_created", "run-1"),
        ev(2, "participant_joined", "buyer"),
        ev(3, "participant_joined", "seller"),
        ev(4, "message_accepted", "q-1", kind="quote_request", sender="buyer",
           to="seller"),
        ev(5, "message_claimed", "q-1", claimant="seller", attempt=1),
        ev(6, "stale_fence_rejected", "q-1", participant="seller"),
        ev(7, "participant_crashed", "seller", observer="runner", exit_code=3),
        ev(8, "participant_restarted", "seller", observer="runner"),
        ev(10, "message_claimed", "q-1", claimant="seller", attempt=2),
        ev(11, "message_accepted", "r-1", kind="quote_response",
           sender="seller", to="buyer", request_id="q-1"),
        ev(12, "ack_recorded", "q-1", observer="seller", status="processed",
           note={"applied": True, "total_cents": 3990}, attempt=2),
        ev(13, "message_claimed", "r-1", claimant="buyer", attempt=1),
        ev(14, "ack_recorded", "r-1", observer="buyer", status="processed",
           note={"correct": True, "total_cents": 3990}, attempt=1),
    ]
    result = evaluate(profile("crash_after_claim"), "run-1", events)
    assert stage(result, "recovered_after_restart").status == "passed"
    assert stage(result, "stale_fence_rejected").status == "passed"
    assert result.verdict == "passed"


def test_duplicate_profile_fault_checks():
    events = clean_events() + [
        ev(11, "duplicate_offered", "q-1", fence="fence-offer", attempt=2),
        ev(12, "ack_recorded", "q-1", observer="seller", status="processed",
           note={"duplicate": True}, fence="fence-offer", attempt=2),
    ]
    result = evaluate(profile("duplicate_delivery"), "run-1", events)
    assert stage(result, "duplicate_recognized").status == "passed"
    assert stage(result, "processed").status == "passed"
    assert result.verdict == "passed"


def test_wakeup_and_ack_fault_checks():
    drop = clean_events() + [ev(11, "notify_suppressed", "q-1")]
    result = evaluate(profile("drop_wakeup"), "run-1", drop)
    assert stage(result, "wakeup_loss_tolerated").status == "passed"

    lost = clean_events() + [ev(11, "ack_dropped", "q-1", participant="seller")]
    result2 = evaluate(profile("lost_ack"), "run-1", lost)
    assert stage(result2, "ack_retry_survived").status == "passed"

    missing = evaluate(profile("lost_ack"), "run-1", clean_events())
    assert stage(missing, "ack_retry_survived").status == "not_enough_evidence"
    assert missing.verdict == "incomplete"


def second_response_events():
    """A seller that answers the one request twice, under two identities."""
    return clean_events()[:-1] + [
        ev(11, "message_accepted", "r-1-again", kind="quote_response",
           sender="seller", to="buyer", request_id="q-1"),
        ev(12, "run_finished", "run-1"),
    ]


def test_second_distinct_response_fails_response_and_correct():
    result = evaluate(profile(), "run-1", second_response_events())
    response = stage(result, "response")
    assert response.status == "failed"
    assert "2 distinct quote responses" in response.note
    assert {"ev-6", "ev-11"} <= set(response.evidence)
    assert stage(result, "correct").status == "failed"
    assert result.verdict == "failed"


def test_two_requests_each_answered_once_names_both_counts():
    # A buyer that sends two distinct requests, each answered correctly
    # once: still not the one exchange the profile expects, but the note
    # must not imply the seller duplicated its answer.
    events = clean_events()[:-1] + [
        ev(11, "message_accepted", "q-2", kind="quote_request",
           sender="buyer", to="seller"),
        ev(12, "message_claimed", "q-2", claimant="seller", attempt=1),
        ev(13, "message_accepted", "r-2", kind="quote_response",
           sender="seller", to="buyer", request_id="q-2"),
        ev(14, "ack_recorded", "q-2", observer="seller", status="processed",
           note={"applied": True, "total_cents": 3990}, attempt=1),
        ev(15, "message_claimed", "r-2", claimant="buyer", attempt=1),
        ev(16, "ack_recorded", "r-2", observer="buyer", status="processed",
           note={"correct": True, "total_cents": 3990}, attempt=1),
        ev(17, "run_finished", "run-1"),
    ]
    result = evaluate(profile(), "run-1", events)
    response = stage(result, "response")
    assert response.status == "failed"
    assert response.note == (
        "2 quote requests and 2 distinct quote responses were accepted; the"
        " profile expects exactly one of each (an idempotent resend of one"
        " identity is not counted)")
    assert {"ev-4", "ev-11", "ev-6", "ev-13"} <= set(response.evidence)
    assert stage(result, "correct").status == "failed"
    assert response.note in stage(result, "correct").note
    # One request answered twice keeps blaming the duplicate response.
    single = stage(evaluate(profile(), "run-1", second_response_events()),
                   "response")
    assert single.note.startswith("2 distinct quote responses were accepted,"
                                  " expected one")


def test_redelivered_request_answered_under_fresh_identity_fails():
    events = clean_events() + [
        ev(11, "duplicate_offered", "q-1", fence="fence-offer", attempt=2),
        ev(12, "message_accepted", "r-9f3a", kind="quote_response",
           sender="seller", to="buyer", request_id="q-1"),
        ev(13, "ack_recorded", "q-1", observer="seller", status="processed",
           note={"duplicate": True}, fence="fence-offer", attempt=2),
    ]
    result = evaluate(profile("duplicate_delivery"), "run-1", events)
    assert stage(result, "response").status == "failed"
    assert "2 distinct quote responses" in stage(result, "response").note
    assert result.verdict == "failed"


def test_idempotent_response_retry_is_not_a_second_response():
    # Resending the same identity with identical content returns the
    # original acceptance: the town records a replay, not a new message.
    events = clean_events() + [
        ev(11, "duplicate_offered", "q-1", fence="fence-offer", attempt=2),
        ev(12, "replay_returned", "r-1", sender="seller"),
        ev(13, "ack_recorded", "q-1", observer="seller", status="processed",
           note={"duplicate": True}, fence="fence-offer", attempt=2),
    ]
    result = evaluate(profile("duplicate_delivery"), "run-1", events)
    assert stage(result, "response").status == "passed"
    assert stage(result, "duplicate_recognized").status == "passed"
    assert result.verdict == "passed"


def test_response_naming_another_request_fails():
    events = clean_events()
    events[5] = ev(6, "message_accepted", "r-1", kind="quote_response",
                   sender="seller", to="buyer", request_id="q-does-not-exist")
    result = evaluate(profile(), "run-1", events)
    response = stage(result, "response")
    assert response.status == "failed"
    assert "q-does-not-exist" in response.note and "q-1" in response.note
    assert stage(result, "correct").status == "failed"
    assert result.verdict == "failed"


def test_response_without_request_id_is_not_enough_evidence():
    events = clean_events()
    events[5] = ev(6, "message_accepted", "r-1", kind="quote_response",
                   sender="seller", to="buyer")
    result = evaluate(profile(), "run-1", events)
    assert stage(result, "response").status == "not_enough_evidence"
    assert stage(result, "correct").status == "not_enough_evidence"
    assert result.verdict == "incomplete"


OVERSIZED_REQUEST_ID = "q-1-" + "x" * 200_000
# Worst case: two bounded values (response and accepted request) plus prose.
NOTE_BOUND = 512


def digest(value):
    """The bounded description the town records for a request_id that is
    not a short string."""
    return {"type": {str: "string", type(None): "null", int: "number"}[
                type(value)],
            "json_length": len(canonical_json(value)),
            "fingerprint": fingerprint(value)}


def response_events(request_message_id="q-1", **correlation):
    """clean_events with the request identity and the response's recorded
    correlation detail replaced."""
    events = clean_events()
    events[3] = ev(4, "message_accepted", request_message_id,
                   kind="quote_request", sender="buyer", to="seller")
    events[4] = ev(5, "message_claimed", request_message_id,
                   claimant="seller", attempt=1)
    events[5] = ev(6, "message_accepted", "r-1", kind="quote_response",
                   sender="seller", to="buyer", **correlation)
    events[6] = ev(7, "ack_recorded", request_message_id, observer="seller",
                   status="processed",
                   note={"applied": True, "total_cents": 3990}, attempt=1)
    return events


def assert_notes_bounded(result):
    for s in result.stages:
        assert len(s.note) < NOTE_BOUND, (s.name, len(s.note))


@pytest.mark.parametrize("request_message_id", ["q-1", "q-" + "7" * 300])
def test_digest_of_the_accepted_request_id_passes(request_message_id):
    # Correlation stays exact when the town recorded only a digest.
    result = evaluate(profile(), "run-1", response_events(
        request_message_id, request_id_digest=digest(request_message_id)))
    assert stage(result, "response").status == "passed"
    assert result.verdict == "passed"


def test_digest_of_another_request_id_fails_with_small_notes():
    # Same prefix as the accepted request, but not the same value.
    result = evaluate(profile(), "run-1", response_events(
        request_id_digest=digest(OVERSIZED_REQUEST_ID)))
    response = stage(result, "response")
    assert response.status == "failed"
    assert fingerprint(OVERSIZED_REQUEST_ID)[:23] in response.note
    assert str(len(canonical_json(OVERSIZED_REQUEST_ID))) in response.note
    assert stage(result, "correct").status == "failed"
    assert result.verdict == "failed"
    assert_notes_bounded(result)


def test_oversized_request_ids_are_bounded_in_notes():
    long_request = "q-" + "7" * 5_000
    result = evaluate(profile(), "run-1", response_events(
        long_request, request_id=OVERSIZED_REQUEST_ID))
    response = stage(result, "response")
    assert response.status == "failed"
    assert fingerprint(OVERSIZED_REQUEST_ID)[:23] in response.note
    assert fingerprint(long_request)[:23] in response.note
    assert stage(result, "correct").status == "failed"
    assert_notes_bounded(result)


@pytest.mark.parametrize("correlation", [
    {"request_id": None}, {"request_id_digest": digest(None)}],
    ids=["recorded-null", "digest-of-null"])
def test_null_request_id_names_no_request_and_fails(correlation):
    result = evaluate(profile(), "run-1", response_events(**correlation))
    response = stage(result, "response")
    assert response.status == "failed"
    assert "JSON null" in response.note and "None" not in response.note
    assert stage(result, "correct").status == "failed"


@pytest.mark.parametrize("correlation", [
    {"request_id": 7}, {"request_id": ["q-1"]},
    {"request_id_digest": digest(7)}, {"request_id_digest": "q-1"},
    {"request_id_digest": {"type": "string"}}],
    ids=["number", "array", "digest-of-number", "malformed-digest",
         "incomplete-digest"])
def test_non_string_or_malformed_correlation_fails(correlation):
    result = evaluate(profile(), "run-1", response_events(**correlation))
    assert stage(result, "response").status == "failed"
    assert result.verdict == "failed"
    assert_notes_bounded(result)


def test_recorded_0_2_0_rules_are_unchanged():
    # 0.2.0 neither counted nor correlated responses. Bundles recorded
    # under it keep that meaning when replayed.
    legacy = evaluate(profile(), "run-1", second_response_events(),
                      version="0.2.0")
    assert legacy.evaluator_version == "0.2.0"
    assert legacy.verdict == "passed"
    current = evaluate(profile(), "run-1", second_response_events())
    assert current.evaluator_version == EVALUATOR_VERSION != "0.2.0"
    assert current.verdict == "failed"


def test_unknown_evaluator_version_is_refused():
    with pytest.raises(ValueError, match="unsupported Track evaluator"):
        evaluate(profile(), "run-1", clean_events(), version="9.9.9")
