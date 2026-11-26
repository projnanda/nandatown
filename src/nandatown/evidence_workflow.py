"""Shared lifecycle for local two-role evidence experiments, outside Town bundles."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Callable, Protocol
import uuid
from urllib.parse import urlsplit

from .web_evidence import (
    AnswerGenerator, BrowserService, EvidenceCollector, EvidencePacket, EventLog,
    ModelService, PlaywrightBrowser, RunConfig, TerminalRenderer, _write_json,
    create_model_service,
)

SCHEMA_VERSION = "web-evidence-experiment@1"


@dataclass(frozen=True)
class DefenseOutcome:
    """An immutable handoff decision; source provenance always names the original."""

    packet: EvidencePacket
    status: str
    audit: dict[str, Any] = field(default_factory=dict)
    model_policy: dict[str, Any] = field(default_factory=dict)


class DefenseAdapter(Protocol):
    """Method-specific behavior injected into the shared lifecycle."""

    name: str
    log_type: type[EventLog]

    def configuration(self, config: RunConfig) -> dict[str, Any]: ...
    def open_collector(self, config: RunConfig, log: EventLog) -> tuple[Any, Any]: ...
    def defend(self, packet: EvidencePacket, output: Path, log: EventLog) -> DefenseOutcome: ...
    def open_answerer(self, config: RunConfig, service: ModelService | None,
                      outcome: DefenseOutcome, output: Path, log: EventLog) -> tuple[Any, Any, RunConfig]: ...
    def close(self) -> None: ...


class BaselineLog(EventLog):
    """Record effective GPT-4.1 controls rather than baseline contract placeholders."""

    def emit(self, agent, kind, detail=None, message=None):
        if (kind == "model_request" and detail and detail.get("provider") == "openai"
                and detail.get("model", "").startswith("gpt-4.1")):
            detail = {**detail, "reasoning_effort": None}
            detail.pop("thinking", None)
        return super().emit(agent, kind, detail, message)


class BaselineAdapter:
    """Identity defense, preserving the baseline provider and retry behavior."""

    name = "baseline"
    log_type = BaselineLog

    def configuration(self, config):
        self.config = config
        return {"defense": self.name, "agent_transport_retries": 1,
                "guard_time_excluded": False,
                "effective_agent_parameters": {"model": config.model,
                    "temperature": 0, "reasoning_effort": (None if config.provider == "openai"
                        and config.model.startswith("gpt-4.1") else "none"),
                    "thinking": "disabled" if config.provider == "deepseek" else "omitted"}}

    def open_collector(self, config, log):
        service = create_model_service(config)
        return service, service

    def defend(self, packet, output, log):
        return DefenseOutcome(packet, "not_applicable", model_policy={
            "model": self.config.model, "reason": "fixed_agent2",
            "temperature": 0, "reasoning_effort": (None if self.config.provider == "openai"
                and self.config.model.startswith("gpt-4.1") else "none")})

    def open_answerer(self, config, service, outcome, output, log):
        return service, None, config

    def close(self):
        pass


class RecordedModel:
    """Persist the exact Answerer messages, including structured-output repairs."""

    def __init__(self, service, output, log):
        self.service, self.output, self.log = service, output, log
        self.requests = []
        self.usage_details = getattr(service, "usage_details", [])

    def complete(self, messages, **kwargs):
        import copy
        entry = {"attempt": len(self.requests) + 1, "messages": copy.deepcopy(messages),
                 "json_mode": kwargs.get("json_mode", False)}
        self.requests.append(entry)
        _write_json(self.output / "agent2_input.json", {"requests": self.requests})
        self.log.emit("agent-2", "actual_model_request", entry)
        return self.service.complete(messages, **kwargs)


def run_workflow(config: RunConfig, out_dir: str | Path, adapter: DefenseAdapter,
                 *, model: ModelService | None = None, browser: BrowserService | None = None,
                 renderer: TerminalRenderer | None = None, packet_path: str | Path | None = None,
                 service_factory: Callable | None = None,
                 browser_factory: Callable = PlaywrightBrowser) -> dict[str, Any]:
    """Collect, defend and answer once, retaining method-specific transport policy.

    Injected services and browsers permit deterministic tests without inference.
    Defense time is independently budgeted for defended methods only.
    """
    endpoint = urlsplit(config.base_url)
    if (endpoint.scheme not in ('http', 'https') or not endpoint.hostname
            or endpoint.username is not None or endpoint.password is not None
            or endpoint.query or endpoint.fragment):
        raise ValueError("Model endpoint must not contain credentials, query or fragment")
    output = Path(out_dir)
    if output.is_symlink() or (output.exists() and
                              (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("Output is not empty or is not a real directory")
    output.mkdir(parents=True, exist_ok=True)
    (output / "pages").mkdir()
    (output / "events.jsonl").write_text("", encoding="utf-8")
    log = adapter.log_type(output / "events.jsonl", renderer)
    _write_json(output / "agent2_input.json", {"requests": []})
    settings = adapter.configuration(config)
    _write_json(output / "config.json", {"schema_version": SCHEMA_VERSION,
                "defense": adapter.name, "agents": config.model_dump(), **settings})
    began = time.monotonic()
    deadline = began + config.run_timeout_seconds
    phase = "setup"
    service = model
    answer_service = collector = generator = packet = None
    handles = []
    result = {"schema_version": SCHEMA_VERSION, "workflow_version": config.workflow_version,
              "run_id": "web-" + uuid.uuid4().hex, "defense": adapter.name,
              "status": "error", "execution_status": "error", "answer": None,
              "question": config.question, "stop_reason": "Run did not start",
              "security_check_status": "not_started", "routing": None,
              "models": {"agent1": None, "agent2": None},
              "pages_visited": 0, "steps_used": 0, "evidence_count": 0,
              "token_usage": {"agent1": {}, "guard": {}, "agent2": {}},
              "usage_details": {"agent1": [], "agent2": []},
              "timing_seconds": {"agent1": 0.0, "guard": 0.0, "agent2": 0.0},
              "factual_support_assessed": False,
              "limitations": ["Citation IDs are checked; semantic support is not assessed.",
                  "Agent 1 sees the original page before the defense.",
                  "Metadata is not screened. This is not a Town verifiable bundle.",
                  "Hosted model behavior is mutable; temperature zero is not determinism."],
              "artifacts": {"config": "config.json", "events": "events.jsonl",
                  "actual_agent2_input": "agent2_input.json",
                  "pages": "pages/", "original_packet": "not_written",
                  "result": "result.json"}}
    if adapter.name == "llm":
        result.update(guardrail="promptarmor", guardrail_version="promptarmor-handoff@1")
    elif adapter.name == "piguard":
        result["workflow_version"] = "adaptive-security-routing@1"
    log.emit("runner", "run_started", {"run_id": result["run_id"], "defense": adapter.name})
    try:
        if packet_path is not None:
            raw = Path(packet_path).read_bytes()
            packet = EvidencePacket.model_validate_json(raw)
            if packet.question != config.question:
                raise ValueError("Replay question differs from packet")
            (output / "handoff.json").write_bytes(raw)
        else:
            phase = "agent1"
            if service is None:
                service, handle = (service_factory or adapter.open_collector)(config, log)
                if handle is not None:
                    handles.append(handle)
            result["models"]["agent1"] = config.model
            browser = browser or browser_factory(config, deadline=deadline)
            browser.start()
            collector = EvidenceCollector(config, service, browser, log,
                lambda page: _write_json(output / "pages" / f"{page.page_id}.json", page), deadline)
            tick = time.monotonic()
            try:
                packet = collector.run()
            finally:
                result["timing_seconds"]["agent1"] = time.monotonic() - tick
            _write_json(output / "handoff.json", packet)
        result["artifacts"]["original_packet"] = "handoff.json"
        result["evidence_count"] = len(packet.items)
        log.emit("runner", "evidence_handoff", {"packet": packet.model_dump()})
        if not packet.items:
            result.update(status="limit_reached" if "limit" in packet.completion_note else "partial",
                          stop_reason=packet.completion_note or "No evidence collected")
            return result
        if time.monotonic() >= deadline:
            result.update(status="limit_reached", stop_reason="Agent time budget exhausted")
            return result
        phase = "guard"
        original = packet.model_dump_json()
        tick = time.monotonic()
        try:
            outcome = adapter.defend(packet, output, log)
        finally:
            duration = time.monotonic() - tick
            result["timing_seconds"]["guard"] = duration
            if adapter.name != "baseline":
                deadline += duration
        if packet.model_dump_json() != original:
            raise RuntimeError("Defense mutated the original packet")
        result["security_check_status"] = outcome.status
        result.update(outcome.audit)
        result["routing"] = outcome.model_policy or None
        _write_json(output / "agent2_handoff.json", outcome.packet)
        result["artifacts"]["delivered_packet"] = "agent2_handoff.json"
        phase = "agent2"
        answer_service, handle, answer_config = adapter.open_answerer(
            config, service, outcome, output, log)
        if handle is not None:
            handles.append(handle)
        result["models"]["agent2"] = answer_config.model
        if adapter.name != "piguard":
            answer_service = RecordedModel(answer_service, output, log)
        generator = AnswerGenerator(answer_config, answer_service, log, deadline)
        tick = time.monotonic()
        try:
            answer = generator.run(outcome.packet)
        finally:
            result["timing_seconds"]["agent2"] = time.monotonic() - tick
        result.update(status="completed" if packet.complete else "partial",
                      answer=answer.model_dump(), stop_reason=packet.completion_note or
                      "Agent 2 returned a structurally valid cited answer; factual accuracy not graded")
    except Exception as exc:
        status = "defense_error" if phase == "guard" else "error"
        result.update(status=status, stop_reason=f"{phase} failed ({type(exc).__name__})")
        safe_errors = {
            "Agent 1 base context exceeds its token budget",
            "Agent 2 input exceeds its token budget",
            "Agent 2 annotated input exceeds its token budget (capacity error; not security blocking)",
        }
        if type(exc) is RuntimeError and str(exc) in safe_errors:
            result["stop_reason"] = str(exc)
        if phase == "guard":
            result["security_check_status"] = "error"
        log.emit("runner", "run_error", {"phase": phase, "type": type(exc).__name__})
    finally:
        if collector:
            result.update(pages_visited=len(collector.pages), steps_used=collector.steps)
            result["token_usage"]["agent1"] = dict(collector.usage)
        if generator:
            result["token_usage"]["agent2"] = dict(generator.usage)
        result["token_usage"]["guard"] = dict(getattr(adapter, "usage", {}))
        result["usage_details"] = {"agent1": getattr(service, "usage_details", []),
                                   "agent2": getattr(answer_service, "usage_details", [])}
        for resource in ([browser] if browser is not None else []) + list(reversed(handles)) + [adapter]:
            try:
                resource.close()
            except Exception as exc:
                log.emit("runner", "cleanup_error", {"type": type(exc).__name__})
                result.update(status="error", stop_reason="Resource cleanup failed")
        result["execution_status"] = "error" if result["status"] == "defense_error" else result["status"]
        for name in ("promptarmor.json", "piguard.json", "manifest.json"):
            if (output / name).exists():
                result["artifacts"][name.removesuffix('.json')] = name
        result["timing_seconds"]["total"] = time.monotonic() - began
        _write_json(output / "result.json", result)
        log.emit("runner", "run_finished", result)
    return result
