"""A reused order id with changed terms: the Path's conflicting retry.

The duplicate_request condition resends the same order unchanged. This
profile resends the same request_id with a different quantity, the Path
counterpart of the coordinator's rule that one message identity with
different content is rejected.
"""

import json

import httpx
import uvicorn
from fastapi.testclient import TestClient

from nandatown import cli, path_runner

from nandatown.a2a_adapter import (
    artifact_text, build_a2a_app, build_agent_card, send_message,
)
from nandatown.bundle import load_bundle, verify_bundle
from nandatown.path_runner import evaluate_path, run_path_test
from nandatown.report import render_report

SUBJECT = "http://testserver"
PROFILE = "a2a-order-conflict@0.1"


def stage(result, name):
    return next(s for s in result.stages if s.name == name)


def quote(http, quantity, request_id="order-abc"):
    body = {"sku": "widget", "quantity": quantity,
            "unit_price_cents": 1995, "request_id": request_id}
    return send_message(SUBJECT, json.dumps(body), http=http)


def run_against_seller(tmp_path, defect=None):
    with TestClient(build_a2a_app(SUBJECT, defect=defect)) as http:
        return run_path_test(SUBJECT, str(tmp_path), profile_ref=PROFILE,
                             http=http)


def retry_peer(second_answer):
    """A peer at the HTTP boundary that quotes attempt 1 honestly and gives
    second_answer(order, envelope) for attempt 2."""
    attempts = 0

    def handle(request):
        nonlocal attempts
        if request.method == "GET":
            return httpx.Response(200, json=build_agent_card(SUBJECT))
        envelope = json.loads(request.content)
        order = json.loads(envelope["params"]["message"]["parts"][0]["text"])
        attempts += 1
        if attempts == 2:
            return second_answer(order, envelope)
        task = {"id": "task-1", "kind": "task",
                "status": {"state": "completed"},
                "artifacts": [{"artifactId": "q", "parts": [
                    {"kind": "text", "text": json.dumps(
                        {"request_id": order["request_id"],
                         "total_cents": 3990})}]}]}
        return httpx.Response(200, json={"jsonrpc": "2.0",
                                        "id": envelope["id"], "result": task})

    return httpx.Client(base_url=SUBJECT, transport=httpx.MockTransport(handle))


# -- the reference seller ----------------------------------------------


def test_reference_seller_refuses_a_reused_order_id_with_changed_terms():
    with TestClient(build_a2a_app(SUBJECT)) as http:
        first = quote(http, 2)
        conflicting = quote(http, 5)
    assert first["status"]["state"] == "completed"
    assert json.loads(artifact_text(first))["total_cents"] == 3990
    assert conflicting["status"]["state"] == "rejected"
    assert conflicting["artifacts"][0]["name"] == "error"
    assert "9975" not in artifact_text(conflicting)


def test_reference_seller_still_answers_an_identical_retry():
    with TestClient(build_a2a_app(SUBJECT)) as http:
        first = quote(http, 2)
        again = quote(http, 2)
    assert again["status"]["state"] == "completed"
    assert artifact_text(again) == artifact_text(first)


def test_reference_seller_binds_terms_per_order_id():
    with TestClient(build_a2a_app(SUBJECT)) as http:
        quote(http, 2, request_id="order-a")
        other = quote(http, 5, request_id="order-b")
    assert other["status"]["state"] == "completed"
    assert json.loads(artifact_text(other))["total_cents"] == 9975


def test_reference_seller_binds_only_string_order_ids():
    # A request_id that cannot key an order is quoted, not a server error.
    with TestClient(build_a2a_app(SUBJECT)) as http:
        first = quote(http, 2, request_id=["not", "a", "key"])
        second = quote(http, 5, request_id={"not": "a key"})
    assert first["status"]["state"] == "completed"
    assert second["status"]["state"] == "completed"


def test_cli_serves_the_negative_control(monkeypatch):
    served = {}
    monkeypatch.setattr(uvicorn, "run",
                        lambda app, **kwargs: served.setdefault("app", app))
    assert cli.main(["a2a", "serve", "--defect",
                     "accept_conflicting_retry"]) == 0
    with TestClient(served["app"]) as http:
        quote(http, 2)
        assert quote(http, 5)["status"]["state"] == "completed"


def test_accept_conflicting_retry_defect_requotes_the_changed_terms():
    with TestClient(build_a2a_app(
            SUBJECT, defect="accept_conflicting_retry")) as http:
        quote(http, 2)
        conflicting = quote(http, 5)
    assert conflicting["status"]["state"] == "completed"
    assert json.loads(artifact_text(conflicting))["total_cents"] == 9975


# -- the profile against the reference seller ---------------------------


def test_order_conflict_profile_passes_when_the_seller_refuses(tmp_path):
    directory, result = run_against_seller(tmp_path)
    assert result.verdict == "passed"
    assert stage(result, "semantic_result").status == "passed"
    conflict = stage(result, "conflicting_retry")
    assert conflict.status == "passed"
    assert "rejected" in conflict.note
    assert result.evaluator_version == "path-order-conflict-0.1"
    assert verify_bundle(directory) == []


def test_the_retry_reuses_the_order_id_with_only_the_quantity_changed(
        tmp_path):
    directory, _ = run_against_seller(tmp_path)
    sends = [i.payload for i in load_bundle(directory)["intents"]
             if i.action == "message_send"]
    assert [s["attempt"] for s in sends] == [1, 2]
    first, retry = sends[0]["body"], sends[1]["body"]
    assert retry["request_id"] == first["request_id"]
    assert first["quantity"] == 2 and retry["quantity"] == 5
    assert {k: v for k, v in retry.items() if k != "quantity"} == \
        {k: v for k, v in first.items() if k != "quantity"}


def test_negative_control_fails_on_conflicting_retry_for_the_predicted_reason(
        tmp_path):
    directory, result = run_against_seller(
        tmp_path, defect="accept_conflicting_retry")
    assert result.verdict == "failed"
    # Everything before the controlled condition still holds: the failure
    # is the conflict, not a broken first quote.
    for name in ("protocol_invocation", "semantic_result"):
        assert stage(result, name).status == "passed"
    conflict = stage(result, "conflicting_retry")
    assert conflict.status == "failed"
    assert "3990" in conflict.note and "9975" in conflict.note
    assert verify_bundle(directory) == []
    assert "First broken stage: conflicting_retry" in render_report(
        load_bundle(directory))


def test_a_recorded_profile_without_retry_changes_is_judged_not_crashed(
        tmp_path):
    # verify replays the profile the bundle recorded, which need not be the
    # shipped one; a missing key must not crash evaluation or the report.
    directory, result = run_against_seller(
        tmp_path, defect="accept_conflicting_retry")
    bundle = load_bundle(directory)
    expected = {k: v for k, v in bundle["profile"].expected.items()
                if k != "retry_changes"}
    bundle["profile"] = bundle["profile"].model_copy(
        update={"expected": expected})
    replay = evaluate_path(bundle["profile"], result.run_id, bundle["events"])
    assert stage(replay, "conflicting_retry").status == "failed"
    assert "Condition: conflicting_retry" in render_report(bundle)


def test_report_names_the_changed_terms_not_a_plain_duplicate(tmp_path):
    directory, _ = run_against_seller(tmp_path)
    condition = next(line for line in
                     render_report(load_bundle(directory)).splitlines()
                     if line.startswith("Condition:"))
    assert "delivered twice" not in condition
    assert "changed terms {'quantity': 5}" in condition


# -- other ways a subject can answer the retry --------------------------


def test_replaying_the_first_quote_is_not_a_refusal(tmp_path):
    def replay_first_quote(order, envelope):
        task = {"id": "task-2", "kind": "task",
                "status": {"state": "completed"},
                "artifacts": [{"artifactId": "q", "parts": [
                    {"kind": "text", "text": json.dumps(
                        {"request_id": order["request_id"],
                         "total_cents": 3990})}]}]}
        return httpx.Response(200, json={"jsonrpc": "2.0",
                                        "id": envelope["id"], "result": task})

    with retry_peer(replay_first_quote) as http:
        directory, result = run_path_test(SUBJECT, str(tmp_path),
                                          profile_ref=PROFILE, http=http)
    conflict = stage(result, "conflicting_retry")
    assert conflict.status == "failed"
    assert "first quote" in conflict.note
    assert result.verdict == "failed"
    assert verify_bundle(directory) == []


def test_a_quote_differing_only_outside_the_price_is_named_honestly(
        tmp_path):
    def restamp_first_quote(order, envelope):
        task = {"id": "task-2", "kind": "task",
                "status": {"state": "completed"},
                "artifacts": [{"artifactId": "q", "parts": [
                    {"kind": "text", "text": json.dumps(
                        {"request_id": order["request_id"],
                         "total_cents": 3990, "ts": 1})}]}]}
        return httpx.Response(200, json={"jsonrpc": "2.0",
                                        "id": envelope["id"], "result": task})

    with retry_peer(restamp_first_quote) as http:
        _, result = run_path_test(SUBJECT, str(tmp_path),
                                  profile_ref=PROFILE, http=http)
    conflict = stage(result, "conflicting_retry")
    assert conflict.status == "failed"
    assert "different quote" in conflict.note


def test_a_failed_task_is_inconclusive_not_a_refusal(tmp_path):
    # failed is an execution error in A2A; a crash on the retry must not
    # read as the agent deciding to refuse it.
    def fail_task(order, envelope):
        task = {"id": "task-2", "kind": "task",
                "status": {"state": "failed"}, "artifacts": []}
        return httpx.Response(200, json={"jsonrpc": "2.0",
                                        "id": envelope["id"], "result": task})

    with retry_peer(fail_task) as http:
        directory, result = run_path_test(SUBJECT, str(tmp_path),
                                          profile_ref=PROFILE, http=http)
    assert stage(result, "conflicting_retry").status == "not_enough_evidence"
    assert result.verdict == "incomplete"
    assert verify_bundle(directory) == []


def test_a_rejected_state_outside_a_task_is_not_a_refusal(tmp_path):
    def rejected_message(order, envelope):
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": envelope["id"],
            "result": {"kind": "message", "status": {"state": "rejected"}}})

    with retry_peer(rejected_message) as http:
        directory, result = run_path_test(SUBJECT, str(tmp_path),
                                          profile_ref=PROFILE, http=http)
    assert stage(result, "conflicting_retry").status == "failed"
    assert verify_bundle(directory) == []


def test_a_nonterminal_answer_is_not_a_refusal(tmp_path):
    def keep_working(order, envelope):
        task = {"id": "task-2", "kind": "task",
                "status": {"state": "working"}, "artifacts": []}
        return httpx.Response(200, json={"jsonrpc": "2.0",
                                        "id": envelope["id"], "result": task})

    with retry_peer(keep_working) as http:
        directory, result = run_path_test(SUBJECT, str(tmp_path),
                                          profile_ref=PROFILE, http=http)
    conflict = stage(result, "conflicting_retry")
    assert conflict.status == "failed"
    assert "'working'" in conflict.note
    assert verify_bundle(directory) == []


def test_an_rpc_error_on_the_retry_is_inconclusive_not_a_pass(tmp_path):
    # A JSON-RPC error cannot be told apart from a malfunction, so it is
    # missing evidence, never a pass.
    def rpc_error(order, envelope):
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": envelope["id"],
            "error": {"code": -32603, "message": "internal error"}})

    with retry_peer(rpc_error) as http:
        directory, result = run_path_test(SUBJECT, str(tmp_path),
                                          profile_ref=PROFILE, http=http)
    assert stage(result, "conflicting_retry").status == "not_enough_evidence"
    assert result.verdict == "incomplete"
    assert verify_bundle(directory) == []


def test_a_town_driver_error_on_the_retry_is_not_blamed_on_the_subject(
        tmp_path, monkeypatch):
    real = path_runner.fingerprint

    def fail_on_the_requote(value):
        if isinstance(value, dict) and value.get("total_cents") == 9975:
            raise RuntimeError("town bug")
        return real(value)

    monkeypatch.setattr(path_runner, "fingerprint", fail_on_the_requote)
    _, result = run_against_seller(tmp_path,
                                   defect="accept_conflicting_retry")
    assert result.verdict == "error"
    assert stage(result, "conflicting_retry").status == "not_tested"


def test_a_first_attempt_that_does_not_complete_sends_no_retry(tmp_path):
    sends = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=build_agent_card(SUBJECT))
        envelope = json.loads(request.content)
        sends.append(envelope)
        task = {"id": "task-1", "kind": "task",
                "status": {"state": "working"}, "artifacts": []}
        return httpx.Response(200, json={"jsonrpc": "2.0",
                                        "id": envelope["id"], "result": task})

    with httpx.Client(base_url=SUBJECT,
                      transport=httpx.MockTransport(handle)) as http:
        _, result = run_path_test(SUBJECT, str(tmp_path),
                                  profile_ref=PROFILE, http=http)
    assert len(sends) == 1
    assert stage(result, "conflicting_retry").status == "not_tested"


def test_two_recorded_retry_exchanges_fail_the_stage(tmp_path):
    directory, result = run_against_seller(tmp_path)
    bundle = load_bundle(directory)
    events = list(bundle["events"])
    retry = next(e for e in events if e.kind == "protocol_exchange"
                 and e.detail["attempt"] == 2)
    events.append(retry.model_copy(update={"event_id": "ev-99"}))
    replay = evaluate_path(bundle["profile"], result.run_id, events)
    conflict = stage(replay, "conflicting_retry")
    assert conflict.status == "failed"
    assert "exactly one protocol exchange" in conflict.note
