"""Shared three-method contract tests; fake browser/models, no model weights."""
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import socket

import pytest

from nandatown.evidence_workflow import BaselineAdapter, SCHEMA_VERSION, run_workflow
from nandatown.piguard_workflow import PIGuardAdapter
from nandatown.promptarmor_workflow import PromptArmorAdapter, default_config
from nandatown.web_evidence import ANSWERER_SYSTEM_PROMPT, RunConfig, SourcePage
from piguard_helpers import report_for
from web_evidence_helpers import FakeBrowser, FakeModel

FIXTURE = Path(__file__).parent / "fixtures/web_evidence/atlas_relay"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def deny(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("No real network in shared workflow tests")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    yield
    assert not attempts


class BodyText(HTMLParser):
    """Static test-double input only; real Chromium extraction has separate tests."""

    def __init__(self):
        super().__init__()
        self.body = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.body = True

    def handle_endtag(self, tag):
        if tag == "body":
            self.body = False

    def handle_data(self, text):
        if self.body:
            self.parts.append(text)


def atlas_page(variant):
    parser = BodyText()
    parser.feed((FIXTURE / "site" / variant / "index.html").read_text(encoding="utf-8"))
    text = "\n".join(parser.parts)
    return SourcePage(page_id="P1", url=f"http://127.0.0.1:8766/{variant}/index.html",
                      title="Atlas Relay", visible_text=text,
                      visible_text_sha256="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                      blocks=[], links=[])


class BenignGuard:
    model = "gpt-6-luna"
    usage = {}

    def complete(self, system, data):
        return json.dumps({"injection": False, "spans": []})


def adapter_for(method, config, model):
    if method == "baseline":
        return BaselineAdapter()
    if method == "llm":
        return PromptArmorAdapter(config, guard_model="gpt-6-luna",
            guard_base_url="https://mock.invalid/v1", guard_key_env="UNUSED",
            agent_key_env="UNUSED", guard_reasoning_effort="medium", guard_client=BenignGuard())
    return PIGuardAdapter("unused", guard=lambda p, *a, **kw: report_for(p, "injection"),
                          injected_model=model)


@pytest.mark.parametrize("method", ["baseline", "llm", "piguard"])
@pytest.mark.parametrize("variant", ["clean", "malicious"])
def test_paired_fixture_complete_handoff_and_shared_contract(tmp_path, method, variant):
    page = atlas_page(variant)
    model, browser = FakeModel(), FakeBrowser(page)
    config = default_config(page.url, (FIXTURE / "task.txt").read_text(encoding="utf-8").strip())
    output = tmp_path / "run"
    result = run_workflow(config, output, adapter_for(method, config, model),
                          model=model, browser=browser)
    assert result["status"] == "completed"
    assert result["schema_version"] == SCHEMA_VERSION
    contract = json.loads((Path(__file__).parents[1] / "docs/contracts/web-evidence-experiment.schema.json").read_text(encoding="utf-8"))
    assert set(contract["required"]) <= result.keys()
    assert result["security_check_status"] in contract["properties"]["security_check_status"]["enum"]
    assert result["defense"] == method
    assert result["factual_support_assessed"] is False  # Fake answers are not efficacy evidence.
    original = json.loads((output / "handoff.json").read_text(encoding="utf-8"))
    delivered = json.loads((output / "agent2_handoff.json").read_text(encoding="utf-8"))
    assert original == delivered
    assert original["items"][0]["text"] == page.visible_text
    actual = json.loads((output / "agent2_input.json").read_text(encoding="utf-8"))["requests"]
    assert actual[0]["messages"][0]["content"] == ANSWERER_SYSTEM_PROMPT
    payload = json.loads(actual[0]["messages"][1]["content"])
    assert payload["evidence_packet"] == original
    assert "ground_truth" not in payload and "attacker_targets" not in payload
    assert result["answer"]["citations"][0]["evidence_id"] == "E1"
    assert browser.closed
    if variant == "malicious":
        assert "Agent 1 completion memo" in payload["evidence_packet"]["items"][0]["text"]


def test_atlas_hashes_and_oracle_outside_served_tree():
    provenance = json.loads((FIXTURE / "provenance.json").read_text(encoding="utf-8"))
    for relative, expected in provenance["files"].items():
        assert hashlib.sha256((FIXTURE / "site" / relative).read_bytes()).hexdigest() == expected
    assert {p.name for p in (FIXTURE / "site").rglob("*") if p.is_file()} == {"index.html"}
    oracle = json.loads((FIXTURE / "oracle.json").read_text(encoding="utf-8"))
    assert oracle["ground_truth"]["incident_credit_cap_usd"] == 48000


def test_errors_never_persist_raw_model_exception(tmp_path):
    class FailingModel(FakeModel):
        def complete(self, *args, **kwargs):
            raise RuntimeError("Bearer do-not-record-this-secret")

    output = tmp_path / "failed"
    result = run_workflow(RunConfig(url="http://127.0.0.1/", question="q"), output,
                          BaselineAdapter(), model=FailingModel(), browser=FakeBrowser())
    assert result["execution_status"] == "error"
    assert "do-not-record-this-secret" not in (output / "events.jsonl").read_text(encoding="utf-8")
    assert "do-not-record-this-secret" not in (output / "result.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("method", ["llm", "piguard"])
def test_unified_cli_selects_defense_defaults(tmp_path, monkeypatch, capsys, method):
    from nandatown.cli import main
    seen = {}

    if method == "llm":
        def run(config, output, **kwargs):
            seen.update(config=config, kwargs=kwargs)
            return {"status": "completed"}
        monkeypatch.setattr("nandatown.promptarmor_workflow.run", run)
    else:
        def cli(args):
            seen.update(model=args.model, provider=args.provider)
            return 0
        monkeypatch.setattr("nandatown.piguard_workflow.cli", cli)
    assert main(["web-evidence", "--defense", method, "--url", "http://127.0.0.1/",
                 "--question", "q", "--out", str(tmp_path / "run"), "--json"]) == 0
    if method == "llm":
        assert seen["config"].model == "gpt-4.1"
        assert seen["kwargs"]["guard_model"] == "gpt-6-luna"
    else:
        assert seen == {"model": "gpt-4.1", "provider": "openai"}


def test_original_prompts_are_preserved():
    from nandatown.web_evidence import COLLECTOR_SYSTEM_PROMPT
    saved = json.loads((FIXTURE.parent / "agent_prompts.json").read_text(encoding="utf-8"))
    assert COLLECTOR_SYSTEM_PROMPT == saved["COLLECTOR_SYSTEM_PROMPT"]
    assert ANSWERER_SYSTEM_PROMPT == saved["ANSWERER_SYSTEM_PROMPT"]


@pytest.mark.parametrize("method", ["baseline", "llm", "piguard"])
def test_no_evidence_never_calls_defense_or_answerer(tmp_path, method):
    model, browser = FakeModel(), FakeBrowser()
    config = default_config(browser.page.url, "q").model_copy(update={"max_steps": 1})
    adapter = adapter_for(method, config, model)

    def unexpected(*args):
        pytest.fail("Defense must not run without evidence")

    adapter.defend = unexpected
    result = run_workflow(config, tmp_path / "empty", adapter, model=model, browser=browser)
    assert result["status"] == "limit_reached"
    assert result["security_check_status"] == "not_started"
    assert not any(r["json_mode"] for r in model.requests)


@pytest.mark.parametrize("method", ["baseline", "llm", "piguard"])
def test_large_handoff_fails_without_truncation_or_answer_call(tmp_path, method):
    model, browser = FakeModel(), FakeBrowser()
    page = browser.page.model_copy(update={"visible_text": "source data " * 4000})
    config = default_config(page.url, "q").model_copy(update={"input_token_budget": 1000})
    packet = {
        "question": "q", "complete": True, "completion_note": "done",
        "items": [{"evidence_id": "E1", "page_id": "P1", "url": page.url,
            "text": page.visible_text, "selector": "body", "block_id": "PAGE_VISIBLE_TEXT",
            "start_offset": 0, "end_offset": len(page.visible_text),
            "source_sha256": page.visible_text_sha256}],
    }
    replay = tmp_path / "packet.json"
    replay.write_text(json.dumps(packet), encoding="utf-8")
    output = tmp_path / "capacity"
    result = run_workflow(config, output, adapter_for(method, config, model), model=model,
                          packet_path=replay)
    assert result["execution_status"] == "error"
    assert not model.requests
    assert json.loads((output / "handoff.json").read_text(encoding="utf-8")) == packet
    assert json.loads((output / "agent2_handoff.json").read_text(encoding="utf-8"))["items"][0]["text"] == page.visible_text
