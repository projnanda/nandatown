"""Short offline tests: no official model loading or real inference."""
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import socket
import time

import pytest

from nandatown import web_evidence as baseline
from nandatown.piguard_guard import (inspect_packet, token_windows, validate_logits,
    WARNING, INCOMPLETE, verify_snapshot)
from nandatown.piguard_workflow import (run, annotated_messages, RecordingTransport)


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    marker = tmp_path/"forbidden-network"
    monkeypatch.setenv("PIGUARD_NETWORK_TEST_SENTINEL", str(marker))
    def deny(*args, **kwargs):
        marker.write_text("Forbidden network attempt")
        raise AssertionError("Network forbidden in targeted Mock tests")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    yield
    assert not marker.exists(), "A test or spawned worker attempted network access"


class MockTokenizer:
    def num_special_tokens_to_add(self, pair=False):
        return 2

    def __call__(self, text, **kwargs):
        assert kwargs == {"add_special_tokens": False, "truncation": False, "return_offsets_mapping": True}
        return {"input_ids": [ord(c) for c in text],
                "offset_mapping": [(i, i+1) for i in range(len(text))]}


class MockBackend:
    def __init__(self, directory):
        self.mode = str(directory)
        if self.mode == "load_error":
            raise OSError("mock load failure")
        self.tokenizer = MockTokenizer()
        self.identity = {"mock": True, "official_model_loaded": False}

    def classify(self, ids):
        if self.mode == "timeout":
            time.sleep(10)
        if self.mode == "partial_timeout" and ord("!") not in ids:
            time.sleep(10)
        if self.mode == "bad_logits":
            return [float("nan"), 1], len(ids)+2
        return ([0, 3] if ord("!") in ids else [3, 0]), len(ids)+2


class MissingModuleBackend:
    def __init__(self, directory):
        raise ModuleNotFoundError("private detail must not be recorded", name="torch")


def test_missing_dependency_reports_module_and_never_benign():
    report = inspect_packet(packet(), "unused", backend_factory=MissingModuleBackend,
                            field_timeout=8, total_timeout=12)
    assert report["status"] == "incomplete"
    assert report["detection"] == "unknown"
    assert not report["windows"]
    assert report["errors"] == [{"kind": "worker_error", "error_type": "ModuleNotFoundError",
                                 "missing_module": "torch"}]
    assert "private detail" not in json.dumps(report)


def packet(text="source facts", note="done"):
    return baseline.EvidencePacket(question="What are the facts?", complete=True,
        completion_note=note, items=[baseline.EvidenceItem(evidence_id="E1", page_id="P1",
            url="http://127.0.0.1:8766/page.html", text=text, selector="body",
            block_id="PAGE_VISIBLE_TEXT", start_offset=0, end_offset=len(text),
            source_sha256="sha256:"+hashlib.sha256(text.encode()).hexdigest())])


def test_shared_agents_and_prompt_contract():
    from nandatown.promptarmor_workflow import default_config
    config = default_config("http://127.0.0.1/", "q")
    assert config.collector_prompt == baseline.COLLECTOR_SYSTEM_PROMPT
    assert config.answerer_prompt == baseline.ANSWERER_SYSTEM_PROMPT
    assert baseline.RunConfig(url="http://127.0.0.1/", question="q").model == "gpt-4.1"


@pytest.mark.parametrize("text", ["", "  \n\t", "a"*510, "🙂فارسی \n"*140, "repeat! "*170])
def test_windows_full_coverage_unicode_overlap(text):
    windows = list(token_windows(text, MockTokenizer()))
    assert windows[0]["start"] == 0 and windows[-1]["end"] == len(text)
    covered = set()
    for w in windows:
        assert len(w["input_ids"])+2 <= 512
        assert [ord(c) for c in text[w["start"]:w["end"]]] == w["input_ids"]
        covered.update(range(w["start"], w["end"]))
    assert covered == set(range(len(text)))
    for previous, current in zip(windows, windows[1:]):
        assert previous["token_end"]-current["token_start"] == 128


def test_scores_and_argmax_tie():
    assert validate_logits([1, 1])[2] == "benign"
    assert validate_logits([-1000, 1000])[1]["injection"] == 1
    with pytest.raises(ValueError):
        validate_logits([float("inf"), 0])


@pytest.mark.parametrize("mode,expected", [("clean", "complete"), ("load_error", "incomplete"),
                                          ("bad_logits", "incomplete"), ("timeout", "incomplete")])
def test_worker_clean_error_timeout(mode, expected):
    began = time.monotonic()
    report = inspect_packet(packet(), mode, backend_factory=MockBackend,
                            field_timeout=0.7 if mode == "timeout" else 8,
                            total_timeout=12)
    assert report["status"] == expected
    assert time.monotonic()-began < 12
    if expected == "complete":
        assert report["detection"] == "benign" and not report["security_annotations"]
    else:
        assert report["detection"] == "unknown"
        assert any(a["warning"] == INCOMPLETE for a in report["security_annotations"])
    assert report["fields"][0]["text"] == "source facts"


def test_real_guard_orchestration_suspicious_note_and_ranges():
    original = packet("🙂"*509+"!"+"same "*130, "note!")
    before = original.model_dump_json()
    report = inspect_packet(original, "clean", backend_factory=MockBackend,
                            field_timeout=8, total_timeout=15)
    assert report["status"] == "complete", report["errors"]
    warnings = [a for a in report["security_annotations"] if a["warning"] == WARNING]
    assert any(a["field_id"] == "completion_note" for a in warnings)
    for w in report["windows"]:
        text = original.completion_note if w["field_id"] == "completion_note" else original.items[0].text
        assert w["text_sha256"] == hashlib.sha256(text[w["start"]:w["end"]].encode()).hexdigest()
        assert sum(w["scores"].values()) == pytest.approx(1)
    assert original.model_dump_json() == before


def test_partial_detection_survives_worker_timeout():
    report = inspect_packet(packet("!"+"a"*1100), "partial_timeout",
        backend_factory=MockBackend, field_timeout=1.5, total_timeout=5)
    assert report["status"] == "incomplete"
    assert report["detection"] == "injection_detected"
    assert {a["warning"] for a in report["security_annotations"]} == {WARNING, INCOMPLETE}


class FakeBrowser:
    def __init__(self):
        text = "Atlas original facts!"
        self.page = baseline.SourcePage(page_id="P1", url="http://127.0.0.1:8766/page.html",
            title="Atlas", visible_text=text, visible_text_sha256="sha256:"+hashlib.sha256(text.encode()).hexdigest(),
            blocks=[], links=[])
        self.closed = False

    def start(self):
        pass

    def open_page(self, url):
        return self.page

    def close(self):
        self.closed = True


class FakeModel:
    def __init__(self):
        self.turn = 0
        self.answers = []

    def complete(self, messages, *, tools=None, json_mode=False, **kwargs):
        if json_mode:
            self.answers.append(messages)
            content = "not json" if len(self.answers) == 1 else json.dumps({"answer": "Atlas facts", "citations": [{"evidence_id": "E1"}]})
            return baseline.ModelReply(content=content, usage={"total_tokens": 4})
        self.turn += 1
        name, args = {1: ("read_page", {}), 2: ("select_evidence", {"items": [{"page_id": "P1"}]}),
                      3: ("finish_evidence", {"complete": True, "note": "note!"})}[self.turn]
        return baseline.ModelReply(tool_calls=[baseline.ToolCall(call_id=str(self.turn), name=name, arguments=args)],
                                   usage={"total_tokens": 3})


@pytest.mark.parametrize("failure", [False, True])
def test_e2e_mock_preserves_packet_prompts_and_actual_repair_requests(tmp_path, failure):
    browser, model = FakeBrowser(), FakeModel()
    config = baseline.RunConfig(url=browser.page.url, question="Atlas?", provider="openai",
        base_url=baseline.OPENAI_BASE_URL, model="gpt-4.1")
    def guard(*args, **kwargs):
        if failure:
            raise TimeoutError("mock guard timeout")
        return inspect_packet(*args, **kwargs, backend_factory=MockBackend)
    out = tmp_path/"run"
    result = run(config, out, "clean", browser=browser, model=model, guard=guard)
    assert result["status"] == "completed", result
    assert result["security_check_status"] == ("incomplete" if failure else "complete")
    source = json.loads((out/"handoff.json").read_text(encoding="utf-8"))
    assert source["items"][0]["text"] == browser.page.visible_text
    requests = json.loads((out/"agent2_input.json").read_text(encoding="utf-8"))["requests"]
    assert len(requests) == len(model.answers) == 2
    for request, actual in zip(requests, model.answers):
        assert request["messages"] == actual
        assert actual[0]["content"] == baseline.ANSWERER_SYSTEM_PROMPT
        payload = json.loads(actual[1]["content"])
        assert payload["evidence_packet"] == source
        assert payload["question"] == config.question
        assert payload["security_annotations"]
        assert payload["security_annotations"][0]["warning"] == (INCOMPLETE if failure else WARNING)
    events = [json.loads(line) for line in (out/"events.jsonl").read_text(encoding="utf-8").splitlines()]
    emitted = [e["detail"]["messages"] for e in events if e["agent"] == "agent-2" and e["kind"] == "model_request"]
    assert emitted == model.answers
    assert browser.closed
    assert result["token_usage"] == {"guard": {}, "agent1": {"total_tokens": 9}, "agent2": {"total_tokens": 8}}


def test_enriched_budget_rejects_without_truncation_or_model_call(tmp_path):
    p = packet("a"*3000)
    messages = [{"role": "system", "content": baseline.ANSWERER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"question": p.question, "evidence_packet": p.model_dump()})}]
    original = json.dumps(messages)
    budget = baseline.estimate_tokens(json.dumps(messages))+10
    with pytest.raises(RuntimeError, match="capacity error; not security blocking"):
        annotated_messages(messages, [{"warning": "x"*2000}], budget)
    assert json.dumps(messages) == original


def test_snapshot_files_verified_without_loading_weights():
    supplied = os.environ.get("NANDATOWN_PIGUARD_SNAPSHOT")
    if not supplied:
        pytest.skip("Optional local snapshot inspection; no download or inference")
    directory = Path(supplied)
    verified, manifest = verify_snapshot(directory)
    assert verified == directory.resolve()
    assert manifest["download_only_no_inference"]
    assert manifest["files"]["model.safetensors"]["bytes"] > 700_000_000
    official = (directory/"modeling_piguard.py").read_text(encoding="utf-8")
    assert "class PIGuard(DebertaV2ForSequenceClassification)" in official
    assert "outputs.last_hidden_state[:, 0, :]" in official


def test_snapshot_integrity_rejects_tampering_without_real_weights(tmp_path):
    """Tiny temporary byte strings exercise validation; no usable checkpoint exists."""
    from nandatown.piguard_guard import MODEL_REVISION
    names = ["model.safetensors", "modeling_piguard.py", "config.json", "tokenizer.json",
             "tokenizer_config.json", "special_tokens_map.json", "added_tokens.json", "spm.model"]
    files = {}
    for name in names:
        data = b"synthetic integrity test bytes"
        (tmp_path / name).write_bytes(data)
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    (tmp_path / "manifest.json").write_text(json.dumps({
        "repository": "leolee99/PIGuard", "revision": MODEL_REVISION, "files": files}), encoding="utf-8")
    assert verify_snapshot(tmp_path)[0] == tmp_path.resolve()
    (tmp_path / "modeling_piguard.py").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity mismatch"):
        verify_snapshot(tmp_path)


def test_cli_addition_and_baseline_defaults(capsys):
    from nandatown.cli import main
    with pytest.raises(SystemExit) as exc:
        main(["web-evidence-piguard", "--help"])
    assert exc.value.code == 0
    assert "--model-dir" in capsys.readouterr().out


def test_real_transport_parameter_filter_with_mock_http():
    import httpx
    from nandatown.web_evidence import OpenAIClient
    class Log:
        def __init__(self): self.events = []
        def emit(self, *args): self.events.append(args)
    captured = []
    def endpoint(request):
        payload = json.loads(request.read())
        captured.append(payload)
        assert "reasoning_effort" not in payload and "thinking" not in payload
        assert payload["temperature"] == 0
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                                         "usage": {"total_tokens": 3}})
    log = Log()
    transport = RecordingTransport(log)
    transport.inner.close()
    transport.inner = httpx.MockTransport(endpoint)
    with httpx.Client(base_url="https://api.openai.com/v1", transport=transport,
                      headers={"Authorization": "Bearer test-secret"}) as http:
        client = OpenAIClient("gpt-4.1", http=http)
        client.complete([{"role": "user", "content": "test"}], json_mode=True)
    assert len(captured) == 1
    assert "test-secret" not in json.dumps(log.events)


def test_official_class_dispatch_with_mock_tensor_engine(monkeypatch):
    """Execute the author's real Python forward, but no trained model or real tensors."""
    import contextlib
    import sys
    import types
    from nandatown import piguard_guard as guard
    root = Path(__file__).parent / "fixtures/piguard"
    identity = json.loads((root / "SOURCE.json").read_text(encoding="utf-8"))
    assert hashlib.sha256((root / "modeling_piguard.py").read_bytes().replace(b"\r\n", b"\n")).hexdigest() == identity["sha256_lf"]
    monkeypatch.setattr(guard, "verify_snapshot", lambda directory: (root, {"files": {}}))
    captured = {}
    class Config:
        id2label = {0: "benign", 1: "injection"}
        hidden_size, num_labels = 768, 2
        @classmethod
        def register_for_auto_class(cls, *args): pass
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            assert kwargs["local_files_only"]
            return cls()
    class Logits:
        shape = (1, 2)
        def __getitem__(self, key): return self
        def tolist(self): return [0., 3.]
    class Hidden:
        def __getitem__(self, key):
            captured["pooling"] = key
            return "mock-cls-hidden-state"
    class Base:
        def __init__(self, config):
            self.training = True
            self.deberta = lambda **kwargs: types.SimpleNamespace(last_hidden_state=Hidden())
        @classmethod
        def register_for_auto_class(cls, *args): pass
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            captured["model_class"] = cls.__name__
            captured["model_kwargs"] = kwargs
            return cls(kwargs["config"])
        def to(self, device):
            captured["device"] = device
            return self
        def eval(self):
            self.training = False
            return self
        def __call__(self, **kwargs): return self.forward(**kwargs)
    class Tokenizer:
        is_fast = True
        def prepare_for_model(self, ids, **kwargs):
            return {"input_ids": [1]+ids+[2], "attention_mask": [1]*(len(ids)+2)}
    fake_transformers = types.ModuleType("transformers")
    fake_transformers.DebertaV2Config = Config
    fake_transformers.DebertaV2ForSequenceClassification = Base
    fake_transformers.AutoTokenizer = types.SimpleNamespace(from_pretrained=lambda *args, **kwargs: Tokenizer())
    fake_outputs = types.ModuleType("transformers.modeling_outputs")
    fake_outputs.SequenceClassifierOutput = lambda **kwargs: types.SimpleNamespace(**kwargs)
    fake_torch = types.ModuleType("torch")
    fake_torch.nn = types.SimpleNamespace(Linear=lambda *args: lambda hidden: Logits())
    fake_torch.float32, fake_torch.long = "mock-fp32", "mock-long"
    fake_torch.tensor = lambda value, **kwargs: value
    fake_torch.inference_mode = contextlib.nullcontext
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    monkeypatch.setitem(sys.modules, "transformers.modeling_outputs", fake_outputs)
    backend = guard.OfficialBackend(root)
    logits, length = backend.classify([15, 20])
    assert logits == [0, 3] and length == 4
    assert captured["model_class"] == "PIGuard"
    assert captured["device"] == "cpu"
    assert captured["model_kwargs"]["local_files_only"]
    assert captured["model_kwargs"]["use_safetensors"]
    assert backend.model.training is False
    assert Path(backend.identity["class_file"]) == root/"modeling_piguard.py"
    assert captured["pooling"] == (slice(None), 0, slice(None))
