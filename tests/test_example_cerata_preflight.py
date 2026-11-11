"""The Cerata preflight example: adapter, buyer and Track wiring, offline.

Everything here runs against canned answers or the example's local
fake_cerata.py fixture, so it needs no network and no API key. A pass shows
the integration is wired correctly; it is not evidence about Cerata itself
(see examples/integrations/cerata-preflight/README.md for the live check).
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys

import pytest

from nandatown.bundle import verify_bundle
from nandatown.runner import run_town

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "integrations", "cerata-preflight")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"cerata_{name}", os.path.join(EXAMPLE, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


seller = _load("cerata_seller")
buyer = _load("agent_buyer")
fake = _load("fake_cerata")


def _case(case_id):
    return buyer.load_case(case_id, os.path.join(EXAMPLE, "cases.json"))


# --- adapter ---------------------------------------------------------------

def test_idempotency_key_is_stable_per_run_and_message():
    k = seller.idempotency_key("run-a", "q-1")
    assert k == seller.idempotency_key("run-a", "q-1")
    assert k != seller.idempotency_key("run-b", "q-1")
    assert k != seller.idempotency_key("run-a", "q-2")


class Canned:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, path, body, headers):
        self.calls.append((path, headers))
        return self.answers.pop(0)


def _claim(duplicate=False):
    return {"message_id": "q-1", "kind": "quote_request", "from": "buyer", "fence": "f",
            "duplicate": duplicate,
            "body": {"case": "example", "preflight": {"action": {"type": "outbound_call"}}}}


DECIDED = (200, {}, {"decision": "block", "check_id": "chk_1", "receipt_id": "rcpt_1", "receipt": "r",
                     "rules": [{"id": "example.rule", "result": "example"}]})
REPLAYED = (200, {"idempotent-replayed": "true"}, DECIDED[2])


def test_first_application_is_reported_and_reply_carries_only_what_the_agent_needs():
    post = Canned(DECIDED)
    status, note, [reply] = seller.build_handler(post, "ak_test_x", "run-a", naive=False)(_claim())
    assert status == "processed" and note["applied"] is True
    assert post.calls[0][1]["idempotency-key"] == seller.idempotency_key("run-a", "q-1")
    assert reply["message_id"] == "r-1" and reply["body"]["request_id"] == "q-1"
    assert reply["body"]["decision"] == "block" and reply["body"]["receipt"] == "r"
    assert "rules" not in reply["body"]  # only what the agent acts on goes into the run record
    assert "idempotent_replayed" not in reply["body"]  # would change the body on replay


def test_replay_of_recorded_work_is_not_applied_again_and_reply_is_identical():
    handler = seller.build_handler(Canned(DECIDED, REPLAYED), "ak_test_x", "run-a", naive=False)
    _, first_note, [first] = handler(_claim())
    _, second_note, [second] = handler(_claim(duplicate=True))
    assert first == second
    assert first_note["applied"] is True and second_note["applied"] is False


def test_replay_the_town_has_no_record_of_reports_the_earlier_check():
    _, note, _ = seller.build_handler(Canned(REPLAYED), "ak_test_x", "run-a", naive=False)(_claim())
    assert note["applied"] is True and note["applied_by_earlier_attempt"] is True


def test_naive_adapter_sends_no_idempotency_key():
    post = Canned(DECIDED)
    seller.build_handler(post, "ak_test_x", "run-a", naive=True)(_claim())
    assert "idempotency-key" not in post.calls[0][1]


@pytest.mark.parametrize("answer", [(0, {}, {"error": "URLError"}), (401, {}, {"error": {"code": "unauthorized"}}),
                                    (500, {}, {}), (200, {}, {"not": "a decision"})])
def test_failures_fail_closed(answer):
    _, _, [reply] = seller.build_handler(Canned(answer), "ak_test_x", "run-a", naive=False)(_claim())
    assert reply["body"]["status"] == "unavailable"
    assert "decision" not in reply["body"] and "token" not in reply["body"]


def test_in_flight_conflict_is_retried():
    post = Canned((409, {"retry-after": "0"}, {}), DECIDED)
    _, _, [reply] = seller.build_handler(post, "ak_test_x", "run-a", naive=False)(_claim())
    assert reply["body"]["decision"] == "block" and len(post.calls) == 2


def test_non_preflight_requests_are_refused():
    claim = {**_claim(), "body": {"sku": "widget", "quantity": 2, "unit_price_cents": 1995}}
    status, note, replies = seller.build_handler(Canned(), "ak_test_x", "run-a", naive=False)(claim)
    assert status == "rejected" and replies == []


# --- buyer -----------------------------------------------------------------

def _verify_ok(body, consume):
    return {"valid": True, "kind": "receipt"}


def test_exact_case_needs_the_exact_decision_and_a_verifying_receipt():
    case = _case("fl-call-after-8pm")
    body = {"status": "decided", "decision": "block", "receipt": "r"}
    assert buyer.judge(body, case, _verify_ok)["correct"] is True
    assert buyer.judge({**body, "decision": "flag"}, case, _verify_ok)["correct"] is False
    bad = buyer.judge(body, case, lambda b, c: {"valid": False, "reason": "bad_signature"})
    assert bad["correct"] is False and bad["checks"]["receipt_verifies"] is False


def test_never_clear_case_rejects_clear_and_tokens():
    case = _case("listing-no-kids")
    ok = {"status": "decided", "decision": "hold", "hold": {"hold_id": "h"}}
    assert buyer.judge(ok, case, _verify_ok)["correct"] is True
    assert buyer.judge({**ok, "decision": "clear", "receipt": "r"}, case, _verify_ok)["correct"] is False
    assert buyer.judge({**ok, "token": "t"}, case, _verify_ok)["correct"] is False


def test_unavailable_is_correct_only_when_expected():
    case = _case("fl-clean-call")
    body = {"status": "unavailable", "cerata_status": 0}
    assert buyer.judge(body, case, _verify_ok, expect_unavailable=True)["correct"] is True
    assert buyer.judge(body, case, _verify_ok)["correct"] is False


def test_cases_use_only_test_phone_numbers():
    with open(os.path.join(EXAMPLE, "cases.json")) as f:
        text = f.read()
    import re
    for number in re.findall(r'"phone": "(\+\d+)"', text):
        assert number[5:8] == "555", number


# --- Track wiring against the local fixture ----------------------------------

@pytest.fixture
def fixture_cerata(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = fake.serve(port, os.path.join(EXAMPLE, "cases.json"))
    monkeypatch.setenv("CERATA_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("CERATA_API_KEY", fake.API_KEY)
    yield
    server.shutdown()


def _harnesses():
    py = sys.executable
    return {"seller": f"cmd:{py} {os.path.join(EXAMPLE, 'cerata_seller.py')}",
            "buyer": f"cmd:{py} {os.path.join(EXAMPLE, 'agent_buyer.py')}"}


@pytest.mark.parametrize("profile", ["quote-clean", "quote-duplicate-delivery", "quote-amnesia-restart"])
def test_track_run_with_fixture_cerata(tmp_path, monkeypatch, fixture_cerata, profile):
    monkeypatch.setenv("CERATA_CASE", "fl-clean-call")
    bundle_dir, result = run_town(profile, str(tmp_path), harnesses=_harnesses())
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    assert verify_bundle(bundle_dir) == []
    with open(os.path.join(bundle_dir, "events.jsonl")) as f:
        notes = [json.loads(line)["detail"].get("note") or {} for line in f
                 if '"ack_recorded"' in line and '"observer":"buyer"' in line]
    assert any(n.get("checks", {}).get("token_reuse_refused") for n in notes), notes


def test_token_case_fails_if_single_use_check_cannot_run():
    case = _case("fl-clean-call")
    case = {**case, "request": {k: v for k, v in case["request"].items() if k != "token"}}
    body = {"status": "decided", "decision": "clear", "receipt": "r", "token": "t"}
    note = buyer.judge(body, case, lambda b, c: {"valid": True})  # verifier gives no claims
    assert note["correct"] is False and note["checks"]["token_audience_known"] is False


def test_repeat_case_needs_the_same_decision_digest():
    case = _case("fl-clean-call-repeat")
    body = {"status": "decided", "decision": "clear", "receipt": "r", "decision_digest": "sha256:a"}
    assert buyer.judge(body, case, _verify_ok, prior={"decision_digest": "sha256:a"})["correct"] is True
    assert buyer.judge(body, case, _verify_ok, prior={"decision_digest": "sha256:b"})["correct"] is False
    assert buyer.judge(body, case, _verify_ok, prior=None)["correct"] is False
