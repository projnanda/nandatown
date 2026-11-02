"""Shared deterministic browser/model doubles for web-evidence tests."""
import json
from pathlib import Path
from nandatown.web_evidence import SourcePage, SourceBlock, ModelReply, ToolCall

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
                "items": [{"page_id": "P1"}]}),
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
