"""Behavioral contract for the native SharpeShooter research Track flow."""
from __future__ import annotations

from copy import deepcopy
import importlib
import json
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
])
def test_research_task_rejects_malformed_contracts(mutation):
    module = research_api()
    document = task_document()
    mutation(document)
    with pytest.raises(ValidationError):
        module.ResearchTask.model_validate(document)


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
    ("task-digest", lambda body: body["project_result"].update(
        task_digest="sha256:" + "0" * 64)),
    ("request-digest", lambda body: body.update(
        request_digest="sha256:" + "0" * 64)),
    ("receipt-hash", lambda body: body["project_result"].update(
        receipt_integrity_hash="sha256:" + "0" * 64)),
    ("selected-spec", lambda body: body["project_result"]["receipt"]
     ["strategy"]["frozen_spec"].update(name="wrong strategy")),
    ("same-reviewer", lambda body: body["project_result"].update(
        reviewer_agent_id=body["project_result"]["author_agent_id"])),
    ("missing-review", lambda body: body["project_result"]["receipt"].pop("review")),
    ("false-lifecycle", lambda body: body["project_result"].update(
        receipt_consumed_by_scout=False)),
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
    failed = module.evaluate_profile(profile(), "run-1", research_events({
        "task_bound": True, "receipt_verified": False,
        "independent_review": True, "control_complete": True}))
    assert failed.verdict == "failed"
    assert {stage.name: stage.status for stage in failed.stages}[
        "receipt_integrity"] == "failed"


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
