"""Shared packet reports and mock wire clients for routing tests."""
import hashlib
import json
import httpx
from nandatown import web_evidence as baseline
from nandatown import piguard_workflow as workflow
from nandatown.piguard_guard import fields_from_packet, make_report, finalize

def report_for(packet, mode):
    report = make_report(fields_from_packet(packet))
    if mode != "incomplete":
        for field in report["fields"]:
            field["status"] = "complete"
    if mode in ("injection", "partial_positive", "note_positive"):
        field = report["fields"][-1] if mode == "note_positive" else report["fields"][0]
        report["windows"].append({"field_id": field["field_id"], "evidence_id": field["evidence_id"],
            "index": 0, "start": 0, "end": len(field["text"]), "label": "injection",
            "scores": {"benign": .1, "injection": .9}, "logits": [0, 2], "input_tokens": 10})
    if mode == "partial_positive":
        report["fields"][-1]["status"] = "not_checked"
    return finalize(report)


class Browser:
    def __init__(self):
        text = "Atlas facts. June 2, 2024; USD 48,000; 72 hours. Keep original — فارسی 🙂"
        self.page = baseline.SourcePage(page_id="P1", url="http://127.0.0.1:8766/page.html", title="Atlas",
            visible_text=text, visible_text_sha256="sha256:"+hashlib.sha256(text.encode()).hexdigest(),
            blocks=[], links=[])
        self.closed = False
    def start(self): pass
    def open_page(self, url): return self.page
    def close(self): self.closed = True


class Factory:
    def __init__(self, *, fail_luna=False):
        self.created, self.calls, self.http = [], [], []
        self.collector_turn = self.answer_turn = 0
        self.fail_luna = fail_luna

    def __call__(self, config, log, *, policy, actor):
        self.created.append((actor, policy))
        def endpoint(request):
            payload = json.loads(request.read())
            self.calls.append((actor, payload))
            assert payload["model"] == policy.model
            if actor == "agent-1":
                assert payload["model"] == "gpt-4.1" and payload["temperature"] == 0
                assert "reasoning_effort" not in payload
                self.collector_turn += 1
                name, arguments = {
                    1: ("read_page", {}),
                    2: ("select_evidence", {"items": [{"page_id": "P1"}]}),
                    3: ("finish_evidence", {"complete": True, "note": "Original collector note!"}),
                }[self.collector_turn]
                message = {"content": None, "tool_calls": [{"id": str(self.collector_turn), "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}]}
                usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
            else:
                self.answer_turn += 1
                if payload["model"] == "gpt-6-luna":
                    assert payload["reasoning_effort"] == "medium"
                    assert "temperature" not in payload
                    if self.fail_luna:
                        if self.fail_luna == "timeout":
                            raise httpx.ReadTimeout("mock timeout", request=request)
                        return httpx.Response(404, json={"error": {"type": "invalid_request_error",
                            "code": "model_not_found", "message": "secret must not be logged: mock-secret"}})
                else:
                    assert "reasoning_effort" not in payload and payload["temperature"] == 0
                message = {"content": "invalid JSON" if self.answer_turn == 1 else json.dumps({
                    "answer": "June 2, 2024; USD 48,000; 72 hours.", "citations": [{"evidence_id": "E1"}]})}
                usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
                         "completion_tokens_details": {"reasoning_tokens": 7 if policy.model == "gpt-6-luna" else 0}}
            return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage})
        transport = workflow.RecordingTransport(log, policy=policy, actor=actor)
        transport.inner.close()
        transport.inner = httpx.MockTransport(endpoint)
        http = httpx.Client(base_url=config.base_url, transport=transport,
                            headers={"Authorization": "Bearer mock-secret"})
        self.http.append(http)
        client = baseline.OpenAIClient(policy.model, config.base_url, http=http)
        client.usage_details = transport.usage_details
        return client, http
