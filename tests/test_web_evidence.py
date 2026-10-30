import json
import os
import socket
import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from nandatown.web_evidence import (
    AgentMemory,
    BrowserUnavailable,
    DeepSeekClient,
    ModelReply,
    PlaywrightBrowser,
    RunConfig,
    SourceBlock,
    SourcePage,
    ToolCall,
    URLPolicy,
    URLPolicyError,
    run_web_evidence,
)


FACT = "Project Alpha launched on April 12, 2024."
QUESTION = "When was Project Alpha launched?"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "web_evidence"


def source_page(url="http://127.0.0.1:8765/clean.html"):
    return SourcePage(
        page_id="P1",
        url=url,
        title="Project Alpha archive",
        visible_text=f"Project Alpha\n{FACT}",
        visible_text_sha256="sha256:" + "a" * 64,
        blocks=[
            SourceBlock(block_id="B1", selector="body > main > h1",
                        text="Project Alpha"),
            SourceBlock(block_id="B2", selector="body > main > p#launch-date",
                        text=FACT),
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
    """Deterministic model boundary; the workflow still runs both agents."""

    def __init__(self, answer_replies=None):
        self.collector_turn = 0
        self.answer_replies = list(answer_replies or [json.dumps({
            "answer": "Project Alpha launched on April 12, 2024.",
            "citations": [{"evidence_id": "E1"}],
        })])
        self.requests = []

    def complete(self, messages, *, tools=None, json_mode=False,
                 timeout_seconds=None):
        self.requests.append({"messages": messages, "tools": tools,
                              "json_mode": json_mode})
        if json_mode:
            return ModelReply(content=self.answer_replies.pop(0),
                              usage={"prompt_tokens": 90,
                                     "completion_tokens": 20})
        self.collector_turn += 1
        calls = {
            1: ToolCall(call_id="c1", name="read_page", arguments={}),
            2: ToolCall(call_id="c2", name="select_evidence", arguments={
                "items": [{"page_id": "P1", "block_id": "B2",
                           "quote": FACT}]}),
            3: ToolCall(call_id="c3", name="finish_evidence", arguments={
                "complete": True, "note": "relevant source found"}),
        }
        return ModelReply(tool_calls=[calls[self.collector_turn]],
                          usage={"prompt_tokens": 50,
                                 "completion_tokens": 10})


class RepeatingModel(FakeModel):
    def complete(self, messages, *, tools=None, json_mode=False,
                 timeout_seconds=None):
        assert not json_mode
        self.requests.append({"messages": messages, "tools": tools,
                              "json_mode": json_mode})
        return ModelReply(tool_calls=[ToolCall(
            call_id=f"repeat-{len(self.requests)}", name="read_page",
            arguments={})])


def test_two_agent_workflow_writes_exact_handoff_and_cited_answer(tmp_path):
    model = FakeModel()
    browser = FakeBrowser()
    config = RunConfig(url=browser.page.url, question=QUESTION)

    directory, result = run_web_evidence(
        config, str(tmp_path / "run"), model=model, browser=browser)

    assert result.status == "completed"
    assert result.answer.text == "Project Alpha launched on April 12, 2024."
    assert result.answer.citations[0].evidence_id == "E1"
    assert result.answer.citations[0].url == browser.page.url
    assert result.answer.citation_support_assessed is False
    handoff = json.loads((directory / "handoff.json").read_text())
    item = handoff["items"][0]
    assert item["text"] == FACT
    assert item["validation"] == "verbatim_match"
    assert item["selector"] == "body > main > p#launch-date"
    assert item["start_offset"] == 0
    assert browser.closed
    agent_2_request = next(request for request in model.requests
                           if request["json_mode"])
    request_text = json.dumps(agent_2_request["messages"])
    assert QUESTION in request_text
    assert FACT in request_text


def test_answerer_retries_unknown_citation_once(tmp_path):
    model = FakeModel(answer_replies=[
        json.dumps({"answer": "wrong shape",
                    "citations": [{"evidence_id": "E999"}]}),
        json.dumps({"answer": "April 12, 2024",
                    "citations": [{"evidence_id": "E1"}]}),
    ])

    _, result = run_web_evidence(
        RunConfig(url=source_page().url, question=QUESTION),
        str(tmp_path / "run"), model=model, browser=FakeBrowser())

    assert result.status == "completed"
    assert result.answer.citations[0].evidence_id == "E1"
    assert sum(request["json_mode"] for request in model.requests) == 2


def test_step_limit_returns_partial_without_inventing_answer(tmp_path):
    model = FakeModel()

    directory, result = run_web_evidence(
        RunConfig(url=source_page().url, question=QUESTION, max_steps=1),
        str(tmp_path / "run"), model=model, browser=FakeBrowser())

    assert result.status == "limit_reached"
    assert result.answer is None
    assert result.evidence_count == 0
    assert json.loads((directory / "handoff.json").read_text())["items"] == []
    assert not any(request["json_mode"] for request in model.requests)


def test_repeated_identical_tool_calls_stop_the_collector(tmp_path):
    directory, result = run_web_evidence(
        RunConfig(url=source_page().url, question=QUESTION, max_steps=6),
        str(tmp_path / "run"), model=RepeatingModel(),
        browser=FakeBrowser())

    assert result.status == "partial"
    assert result.answer is None
    assert result.stop_reason == "repeated identical tool call detected"
    events = (directory / "events.jsonl").read_text()
    assert "repetition stopped" not in events  # terminal text is not evidence
    assert "repeated identical tool call detected" in events


def test_missing_deepseek_key_is_recorded_without_starting_browser(
        tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    directory, result = run_web_evidence(
        RunConfig(url=source_page().url, question=QUESTION),
        str(tmp_path / "run"), browser=FakeBrowser())

    assert result.status == "error"
    assert "DEEPSEEK_API_KEY is required" in result.stop_reason
    assert json.loads((directory / "result.json").read_text())["status"] == "error"


def test_output_directory_must_be_empty(tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    (output / "keep.txt").write_text("user data")

    with pytest.raises(ValueError, match="not empty"):
        run_web_evidence(RunConfig(url=source_page().url, question=QUESTION),
                         str(output), model=FakeModel(), browser=FakeBrowser())

    assert (output / "keep.txt").read_text() == "user data"


def test_memory_trimming_never_discards_question_or_task_state():
    memory = AgentMemory(QUESTION, recent_limit=4)
    for index in range(10):
        memory.add_exchange(
            {"role": "assistant", "content": "x" * 500,
             "tool_calls": []},
            {"role": "tool", "tool_call_id": str(index),
             "content": "y" * 500},
        )
    messages = memory.build("SYSTEM", {"evidence": [FACT]}, token_budget=500)

    assert messages[0]["content"] == "SYSTEM"
    assert QUESTION in messages[1]["content"]
    assert FACT in messages[1]["content"]
    assert len(messages) <= 4


def test_deepseek_request_explicitly_disables_thinking():
    observed = {}

    def handle(request):
        observed.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {
                "content": '{"answer":"ok","citations":[]}',
                "tool_calls": []}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4,
                      "total_tokens": 7},
        })

    client = DeepSeekClient(
        http=httpx.Client(base_url="https://api.deepseek.invalid",
                          transport=httpx.MockTransport(handle)))
    reply = client.complete([{"role": "user", "content": "json"}],
                            json_mode=True)

    assert observed["thinking"] == {"type": "disabled"}
    assert observed["reasoning_effort"] == "none"
    assert observed["response_format"] == {"type": "json_object"}
    assert reply.usage["total_tokens"] == 7


def resolver_for(address):
    def resolve(_host, _port, **_kwargs):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 0))]
    return resolve


def test_url_policy_allows_public_assets_but_same_origin_navigation():
    policy = URLPolicy("https://example.test/start",
                       resolver=resolver_for("93.184.216.34"))

    policy.validate_navigation("https://example.test/next")
    policy.validate_subresource("https://cdn.example.test/app.js")
    with pytest.raises(URLPolicyError, match="same-origin"):
        policy.validate_navigation("https://other.example.test/next")


def test_loopback_fixture_cannot_load_another_loopback_origin():
    policy = URLPolicy("http://127.0.0.1:8765/clean.html")

    policy.validate_subresource("http://127.0.0.1:8765/style.css")
    with pytest.raises(URLPolicyError, match="loopback"):
        policy.validate_subresource("http://127.0.0.1:9999/private")


def test_url_policy_blocks_private_network_and_credentials():
    with pytest.raises(URLPolicyError, match="private"):
        URLPolicy("http://private.test/", resolver=resolver_for("10.1.2.3"))
    with pytest.raises(URLPolicyError, match="credentials"):
        URLPolicy("https://user:secret@example.test/",
                  resolver=resolver_for("93.184.216.34"))


@contextmanager
def fixture_server():
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

    handler = lambda *args, **kwargs: QuietHandler(
        *args, directory=str(FIXTURE_DIR), **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/clean.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_playwright_reads_clean_local_fixture():
    with fixture_server() as url:
        config = RunConfig(url=url, question=QUESTION)
        browser = PlaywrightBrowser(config)
        try:
            browser.start()
        except BrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = browser.open_page(url)
        finally:
            browser.close()

    assert page.title == "Project Alpha archive"
    assert FACT in page.visible_text
    assert any(block.text == FACT for block in page.blocks)


@pytest.mark.skipif(
    os.environ.get("NANDATOWN_RUN_LIVE_DEEPSEEK") != "1",
    reason="manual live smoke; set NANDATOWN_RUN_LIVE_DEEPSEEK=1 explicitly")
def test_manual_live_deepseek_end_to_end(tmp_path):
    if not os.environ.get("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY is not set")
    with fixture_server() as url:
        directory, result = run_web_evidence(
            RunConfig(url=url, question=QUESTION),
            str(tmp_path / "live"))

    assert result.status == "completed", (directory, result.stop_reason)
    assert result.answer
    assert result.answer.citations
