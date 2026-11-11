"""Behavioral contract for the native SharpeShooter research Track flow."""
from __future__ import annotations

from copy import deepcopy
import importlib
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nandatown.bundle import load_bundle, verify_bundle, write_bundle
from nandatown.coordinator import build_app
from nandatown.profiles import PROFILES
from nandatown.records import (
    RunRecord, TestProfile as ProfileRecord, TownEvent, fingerprint,
    strict_fingerprint,
)
from nandatown.runner import run_town
from test_sharpeshooter_adapter import (
    TASK_DIGEST, api as adapter_api, call as adapter_call,
    configured as configure_adapter, launch_count,
)


ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = ROOT / "tests" / "fixtures" / "sharpeshooter_research_task_v1.json"
ADAPTER = ROOT / "examples" / "sharpeshooter_adapter.py"
RESEARCH_EVALUATOR = "sharpeshooter-research-0.1.0"
QUOTE_FINGERPRINTS = {
    "quote-amnesia-fresh-ids": "sha256:e8a5f86ddd9f6cc7fb3b346cee99832b0e4c03033878ce7c510ec176ff29cd95",
    "quote-amnesia-restart": "sha256:52035625878841928ad218ade0e10886e48c96a7d5b3eb0e1927f61ac53e0d93",
    "quote-clean": "sha256:87a8ada4df2b3b2dc28fded719700e2db02559d8ed4e1647ffaf221e577eaa39",
    "quote-crash-restart": "sha256:a788b2fab17b08a8f6b6ba8d72a433861d5a88d2aee3713b598a747d0628ae36",
    "quote-drop-wakeup": "sha256:5ab74b4bd0a3b113845df7403b34650897f43c2e1aea1d85e6e672ac7a2719c4",
    "quote-duplicate-delivery": "sha256:649ac81897f7d652b41a86393afa52d301fe3197b8db3a3aaaab6e57c25cfca0",
    "quote-llm": "sha256:1e0e144ee7c96edcccfabdd25817305df306892b86c11d0a28b5f030ee0321bb",
    "quote-llm-tool-error": "sha256:a18f78eed773ee4a14a1c0eb28f76366a980a97c1aad57cceedab856107a5a37",
    "quote-llm-truncation": "sha256:123efd5c3fd6c30ce48ca2920343006be6834123034c9848c5460c04de98508b",
    "quote-lost-ack": "sha256:68c9b4c2eab9177a86cc99b1e8089820c5857d78abdb0b67b6cd7ca5ec556702",
}


def task_document() -> dict:
    return json.loads(TASK_PATH.read_text(encoding="utf-8"))


def research_api():
    try:
        return importlib.import_module("nandatown.research")
    except ImportError:
        pytest.fail("native research contract is not implemented")


def profile(name: str = "sharpeshooter-research-clean") -> ProfileRecord:
    try:
        return PROFILES[name]
    except KeyError:
        pytest.fail(f"research profile {name} is not implemented")


def event(number: int, event_kind: str, subject: str,
          observer: str = "town", **detail):
    return TownEvent(event_id=f"ev-{number}", run_id="run-1", at=float(number),
                     observer=observer, kind=event_kind, subject=subject,
                     detail=detail)


def research_events(assertions: dict[str, bool] | None = None) -> list[TownEvent]:
    assertions = assertions or {
        "task_bound": True, "receipt_verified": True,
        "independent_review": True, "control_complete": True,
    }
    return [
        event(1, "message_accepted", "research-1", kind="research_request",
              sender="buyer", to="seller"),
        event(2, "message_claimed", "research-1", claimant="seller", fence="f-1"),
        event(3, "ack_recorded", "research-1", observer="seller",
              status="processed", fence="f-1", note={"applied": True}),
        event(4, "message_accepted", "research-1-result", kind="research_result",
              sender="seller", to="buyer", request_id="research-1"),
        event(5, "message_claimed", "research-1-result", claimant="buyer", fence="f-2"),
        event(6, "ack_recorded", "research-1-result", observer="buyer",
              status="processed", fence="f-2", note=assertions),
    ]


def duplicate_research_events() -> list[TownEvent]:
    return research_events() + [
        event(7, "duplicate_offered", "research-1",
              fault="duplicate_delivery", fence="f-3", attempt=2),
        event(8, "ack_recorded", "research-1", observer="seller",
              status="processed", fence="f-3", attempt=2,
              note={"duplicate": True}),
        event(9, "replay_returned", "research-1-result", sender="seller"),
    ]


def test_research_task_and_profiles_round_trip_strictly_without_changing_quotes():
    module = research_api()
    task = module.ResearchTask.model_validate(task_document())
    assert module.ResearchTask.model_validate_json(task.model_dump_json()) == task
    assert module.research_task_digest(task) == TASK_DIGEST
    clean = profile()
    duplicate = profile("sharpeshooter-research-duplicate-delivery")
    assert clean.task == task
    assert clean.fault == "none" and duplicate.fault == "duplicate_delivery"
    assert clean.lease_seconds == duplicate.lease_seconds == 45
    assert clean.capabilities == duplicate.capabilities == {
        "buyer": [], "seller": ["research.reviewed-receipt"]}
    assert clean.evaluator == duplicate.evaluator == RESEARCH_EVALUATOR
    assert ProfileRecord.model_validate_json(clean.model_dump_json()) == clean
    assert {name: fingerprint(PROFILES[name].model_dump())
            for name in QUOTE_FINGERPRINTS} == QUOTE_FINGERPRINTS


@pytest.mark.parametrize("path", [(), ("idea",), ("candidates", 0),
                                   ("candidates", 0, "strategy_spec")])
def test_research_task_rejects_unknown_fields_at_every_contract_layer(path):
    module = research_api()
    document = task_document()
    target = document
    for part in path:
        target = target[part]
    target["unexpected"] = True
    with pytest.raises(ValidationError):
        module.ResearchTask.model_validate(document)


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(schema_version="wrong"),
    lambda value: value.update(selected_candidate_id="missing"),
    lambda value: value.update(candidates=value["candidates"][:1]),
    lambda value: value["candidates"][1].update(
        candidate_id=value["candidates"][0]["candidate_id"]),
    lambda value: value["candidates"][1].update(
        strategy_spec=deepcopy(value["candidates"][0]["strategy_spec"])),
])
def test_research_task_rejects_malformed_contracts(mutation):
    module = research_api()
    document = task_document()
    mutation(document)
    with pytest.raises(ValidationError):
        module.ResearchTask.model_validate(document)


@pytest.mark.parametrize("name,mutate", [
    ("http-uri", lambda value: value["idea"]["sources"][0].update(
        uri="http://example.com/source")),
    ("uri-userinfo", lambda value: value["idea"]["sources"][0].update(
        uri="https://" + "user:secret@" + "example.com/source")),
    ("ambiguous-uri", lambda value: value["idea"]["sources"][0].update(
        uri="https://example.com" + "\\@" + "other.example/source")),
    ("invalid-host-underscore", lambda value: value["idea"]["sources"][0]
     .update(uri="https://bad_host.example/source")),
    ("invalid-host-leading-hyphen", lambda value: value["idea"]["sources"][0]
     .update(uri="https://-bad.example/source")),
    ("invalid-host-trailing-dot", lambda value: value["idea"]["sources"][0]
     .update(uri="https://example.com./source")),
    ("invalid-port", lambda value: value["idea"]["sources"][0].update(
        uri="https://example.com:not-a-port/source")),
    ("invalid-date", lambda value: value["idea"]["sources"][0].update(
        accessed_on="2025-02-29")),
    ("non-nfc", lambda value: value["idea"].update(title="Cafe\u0301")),
    ("control", lambda value: value["idea"].update(rationale="bad\u0085text")),
    ("credential", lambda value: value["idea"].update(
        hypothesis="token " + "sk-" + "ABCDEFGHIJKLMNOP")),
    ("authority", lambda value: value["candidates"][0]["strategy_spec"]
     ["parameters"].update(verdict="accept")),
    ("nonfinite", lambda value: value["candidates"][0]["strategy_spec"]
     ["parameters"].update(alpha=math.nan)),
    ("magnitude", lambda value: value["candidates"][0]["strategy_spec"]
     ["parameters"].update(alpha=1_000_000_000_001)),
    ("bad-key", lambda value: value["candidates"][0]["strategy_spec"]
     ["parameters"].update({"Bad-Key": 1})),
    ("string-bound", lambda value: value["candidates"][0]["strategy_spec"]
     ["parameters"].update(label="x" * 257)),
    ("fanout", lambda value: value["candidates"][0]["strategy_spec"]
     ["parameters"].update(items=list(range(33)))),
    ("depth", lambda value: value["candidates"][0]["strategy_spec"]
     ["parameters"].update(level1={"level2": {"level3": {
         "level4": {"level5": {"too_deep": 1}}}}})),
])
def test_research_task_rejects_unsafe_or_authoritative_content(name, mutate):
    module = research_api()
    document = task_document()
    mutate(document)
    with pytest.raises(ValidationError):
        module.ResearchTask.model_validate(document)


def test_research_task_accepts_parameter_boundaries_and_valid_leap_date():
    module = research_api()
    document = task_document()
    document["idea"]["sources"][0]["accessed_on"] = "2024-02-29"
    document["candidates"][0]["strategy_spec"]["parameters"] = {
        **{f"key_{index}": index for index in range(27)},
        "limit_int": 1_000_000_000_000,
        "limit_float": -1_000_000_000_000.0,
        "text": "x" * 256,
        "nested": [{"level2": [None]}],
        "boolean": True,
    }
    assert len(document["candidates"][0]["strategy_spec"]["parameters"]) == 32
    module.ResearchTask.model_validate(document)


@pytest.mark.parametrize("uri", [
    "https://127.0.0.1:443/source",
    "https://[2001:db8::1]/source",
    "https://münich.example/source",
    "https://example.test/research%7Bvalue%7D?value=%22%3C%3E%5E%60%7C#value=%5B%5D",
])
def test_research_task_accepts_frozen_https_host_and_component_boundaries(uri):
    module = research_api()
    document = task_document()
    document["idea"]["sources"][0]["uri"] = uri

    assert module.ResearchTask.model_validate(
        document).idea.sources[0].uri == uri


def test_research_task_enforces_canonical_spec_and_task_byte_bounds():
    module = research_api()
    oversized_spec = task_document()
    spec = oversized_spec["candidates"][0]["strategy_spec"]
    for field in ("universe", "signal", "entry_rule", "exit_rule",
                  "position_sizing", "rebalance"):
        spec[field] = "😀" * 512
    with pytest.raises(ValidationError):
        module.ResearchTask.model_validate(oversized_spec)

    oversized_task = task_document()
    oversized_task["idea"].update(
        title="😀" * 160,
        hypothesis="😀" * 1000,
        rationale="😀" * 2000,
        sources=[{
            "uri": "https://example.com/" + "x" * 2027,
            "label": f"source-{index}",
            "accessed_on": "2024-02-29",
        } for index in range(8)],
    )
    template = oversized_task["candidates"][0]["strategy_spec"]
    candidates = []
    for index in range(8):
        candidate_spec = deepcopy(template)
        candidate_spec["signal"] = "😀" * 512
        candidate_spec["universe"] = "u" * 512
        candidate_spec["entry_rule"] = "e" * 512
        candidate_spec["exit_rule"] = "x" * 512
        candidate_spec["position_sizing"] = "p" * 512
        candidate_spec["rebalance"] = "r" * 512
        candidate_spec["parameters"] = {"variant": index}
        candidates.append({"candidate_id": f"candidate-{index}",
                           "strategy_spec": candidate_spec})
    oversized_task["candidates"] = candidates
    oversized_task["selected_candidate_id"] = "candidate-0"
    with pytest.raises(ValidationError):
        module.ResearchTask.model_validate(oversized_task)


def test_canonical_fingerprint_rejects_nonfinite_numbers():
    with pytest.raises((ValueError, TypeError)):
        strict_fingerprint({"value": math.nan})


def test_closed_contract_selector_is_profile_specific_and_rejects_unknown_pairing():
    module = research_api()
    quote = module.contract_for_profile(PROFILES["quote-clean"])
    research = module.contract_for_profile(profile())
    assert (quote.request_kind, quote.response_kind, quote.evaluator_version) == (
        "quote_request", "quote_response", "0.5.0")
    assert quote.skills == ("town-protocol", "quote.read", "quote.request")
    assert (research.request_kind, research.response_kind,
            research.evaluator_version) == (
        "research_request", "research_result", RESEARCH_EVALUATOR)
    assert research.skills == (
        "town-protocol", "research.request", "research.reviewed-receipt")
    assert module.contract_for_profile(
        profile("sharpeshooter-research-duplicate-delivery")) == research
    with pytest.raises(ValueError, match="unsupported profile/evaluator pairing"):
        module.contract_for_profile(
            profile().model_copy(update={"evaluator": "stage-evaluator"}))


def _create_run(client: TestClient, profile_value: ProfileRecord):
    response = client.post("/runs", json={"profile": profile_value.model_dump()},
                           headers={"X-Town-Admin": "admin"})
    assert response.status_code == 200
    created = response.json()
    sessions = {}
    for name, token in created["join_tokens"].items():
        joined = client.post(f"/runs/{created['run_id']}/join",
                             json={"name": name, "token": token})
        assert joined.status_code == 200
        sessions[name] = joined.json()["session"]
    return created["run_id"], sessions


@pytest.mark.parametrize("profile_name,kind", [
    ("quote-drop-wakeup", "quote_request"),
    ("sharpeshooter-research-clean", "research_request"),
])
def test_coordinator_targets_drop_fault_at_the_profiles_request_kind(
        tmp_path, profile_name, kind):
    selected = PROFILES[profile_name].model_copy(update={"fault": "drop_wakeup"})
    with TestClient(build_app(str(tmp_path / "town.db"), "admin")) as client:
        run_id, sessions = _create_run(client, selected)
        response = client.post(
            f"/runs/{run_id}/messages",
            json={"message_id": "request-1", "to": "seller", "kind": kind,
                  "body": {"fixture": True}},
            headers={"X-Town-Session": sessions["buyer"]})
        assert response.status_code == 202
        events = client.get(f"/runs/{run_id}/events",
                            headers={"X-Town-Admin": "admin"}).json()["events"]
    assert [item["subject"] for item in events
            if item["kind"] == "notify_suppressed"] == ["request-1"]


@pytest.fixture(scope="module")
def valid_town_result(tmp_path_factory):
    outcome, _env, _runtime = adapter_call(
        adapter_api(), tmp_path_factory.mktemp("research-result"))
    return outcome["body"]


def test_requester_builds_canonical_request_and_accepts_a_valid_reviewed_result(
        valid_town_result):
    module = research_api()
    task = module.ResearchTask.model_validate(task_document())
    request = module.build_research_request(task)
    assert request == {"schema_version": "sharpeshooter.research-request.v1",
                       "task": task_document(), "task_digest": TASK_DIGEST}
    assert module.validate_research_result(
        valid_town_result, request_id="research-1", request=request) == {
            "task_bound": True, "receipt_verified": True,
            "independent_review": True, "control_complete": True}


@pytest.mark.parametrize("name,mutate", [
    ("request-binding", lambda body: body.update(schema_version="wrong")),
    ("request-binding", lambda body: body.update(request_id="research-2")),
    ("task-digest", lambda body: body["project_result"].update(
        task_digest="sha256:" + "0" * 64)),
    ("request-digest", lambda body: body.update(
        request_digest="sha256:" + "0" * 64)),
    ("receipt-hash", lambda body: body["project_result"].update(
        receipt_integrity_hash="sha256:" + "0" * 64)),
    ("selected-spec", lambda body: body["project_result"]["receipt"]
     ["strategy"]["frozen_spec"].update(name="wrong strategy")),
    ("selected-spec", lambda body: body["project_result"].update(
        selected_candidate_id="candidate-b")),
    ("selected-spec", lambda body: body["project_result"].update(
        selected_is_strategy_hash="sha256:" + "0" * 64)),
    ("candidate-binding", lambda body: body["project_result"].update(
        candidate_results=list(reversed(
            body["project_result"]["candidate_results"])))),
    ("candidate-binding", lambda body: body["project_result"][
        "candidate_results"][1].update(
            candidate_id=body["project_result"]["candidate_results"][0]
            ["candidate_id"])),
    ("task-binding", lambda body: body["project_result"].update(
        client_task_id="another-task")),
    ("task-binding", lambda body: body["project_result"].update(title="wrong")),
    ("task-binding", lambda body: body["project_result"].update(
        hypothesis="wrong")),
    ("task-binding", lambda body: body["project_result"].update(
        sources_digest="sha256:" + "0" * 64)),
    ("same-reviewer", lambda body: body["project_result"].update(
        reviewer_agent_id=body["project_result"]["author_agent_id"])),
    ("missing-review", lambda body: body["project_result"]["receipt"].pop("review")),
    ("missing-review", lambda body: body["project_result"].update(
        review_recommendation="reject")),
    ("false-lifecycle", lambda body: body["project_result"].update(
        receipt_consumed_by_scout=False)),
    ("false-lifecycle", lambda body: body["project_result"].update(
        receipt_verification="FAILED")),
    ("false-lifecycle", lambda body: body["project_result"].update(
        operator_approval="pending")),
    ("false-lifecycle", lambda body: body["project_result"].update(
        operator_verdict="reject")),
    ("false-lifecycle", lambda body: body["project_result"].update(
        budget_before=1)),
    ("false-lifecycle", lambda body: body["project_result"].update(
        budget_after=0)),
    ("wrong-limitations", lambda body: body["project_result"].update(limitations=[])),
])
def test_requester_rejects_unbound_or_unreviewed_results(
        valid_town_result, name, mutate):
    module = research_api()
    request = module.build_research_request(
        module.ResearchTask.model_validate(task_document()))
    body = deepcopy(valid_town_result)
    mutate(body)
    with pytest.raises(module.ResearchResultError, match=name):
        module.validate_research_result(body, request_id="research-1", request=request)


def test_requester_rejects_unknown_project_result_fields(valid_town_result):
    module = research_api()
    request = module.build_research_request(
        module.ResearchTask.model_validate(task_document()))
    body = deepcopy(valid_town_result)
    body["project_result"]["unexpected"] = True
    with pytest.raises(module.ResearchResultError, match="result-shape"):
        module.validate_research_result(
            body, request_id="research-1", request=request)


@pytest.mark.parametrize("size", [66_000, 99_000])
def test_requester_reports_frozen_result_byte_bound_failures(
        valid_town_result, size):
    module = research_api()
    request = module.build_research_request(
        module.ResearchTask.model_validate(task_document()))
    body = deepcopy(valid_town_result)
    body["project_result"]["limitations"] = ["x" * size]
    with pytest.raises(module.ResearchResultError, match="result-size"):
        module.validate_research_result(
            body, request_id="research-1", request=request)


def _rehash_receipt(body):
    body["project_result"]["receipt_integrity_hash"] = fingerprint(
        body["project_result"]["receipt"])


def _mutate_project_and_receipt_id(body, field, value):
    body["project_result"][field] = value
    body["project_result"]["receipt"]["idea"][field] = value
    _rehash_receipt(body)


def _mutate_receipt(body, path, value):
    target = body["project_result"]["receipt"]
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    _rehash_receipt(body)


def _reuse_is_run_as_oos(body):
    reused_run_id = body["project_result"]["candidate_results"][0]["is_run_id"]
    body["project_result"]["oos_run_id"] = reused_run_id
    body["project_result"]["receipt"]["evaluation"]["run_id"] = reused_run_id
    _rehash_receipt(body)


def _use_unhashable_oos_run_id(body):
    body["project_result"]["oos_run_id"] = []
    body["project_result"]["receipt"]["evaluation"]["run_id"] = []
    _rehash_receipt(body)


@pytest.mark.parametrize("name,mutate", [
    ("project-schema", lambda body: body["project_result"].update(
        schema_version="wrong")),
    ("runtime", lambda body: body["project_result"].update(
        agent_runtime="codex")),
    ("project-synthetic", lambda body: body["project_result"].update(
        synthetic_metrics=False)),
    ("candidate-run-id", lambda body: body["project_result"]
     ["candidate_results"][0].update(is_run_id="bad")),
    ("idea-id", lambda body: _mutate_project_and_receipt_id(
        body, "idea_id", "not-a-project-id")),
    ("receipt-schema", lambda body: _mutate_receipt(
        body, ("schema_version",), "2.0")),
    ("receipt-split", lambda body: _mutate_receipt(
        body, ("evaluation", "split"), "is")),
    ("receipt-family", lambda body: _mutate_receipt(
        body, ("idea", "family_id"), "3" * 32)),
    ("receipt-run", lambda body: _mutate_receipt(
        body, ("evaluation", "run_id"), "3" * 32)),
    ("reused-is-run", _reuse_is_run_as_oos),
    ("unhashable-oos-run", _use_unhashable_oos_run_id),
    ("receipt-trial", lambda body: _mutate_receipt(
        body, ("evaluation", "family_oos_trial_number"), 2)),
    ("receipt-synthetic", lambda body: _mutate_receipt(
        body, ("evaluation", "synthetic_metrics"), False)),
    ("receipt-check-synthetic", lambda body: _mutate_receipt(
        body, ("evaluation", "checks", "synthetic_metrics"), False)),
    ("review-content", lambda body: _mutate_receipt(
        body, ("review", "assessment"), "plausible but unfrozen")),
    ("receipt-oversize", lambda body: _mutate_receipt(
        body, ("review", "assessment"), "x" * 32_769)),
])
def test_requester_rejects_invalid_frozen_project_and_receipt_facts(
        valid_town_result, name, mutate):
    module = research_api()
    request = module.build_research_request(
        module.ResearchTask.model_validate(task_document()))
    body = deepcopy(valid_town_result)
    mutate(body)
    with pytest.raises(module.ResearchResultError):
        module.validate_research_result(
            body, request_id="research-1", request=request)


def test_research_evaluator_has_independent_attributed_stages():
    module = research_api()
    result = module.evaluate_profile(profile(), "run-1", research_events())
    assert result.evaluator_version == RESEARCH_EVALUATOR
    assert result.verdict == "passed"
    assert [stage.name for stage in result.stages] == [
        "accepted", "claimed", "received", "processed", "response",
        "task_bound", "receipt_integrity", "independent_review",
        "control_complete", "portable_identity"]
    by_name = {stage.name: stage for stage in result.stages}
    assert by_name["portable_identity"].status == "not_tested"
    for name in ("task_bound", "receipt_integrity", "independent_review",
                 "control_complete"):
        assert by_name[name].status == "passed"
        assert "requester-attributed" in by_name[name].note


@pytest.mark.parametrize("assertion,stage", [
    ("task_bound", "task_bound"),
    ("receipt_verified", "receipt_integrity"),
    ("independent_review", "independent_review"),
    ("control_complete", "control_complete"),
])
def test_research_evaluator_fails_each_requester_assertion_independently(
        assertion, stage):
    module = research_api()
    assertions = {
        "task_bound": True, "receipt_verified": True,
        "independent_review": True, "control_complete": True,
    }
    assertions[assertion] = False
    failed = module.evaluate_profile(
        profile(), "run-1", research_events(assertions))
    assert failed.verdict == "failed"
    assert {item.name: item.status for item in failed.stages}[stage] == "failed"


@pytest.mark.parametrize("event_index,field,value,stage", [
    (0, "sender", "seller", "accepted"),
    (0, "to", "buyer", "accepted"),
    (3, "sender", "buyer", "response"),
    (3, "to", "seller", "response"),
])
def test_research_evaluator_rejects_wrong_transport_direction(
        event_index, field, value, stage):
    module = research_api()
    events = research_events()
    events[event_index] = events[event_index].model_copy(update={
        "detail": {**events[event_index].detail, field: value}})
    result = module.evaluate_profile(profile(), "run-1", events)
    assert {item.name: item.status for item in result.stages}[stage] == "failed"


@pytest.mark.parametrize("status", ["rejected", "failed", "retryable"])
def test_research_evaluator_requires_processed_terminal_requester_ack(status):
    module = research_api()
    events = research_events()
    events[-1] = events[-1].model_copy(update={
        "detail": {**events[-1].detail, "status": status}})
    result = module.evaluate_profile(profile(), "run-1", events)
    by_name = {item.name: item.status for item in result.stages}
    assert all(by_name[name] == "failed" for name in (
        "task_bound", "receipt_integrity", "independent_review",
        "control_complete"))


def test_research_evaluator_rejects_missing_or_extra_requester_assertions():
    module = research_api()
    for note in (
        {"task_bound": True, "receipt_verified": True,
         "independent_review": True},
        {"task_bound": True, "receipt_verified": True,
         "independent_review": True, "control_complete": True,
         "unexpected": True},
    ):
        events = research_events(note)
        result = module.evaluate_profile(profile(), "run-1", events)
        by_name = {item.name: item.status for item in result.stages}
        assert all(by_name[name] == "failed" for name in (
            "task_bound", "receipt_integrity", "independent_review",
            "control_complete"))

    missing_ack = research_events()[:-1]
    result = module.evaluate_profile(profile(), "run-1", missing_ack)
    by_name = {item.name: item.status for item in result.stages}
    assert all(by_name[name] == "failed" for name in (
        "task_bound", "receipt_integrity", "independent_review",
        "control_complete"))


@pytest.mark.parametrize("duplicate_index,stage", [
    (1, "claimed"),
    (2, "received"),
    (4, "response"),
    (5, "task_bound"),
])
def test_research_evaluator_rejects_extra_claims_and_acknowledgements(
        duplicate_index, stage):
    module = research_api()
    events = research_events()
    duplicated = events[duplicate_index].model_copy(update={
        "event_id": f"ev-extra-{duplicate_index}",
        "at": events[duplicate_index].at + 0.1,
    })
    events.append(duplicated)
    result = module.evaluate_profile(profile(), "run-1", events)
    assert {item.name: item.status for item in result.stages}[stage] == "failed"


def test_duplicate_profile_derives_recognition_from_town_transport_events():
    module = research_api()
    result = module.evaluate_profile(
        profile("sharpeshooter-research-duplicate-delivery"), "run-1",
        duplicate_research_events())
    assert [item.name for item in result.stages][-2:] == [
        "duplicate_recognized", "portable_identity"]
    assert {item.name: item.status for item in result.stages}[
        "duplicate_recognized"] == "passed"
    duplicate_stage = next(item for item in result.stages
                           if item.name == "duplicate_recognized")
    assert "Town observed" in duplicate_stage.note
    assert "evaluated separately" in duplicate_stage.note


@pytest.mark.parametrize(
    "mutation",
    [
        lambda events: events.__delitem__(6),
        lambda events: events[6].detail.update(fence=["f-3"]),
        lambda events: events[7].detail.update(fence="wrong-fence"),
        lambda events: events[7].detail.update(note={"duplicate": True,
                                                     "extra": True}),
        lambda events: events.__delitem__(8),
        lambda events: events.__setitem__(
            8, events[8].model_copy(update={"subject": "wrong-result"})),
        lambda events: events.insert(6, events.pop(8)),
        lambda events: events.append(events[6].model_copy(
            update={"event_id": "ev-extra-offer", "at": 9.5})),
        lambda events: events.append(events[8].model_copy(
            update={"event_id": "ev-extra-replay", "at": 10.0})),
    ],
    ids=(
        "missing-offer", "malformed-offer-fence", "wrong-ack-fence",
        "nonexact-duplicate-note", "missing-replay",
        "wrong-replay-identity", "replay-before-offer", "multiple-offers",
        "multiple-replays",
    ),
)
def test_duplicate_recognition_rejects_unbound_or_ambiguous_transport(mutation):
    module = research_api()
    events = duplicate_research_events()
    mutation(events)

    result = module.evaluate_profile(
        profile("sharpeshooter-research-duplicate-delivery"), "run-1", events)

    assert {item.name: item.status for item in result.stages}[
        "duplicate_recognized"] == "failed"


def test_research_bundle_replays_its_own_version_without_reinterpreting_quotes(tmp_path):
    module = research_api()
    selected = profile()
    events = research_events()
    result = module.evaluate_profile(selected, "run-1", events)
    run = RunRecord(
        run_id="run-1", profile_name=selected.name,
        profile_fingerprint=fingerprint(selected.model_dump()), created_at=1.0,
        participants=[{"name": "buyer", "role": "buyer"},
                      {"name": "seller", "role": "seller"}],
        releases={"nandatown": "0.2.0", "evaluator": RESEARCH_EVALUATOR,
                  "python": sys.version.split()[0]})
    directory = tmp_path / "research-bundle"
    write_bundle(str(directory), selected, run, [], events, result)
    assert verify_bundle(str(directory)) == []
    loaded = load_bundle(str(directory))
    assert loaded["profile"] == selected
    assert loaded["result"].evaluator_version == RESEARCH_EVALUATOR
    assert module.contract_for_profile(PROFILES["quote-clean"]).evaluator_version == "0.5.0"
    report = (directory / "report.md").read_text(encoding="utf-8")
    assert "selected momentum-6-1" in report
    assert "not proof of real project, OOS, or receipt compatibility" in report
    assert "deterministic synthetic provider" not in report

    crossed = selected.model_copy(update={"evaluator": "stage-evaluator"})
    crossed_run = run.model_copy(update={
        "profile_fingerprint": fingerprint(crossed.model_dump())})
    crossed_directory = tmp_path / "crossed-bundle"
    write_bundle(str(crossed_directory), crossed, crossed_run, [], events, result)
    assert any("unsupported profile/evaluator pairing" in problem
               for problem in verify_bundle(str(crossed_directory)))


def test_fixture_only_healthy_profile_runs_through_the_real_coordinator(
        tmp_path, monkeypatch):
    env, runtime = configure_adapter(tmp_path / "fixture-boundary")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    bundle_dir, result = run_town(
        "sharpeshooter-research-clean", str(tmp_path / "town-bundle"),
        external={"seller": [sys.executable, str(ADAPTER)]}, wait_timeout=90)
    assert result.verdict == "passed"
    assert result.evaluator_version == RESEARCH_EVALUATOR
    assert verify_bundle(bundle_dir) == []
    assert launch_count(runtime) == 1
    bundle = load_bundle(bundle_dir)
    assert len([path for path in (tmp_path / "town-bundle").iterdir()
                if (path / "manifest.json").is_file()]) == 1
    assert bundle["profile"].evaluator == RESEARCH_EVALUATOR
    assert bundle["manifest"]["evaluator_version"] == RESEARCH_EVALUATOR
    assert bundle["run"].releases["evaluator"] == RESEARCH_EVALUATOR
    seller = next(participant for participant in bundle["run"].participants
                  if participant["name"] == "seller")
    assert seller["release"] == "external command; immutable release not recorded"

    requests = [event for event in bundle["events"]
                if event.kind == "message_accepted"
                and event.detail.get("kind") == "research_request"]
    responses = [event for event in bundle["events"]
                 if event.kind == "message_accepted"
                 and event.detail.get("kind") == "research_result"]
    assert len(requests) == len(responses) == 1
    request, response = requests[0], responses[0]
    assert request.subject == "research-1"
    assert response.subject == "research-1-result"
    seller_claims = [event for event in bundle["events"]
                     if event.kind == "message_claimed"
                     and event.subject == "research-1"
                     and event.detail.get("claimant") == "seller"]
    seller_acks = [event for event in bundle["events"]
                   if event.kind == "ack_recorded"
                   and event.subject == "research-1"
                   and event.observer == "seller"]
    buyer_claims = [event for event in bundle["events"]
                    if event.kind == "message_claimed"
                    and event.subject == "research-1-result"
                    and event.detail.get("claimant") == "buyer"]
    buyer_acks = [event for event in bundle["events"]
                  if event.kind == "ack_recorded"
                  and event.subject == "research-1-result"
                  and event.observer == "buyer"]
    assert len(seller_claims) == len(seller_acks) == 1
    assert seller_acks[0].detail["note"] == {"applied": True}
    assert len(buyer_claims) == len(buyer_acks) == 1
    buyer_ack = buyer_acks[0]
    assert buyer_ack.detail["note"] == {
        "task_bound": True, "receipt_verified": True,
        "independent_review": True, "control_complete": True}
    assert {stage.name: stage.status for stage in result.stages} == {
        "accepted": "passed", "claimed": "passed", "received": "passed",
        "processed": "passed", "response": "passed", "task_bound": "passed",
        "receipt_integrity": "passed", "independent_review": "passed",
        "control_complete": "passed", "portable_identity": "not_tested",
    }

    verified = subprocess.run(
        [sys.executable, "-m", "nandatown.cli", "verify", bundle_dir],
        text=True, capture_output=True, check=False)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "evaluator reproduces" in verified.stdout
    replayed = subprocess.run(
        [sys.executable, "-m", "nandatown.cli", "replay", bundle_dir],
        text=True, capture_output=True, check=False)
    assert replayed.returncode == 0, replayed.stdout + replayed.stderr
    assert replayed.stdout.startswith("Replay of ")


def test_fixture_only_duplicate_profile_runs_through_the_real_coordinator(
        tmp_path, monkeypatch):
    """Two Town deliveries must replay one fixture result under one fence."""
    env, runtime = configure_adapter(tmp_path / "fixture-duplicate-boundary")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    bundle_dir, result = run_town(
        "sharpeshooter-research-duplicate-delivery",
        str(tmp_path / "town-duplicate-bundle"),
        external={"seller": [sys.executable, str(ADAPTER)]},
        wait_timeout=90,
    )

    bundle = load_bundle(bundle_dir)
    events = bundle["events"]
    assert result.verdict == "passed", {
        "stages": [(stage.name, stage.status, stage.note)
                   for stage in result.stages],
        "events": [(item.kind, item.subject, item.observer, item.detail)
                   for item in events],
    }
    assert verify_bundle(bundle_dir) == []
    assert launch_count(runtime) == 1

    requests = [item for item in events
                if item.kind == "message_accepted"
                and item.detail.get("kind") == "research_request"]
    assert [item.subject for item in requests] == ["research-1"]

    seller_claims = [item for item in events
                     if item.kind == "message_claimed"
                     and item.subject == "research-1"
                     and item.detail.get("claimant") == "seller"]
    seller_acks = [item for item in events
                   if item.kind == "ack_recorded"
                   and item.subject == "research-1"
                   and item.observer == "seller"
                   and item.detail.get("status") == "processed"]
    assert len(seller_claims) == 1
    assert len(seller_acks) == 2
    assert [item.detail["note"] for item in seller_acks] == [
        {"applied": True}, {"duplicate": True}]

    duplicate_offers = [item for item in events
                        if item.kind == "duplicate_offered"
                        and item.subject == "research-1"]
    assert len(duplicate_offers) == 1
    duplicate_fence = duplicate_offers[0].detail["fence"]
    assert seller_acks[1].detail["fence"] == duplicate_fence

    responses = [item for item in events
                 if item.kind == "message_accepted"
                 and item.detail.get("kind") == "research_result"]
    assert [item.subject for item in responses] == ["research-1-result"]
    response_replays = [item for item in events
                        if item.kind == "replay_returned"
                        and item.subject == "research-1-result"]
    assert len(response_replays) == 1

    journal_path = (Path(bundle_dir) / "state" / "seller"
                    / "sharpeshooter-journal-v1.json")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    module = adapter_api()
    assert journal["state"] == "completed"
    assert journal["result_message_id"] == responses[0].subject
    assert journal["result_body"]["request_id"] == requests[0].subject
    assert responses[0].detail["content_fingerprint"] == fingerprint(
        journal["result_body"])
    assert journal["result_body_digest"] == module._digest(
        journal["result_body"])
    assert journal["project_result_digest"] == module._digest(
        journal["result_body"]["project_result"])
    assert journal["replay_facts"] == {
        "project_invocations": 1, "response_count": 1}

    statuses = {stage.name: stage.status for stage in result.stages}
    assert statuses["duplicate_recognized"] == "passed"
