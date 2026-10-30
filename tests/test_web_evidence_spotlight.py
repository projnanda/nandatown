import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from nandatown.cli import main
from nandatown.web_evidence import (
    ANSWERER_SYSTEM_PROMPT,
    Answer,
    Citation,
    EvidenceItem,
    EvidencePacket,
    ModelReply,
    RunConfig,
    RunResult,
    SourceBlock,
    SourcePage,
    ToolCall,
    run_web_evidence,
)
from nandatown.web_evidence_demo import DEMO_QUESTION
from nandatown.web_evidence_spotlight import (
    SPOTLIGHT_MARKER,
    SPOTLIGHTING_PROMPT_APPENDIX,
    datamark_packet,
    datamark_text,
    protected_run_config,
    run_protected_web_evidence,
)


FACT = "Project Alpha launched on April 12, 2024."
QUESTION = "When was Project Alpha launched?"
URL = "http://127.0.0.1:8765/clean.html"


def source_page(fact=FACT):
    return SourcePage(
        page_id="P1",
        url=URL,
        title="Project Alpha archive",
        visible_text=f"Project Alpha\n{fact}",
        visible_text_sha256="sha256:" + "a" * 64,
        blocks=[
            SourceBlock(
                block_id="B1",
                selector="body > main > h1",
                text="Project Alpha",
            ),
            SourceBlock(
                block_id="B2",
                selector="body > main > p#launch-date",
                text=fact,
            ),
        ],
        links=[],
    )


class FakeBrowser:
    def __init__(self, page=None):
        self.page = page or source_page()
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def open_page(self, url):
        assert self.started
        return self.page.model_copy(update={"url": url})

    def close(self):
        self.closed = True


class FakeModel:
    def __init__(self, fact=FACT):
        self.fact = fact
        self.collector_turn = 0
        self.requests = []

    def complete(self, messages, *, tools=None, json_mode=False,
                 timeout_seconds=None):
        self.requests.append({
            "messages": messages,
            "tools": tools,
            "json_mode": json_mode,
        })
        if json_mode:
            return ModelReply(content=json.dumps({
                "answer": "Project Alpha launched on April 12, 2024.",
                "citations": [{"evidence_id": "E1"}],
            }))
        self.collector_turn += 1
        calls = {
            1: ToolCall(call_id="c1", name="read_page", arguments={}),
            2: ToolCall(
                call_id="c2",
                name="select_evidence",
                arguments={
                    "items": [{
                        "page_id": "P1",
                        "block_id": "B2",
                        "quote": self.fact,
                    }],
                },
            ),
            3: ToolCall(
                call_id="c3",
                name="finish_evidence",
                arguments={
                    "complete": True,
                    "note": "relevant source found",
                },
            ),
        }
        return ModelReply(tool_calls=[calls[self.collector_turn]])


def evidence_packet(text="Alpha launched in 2024."):
    return EvidencePacket(
        question=QUESTION,
        items=[EvidenceItem(
            evidence_id="E1",
            page_id="P1",
            url=URL,
            text=text,
            selector="body > main > p",
            block_id="B2",
            start_offset=0,
            end_offset=len(text),
            source_sha256="sha256:" + "a" * 64,
            validation="verbatim_match",
            truncated=False,
        )],
        complete=True,
        completion_note="relevant source found",
    )


def test_datamarking_replaces_each_whitespace_character_only():
    original = "Alpha Beta\nGamma\tDelta\u00a0Epsilon"

    marked = datamark_text(original)

    assert marked == SPOTLIGHT_MARKER.join(
        ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
    )
    assert len(marked) == len(original)
    assert "".join(character for character in marked
                   if character != SPOTLIGHT_MARKER) == (
                       "AlphaBetaGammaDeltaEpsilon"
                   )


def test_datamarking_stops_when_source_contains_marker():
    with pytest.raises(ValueError, match=r"already contains U\+E000"):
        datamark_text(f"source{SPOTLIGHT_MARKER}text")


def test_packet_copy_changes_only_untrusted_text_fields():
    original = evidence_packet()

    marked = datamark_packet(original)

    assert original.items[0].text == "Alpha launched in 2024."
    assert marked.items[0].text == (
        f"Alpha{SPOTLIGHT_MARKER}launched{SPOTLIGHT_MARKER}in"
        f"{SPOTLIGHT_MARKER}2024."
    )
    original_data = original.model_dump()
    marked_data = marked.model_dump()
    original_text = original_data["items"][0].pop("text")
    marked_text = marked_data["items"][0].pop("text")
    original_note = original_data.pop("completion_note")
    marked_note = marked_data.pop("completion_note")
    assert original_text != marked_text
    assert original_note == "relevant source found"
    assert marked_note == datamark_text(original_note)
    assert original_data == marked_data


def test_datamarking_stops_when_completion_note_contains_marker():
    original = evidence_packet().model_copy(update={
        "completion_note": f"source{SPOTLIGHT_MARKER}note",
    })

    with pytest.raises(ValueError, match=r"already contains U\+E000"):
        datamark_packet(original)

    assert original.completion_note == f"source{SPOTLIGHT_MARKER}note"


def test_protected_prompt_is_exact_appendix_to_original_prompt():
    baseline = RunConfig(url=URL, question=QUESTION)

    protected = protected_run_config(baseline)

    assert baseline.answerer_prompt == ANSWERER_SYSTEM_PROMPT
    assert protected.answerer_prompt == (
        ANSWERER_SYSTEM_PROMPT + SPOTLIGHTING_PROMPT_APPENDIX
    )
    assert protected.collector_prompt == baseline.collector_prompt
    baseline_data = baseline.model_dump()
    protected_data = protected.model_dump()
    baseline_data.pop("answerer_prompt")
    protected_data.pop("answerer_prompt")
    assert protected_data == baseline_data


def test_protected_workflow_preserves_agent1_and_logs_actual_handoff(tmp_path):
    baseline_model = FakeModel()
    protected_model = FakeModel()
    config = RunConfig(url=URL, question=QUESTION)

    run_web_evidence(
        config,
        str(tmp_path / "baseline"),
        model=baseline_model,
        browser=FakeBrowser(),
    )
    directory, result = run_protected_web_evidence(
        config,
        str(tmp_path / "protected"),
        model=protected_model,
        browser=FakeBrowser(),
    )

    baseline_collector_requests = [
        request for request in baseline_model.requests
        if not request["json_mode"]
    ]
    protected_collector_requests = [
        request for request in protected_model.requests
        if not request["json_mode"]
    ]
    assert protected_collector_requests == baseline_collector_requests

    original_handoff = json.loads(
        (directory / "handoff.json").read_text(encoding="utf-8")
    )
    actual_handoff = json.loads(
        (directory / "agent2_handoff.json").read_text(encoding="utf-8")
    )
    answer_request = next(
        request for request in protected_model.requests
        if request["json_mode"]
    )
    answer_payload = json.loads(answer_request["messages"][1]["content"])

    assert original_handoff["items"][0]["text"] == FACT
    assert actual_handoff["items"][0]["text"] == datamark_text(FACT)
    assert original_handoff["completion_note"] == "relevant source found"
    assert actual_handoff["completion_note"] == datamark_text(
        "relevant source found"
    )
    assert answer_payload["evidence_packet"] == actual_handoff
    assert answer_request["messages"][0]["content"] == (
        ANSWERER_SYSTEM_PROMPT + SPOTLIGHTING_PROMPT_APPENDIX
    )
    assert result.status == "completed"
    assert result.stop_reason == "relevant source found"
    assert result.answer.citations == [Citation(evidence_id="E1", url=URL)]
    assert result.artifacts["agent2_handoff"] == "agent2_handoff.json"

    guardrail = json.loads(
        (directory / "guardrail.json").read_text(encoding="utf-8")
    )
    assert guardrail["enabled"] is True
    assert guardrail["marker"] == "U+E000"
    assert guardrail["applied"] is True
    assert guardrail["duration_seconds"] >= 0

    events = [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    handoff_event = next(
        event for event in events if event["kind"] == "evidence_handoff"
    )
    assert handoff_event["detail"]["packet"] == actual_handoff


def test_marker_collision_preserves_original_and_stops_before_agent2(tmp_path):
    fact = f"Project{SPOTLIGHT_MARKER}Alpha"
    model = FakeModel(fact=fact)
    browser = FakeBrowser(source_page(fact))

    directory, result = run_protected_web_evidence(
        RunConfig(url=URL, question=QUESTION),
        str(tmp_path / "collision"),
        model=model,
        browser=browser,
    )

    assert result.status == "error"
    assert "already contains U+E000" in result.stop_reason
    assert json.loads(
        (directory / "handoff.json").read_text(encoding="utf-8")
    )["items"][0]["text"] == fact
    assert not (directory / "agent2_handoff.json").exists()
    assert not any(request["json_mode"] for request in model.requests)
    assert browser.closed is True
    guardrail = json.loads(
        (directory / "guardrail.json").read_text(encoding="utf-8")
    )
    assert guardrail["applied"] is False


def test_protected_agent1_hard_context_limit_stops_before_model(tmp_path):
    model = FakeModel()
    browser = FakeBrowser()

    _, result = run_protected_web_evidence(
        RunConfig(
            url=URL,
            question="Q" * 20_000,
            input_token_budget=1_000,
        ),
        str(tmp_path / "context-limit"),
        model=model,
        browser=browser,
    )

    assert result.status == "error"
    assert "Agent 1 base context exceeds its token budget" in result.stop_reason
    assert model.requests == []
    assert browser.closed


def test_protected_runner_passes_overall_deadline_to_playwright(
        tmp_path, monkeypatch):
    observed = {}

    class DeadlineBrowser(FakeBrowser):
        def __init__(self, config, policy=None, deadline=None):
            super().__init__(source_page())
            observed["deadline"] = deadline

    monkeypatch.setattr(
        "nandatown.web_evidence_spotlight.PlaywrightBrowser",
        DeadlineBrowser,
    )
    before = time.monotonic()

    _, result = run_protected_web_evidence(
        RunConfig(url=URL, question=QUESTION),
        str(tmp_path / "deadline"),
        model=FakeModel(),
    )

    assert result.status == "completed"
    assert before < observed["deadline"] <= before + 120.5


def completed_result():
    return RunResult(
        run_id="web-protected-test",
        status="completed",
        stop_reason="relevant source found",
        question=DEMO_QUESTION,
        answer=Answer(
            text="The project started on March 14, 2024.",
            citations=[Citation(
                evidence_id="E2",
                url="http://127.0.0.1:8765/projects.html",
            )],
        ),
        evidence_count=3,
        pages_visited=2,
        steps_used=6,
    )


@contextmanager
def fake_demo_site():
    yield "http://127.0.0.1:8765/index.html"


def test_protected_cli_is_separate_and_uses_protected_runner(
        tmp_path, monkeypatch, capsys):
    observed = {}

    def fake_run(config, output, *, renderer):
        observed.update(config=config, output=output, renderer=renderer)
        return Path(output), completed_result()

    monkeypatch.setattr(
        "nandatown.web_evidence_demo.serve_northstar_site",
        fake_demo_site,
    )
    monkeypatch.setattr(
        "nandatown.web_evidence_spotlight.run_protected_web_evidence",
        fake_run,
    )

    code = main([
        "web-evidence-protected",
        "--json",
        "--out",
        str(tmp_path / "run"),
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["guardrail"] == "spotlighting/datamarking"
    assert observed["config"].question == DEMO_QUESTION
    assert observed["renderer"].enabled is False
