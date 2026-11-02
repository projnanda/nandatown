"""Small offline routing tests; no trained-model inference or live API calls."""
import hashlib
import json
from pathlib import Path
import socket

import httpx
import pytest

from nandatown import piguard_workflow as workflow
from nandatown import web_evidence as baseline
from piguard_helpers import report_for, Browser, Factory


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempted = []
    def deny(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("Network forbidden in adaptive routing tests")
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    yield
    assert not attempted




@pytest.mark.parametrize("status,detection,expected", [
    ("complete", "benign", "gpt-4.1"),
    ("complete", "injection_detected", "gpt-6-luna"),
    ("incomplete", "unknown", "gpt-6-luna"),
    ("incomplete", "injection_detected", "gpt-6-luna"),
])
def test_routing_rules(status, detection, expected):
    decision = workflow.select_route({"status": status, "detection": detection})
    assert decision.model == expected
    assert decision.parameters() == ({"model": "gpt-4.1", "temperature": 0} if expected == "gpt-4.1"
                                    else {"model": "gpt-6-luna", "reasoning_effort": "medium"})






@pytest.mark.parametrize("mode", ["clean", "injection", "incomplete", "error", "partial_positive", "note_positive"])
def test_mock_end_to_end_routing_and_preservation(tmp_path, mode):
    browser, factory = Browser(), Factory()
    saved = {}
    def guard(packet, *args, **kwargs):
        saved["packet"] = packet.model_dump()
        if mode == "error": raise TimeoutError("mock guard failure")
        report = report_for(packet, mode)
        saved["annotations"] = report["security_annotations"]
        return report
    config = baseline.RunConfig(url=browser.page.url, question="Atlas?", model="gpt-4.1",
                               provider="openai", base_url=baseline.OPENAI_BASE_URL)
    output = tmp_path/"run"
    result = workflow.run(config, output, "unused-model-path", browser=browser, guard=guard, service_factory=factory)
    expected = "gpt-4.1" if mode == "clean" else "gpt-6-luna"
    assert result["status"] == "completed", result
    assert [(actor, policy.model) for actor, policy in factory.created] == [("agent-1", "gpt-4.1"), ("agent-2", expected)]
    assert result["routing"]["model"] == expected
    assert result["routing"]["reason"] == ("complete_without_detection" if mode=="clean" else
        "guard_incomplete" if mode in ("incomplete", "error") else "injection_detected")
    assert factory.answer_turn == 2
    assert browser.closed and all(h.is_closed for h in factory.http)
    original = json.loads((output/"handoff.json").read_text(encoding="utf-8"))
    assert original == saved["packet"] and original["items"][0]["text"] == browser.page.visible_text
    actual = [payload for actor, payload in factory.calls if actor=="agent-2"]
    inputs = json.loads((output/"agent2_input.json").read_text(encoding="utf-8"))["requests"]
    guard_report = json.loads((output/"piguard.json").read_text(encoding="utf-8"))
    assert len(inputs) == len(actual) == 2
    for record, payload in zip(inputs, actual):
        assert record["messages"] == payload["messages"]
        assert record["model"] == expected
        assert payload["messages"][0]["content"] == baseline.ANSWERER_SYSTEM_PROMPT
        message = json.loads(payload["messages"][1]["content"])
        assert message["evidence_packet"] == original
        assert message["security_annotations"] == guard_report["security_annotations"]
    collector = next(payload for actor, payload in factory.calls if actor=="agent-1")
    assert collector["messages"][0]["content"] == baseline.COLLECTOR_SYSTEM_PROMPT
    events = [json.loads(line) for line in (output/"events.jsonl").read_text(encoding="utf-8").splitlines()]
    requests = [e["detail"] for e in events if e["kind"]=="model_request" and e["agent"]=="agent-2"]
    assert len(requests) == 2
    for request in requests:
        assert request["model"] == expected
        assert request["reasoning_effort"] == (None if expected=="gpt-4.1" else "medium")
        assert request["temperature"] == (0 if expected=="gpt-4.1" else None)
        assert "thinking" not in request
    assert result["token_usage"] == {"guard": {}, "agent1": {"prompt_tokens":30,"completion_tokens":6,"total_tokens":36},
                                      "agent2": {"prompt_tokens":40,"completion_tokens":20,"total_tokens":60}}
    assert result["usage_details"]["agent2"][0]["completion_tokens_details"]["reasoning_tokens"] == (7 if expected=="gpt-6-luna" else 0)
    assert "mock-secret" not in (output/"events.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize("failure", ["not_found", "timeout"])
def test_luna_error_has_no_fallback_and_keeps_evidence(tmp_path, failure):
    browser, factory = Browser(), Factory(fail_luna=failure)
    config = baseline.RunConfig(url=browser.page.url, question="Atlas?", model="gpt-4.1", provider="openai")
    output = tmp_path/"error"
    result = workflow.run(config, output, "unused", browser=browser,
        guard=lambda p,*a,**kw:report_for(p,"injection"), service_factory=factory)
    assert result["status"] == "error" and result["answer"] is None
    assert result["routing"]["model"] == "gpt-6-luna" and result["routing"]["fallback"] is False
    attempts = 2 if failure == "timeout" else 1
    assert factory.answer_turn == attempts
    assert [p["model"] for actor,p in factory.calls if actor=="agent-2"] == ["gpt-6-luna"] * attempts
    assert (output/"handoff.json").is_file() and (output/"piguard.json").is_file()
    assert result["timing_seconds"]["agent2"] >= 0
    assert all(h.is_closed for h in factory.http)
    events=(output/"events.jsonl").read_text(encoding="utf-8")
    assert "mock-secret" not in events
    if failure == "not_found":
        assert '"status_code":404' in events


def test_nonfixed_agent1_is_rejected_before_model_creation(tmp_path):
    config=baseline.RunConfig(url="http://127.0.0.1/",question="q",model="gpt-6-luna",provider="openai")
    with pytest.raises(ValueError,match="gpt-4.1"):
        workflow.run(config,tmp_path/"wrong","unused")


def test_cli_keeps_agent1_fixed(capsys):
    from nandatown.cli import main
    with pytest.raises(SystemExit) as exc:
        main(["web-evidence-piguard", "--url", "http://127.0.0.1/", "--question", "q",
              "--model", "gpt-6-luna"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
