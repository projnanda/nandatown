"""Adaptive Security Routing around unchanged Full-Page agents and PIGuard."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time

import httpx

from .web_evidence import (RunConfig, EventLog, OpenAIClient, estimate_tokens, _write_json)
from .piguard_guard import (inspect_packet, fields_from_packet, make_report, finalize)


@dataclass(frozen=True)
class ModelPolicy:
    model: str
    reason: str
    reasoning_effort: str | None = None
    temperature: int | None = 0

    def parameters(self):
        payload = {"model": self.model}
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        return payload

    def record(self):
        return {"model": self.model, "reason": self.reason,
                "reasoning_effort": self.reasoning_effort, "temperature": self.temperature,
                "temperature_sent": self.temperature is not None,
                "request_parameters": self.parameters()}


COLLECTOR_POLICY = ModelPolicy("gpt-4.1", "fixed_agent1")


def select_route(report):
    positive = (report.get("detection") == "injection_detected" or
                any(w.get("label") == "injection" for w in report.get("windows", [])))
    if positive:
        return ModelPolicy("gpt-6-luna", "injection_detected", "medium", None)
    if (report.get("status") == "complete" and report.get("detection") == "benign"
            and not report.get("errors")):
        return ModelPolicy("gpt-4.1", "complete_without_detection")
    return ModelPolicy("gpt-6-luna", "guard_incomplete", "medium", None)


def annotated_messages(messages, annotations, budget):
    """Attach defense-owned metadata without mutating the source packet or prompts."""
    result = copy.deepcopy(messages)
    payload = json.loads(result[1]["content"])
    payload["security_annotations"] = copy.deepcopy(annotations)
    result[1]["content"] = json.dumps(payload, ensure_ascii=False)
    count = estimate_tokens(json.dumps(result, ensure_ascii=False))
    if count > budget:
        raise RuntimeError("Agent 2 annotated input exceeds its token budget (capacity error; not security blocking)")
    return result, count


class AnnotationLog(EventLog):
    annotations = None
    budget = 6000
    policies = None

    def emit(self, agent, kind, detail, message=""):
        if agent == "agent-2" and kind == "model_request" and self.annotations is not None:
            actual, count = annotated_messages(detail["messages"], self.annotations, self.budget)
            detail = {**detail, "messages": actual, "estimated_input_tokens": count,
                      "security_annotations_applied": True}
        if kind == "model_request" and self.policies and agent in self.policies:
            # Baseline config fields describe its original contract. Log actual
            # routing/transport parameters here rather than stale none/zero values.
            detail = {**detail, **self.policies[agent].record()}
            detail.pop("thinking", None)  # Not an OpenAI wire parameter.
        return super().emit(agent, kind, detail, message)


class AnnotatedModel:
    def __init__(self, service, report, budget, output, log, policy=None):
        self.service, self.report, self.budget = service, report, budget
        self.output, self.log = output, log
        self.requests = []
        self.policy = policy

    def complete(self, messages, **kwargs):
        actual, count = annotated_messages(messages, self.report["security_annotations"], self.budget)
        self.requests.append({"attempt": len(self.requests)+1, "messages": actual,
                              "estimated_input_tokens": count, "json_mode": kwargs.get("json_mode", False),
                              **(self.policy.record() if self.policy else {})})
        _write_json(self.output/"agent2_input.json", {"requests": self.requests})
        self.log.emit("agent-2", "actual_model_request", self.requests[-1])
        return self.service.complete(actual, **kwargs)


class RecordingTransport(httpx.BaseTransport):
    """Apply model-specific wire parameters without changing baseline agents."""
    def __init__(self, log, *, policy=None, actor="agent-api"):
        self.inner = httpx.HTTPTransport(retries=0)
        self.log = log
        self.policy, self.actor = policy, actor
        self.usage_details = []

    def handle_request(self, request):
        payload = json.loads(request.read())
        if self.policy:
            if payload["model"] != self.policy.model:
                raise ValueError("Client model differs from the selected route")
            payload.pop("temperature", None)
            payload.pop("reasoning_effort", None)
            payload.update(self.policy.parameters())
        elif payload["model"].startswith("gpt-4.1"):
            payload.pop("reasoning_effort", None)
        self.log.emit(self.actor, "wire_request", {"endpoint": str(request.url), "payload": payload})
        # Only payload and public endpoint are logged; headers never are.
        headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
        outbound = httpx.Request(request.method, request.url, headers=headers, json=payload,
                                 extensions=request.extensions)
        response = self.inner.handle_request(outbound)
        response.read()
        detail = {"status_code": response.status_code, "model": payload["model"]}
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            if isinstance(body.get("usage"), dict):
                usage = copy.deepcopy(body["usage"])
                self.usage_details.append(usage)
                detail["usage"] = usage
            error = body.get("error")
            if isinstance(error, dict):
                detail["error"] = {k: error[k][:80] for k in ("type", "code", "param")
                                   if isinstance(error.get(k), str)}
        self.log.emit(self.actor, "wire_response", detail)
        return response

    def close(self):
        self.inner.close()


def open_agent_service(config, log, *, policy=COLLECTOR_POLICY, actor="agent-1"):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY is required")
    transport = RecordingTransport(log, policy=policy, actor=actor)
    http = httpx.Client(base_url=config.base_url, headers={"Authorization": "Bearer "+key},
                        transport=transport, trust_env=False)
    service = OpenAIClient(policy.model, config.base_url, http=http,
                           temperature=0, reasoning_effort=policy.reasoning_effort or "none")
    service.usage_details = transport.usage_details
    return service, http


class PIGuardAdapter:
    """Original packet plus classifier warnings and the retained adaptive route."""

    name = "piguard"
    log_type = AnnotationLog

    def __init__(self, model_dir, guard=inspect_packet, field_timeout=120.0,
                 total_timeout=600.0, service_factory=None, injected_model=None):
        self.model_dir, self.guard = model_dir, guard
        self.field_timeout, self.total_timeout = field_timeout, total_timeout
        self.factory = service_factory or open_agent_service
        self.injected_model = injected_model

    def configuration(self, config):
        return {"guard": {"model_dir": str(Path(self.model_dir).resolve()),
                    "policy": "detection_and_warning", "field_timeout_seconds": self.field_timeout,
                    "total_timeout_seconds": self.total_timeout},
                "routing_policy": {"agent1": COLLECTOR_POLICY.record(),
                    "agent2_complete_benign": ModelPolicy("gpt-4.1", "complete_without_detection").record(),
                    "agent2_injection": ModelPolicy("gpt-6-luna", "injection_detected", "medium", None).record(),
                    "agent2_incomplete": ModelPolicy("gpt-6-luna", "guard_incomplete", "medium", None).record(),
                    "fallback": False},
                "agent_transport_retries": 1, "guard_retries": 0, "guard_time_excluded": True,
                "agents_contract_note": "Effective route parameters override baseline none/zero placeholders."}

    def open_collector(self, config, log):
        log.policies = {"agent-1": COLLECTOR_POLICY}
        return self.factory(config, log, policy=COLLECTOR_POLICY, actor="agent-1")

    def defend(self, packet, output, log):
        from .evidence_workflow import DefenseOutcome
        try:
            report = self.guard(packet, self.model_dir, field_timeout=self.field_timeout,
                total_timeout=self.total_timeout, on_event=lambda event: log.emit("piguard", "progress", event))
        except Exception as exc:
            report = make_report(fields_from_packet(packet))
            report["errors"].append({"type": type(exc).__name__})
            report = finalize(report)
        _write_json(output / "piguard.json", report)
        policy = select_route(report)
        route = {**policy.record(), "guard_status": report["status"],
                 "guard_detection": report["detection"], "fallback": False,
                 "suspicious_windows": sum(w["label"] == "injection" for w in report["windows"])}
        return DefenseOutcome(packet, report["status"], {
            "guard_detection": report["detection"],
            "guard_resources": {"usage": report["usage"], "resources": report.get("resources", {})}}, route)

    def open_answerer(self, config, service, outcome, output, log):
        report = json.loads((output / "piguard.json").read_text(encoding="utf-8"))
        policy = select_route(report)
        log.policies = {"agent-1": COLLECTOR_POLICY, "agent-2": policy}
        log.annotations, log.budget = report["security_annotations"], config.input_token_budget
        log.emit("runner", "model_routing_selected", outcome.model_policy)
        answer_config = config.model_copy(update={"model": policy.model})
        handle = None
        if self.injected_model is not None:
            answer_service = self.injected_model
        else:
            answer_service, handle = self.factory(answer_config, log, policy=policy, actor="agent-2")
        wrapped = AnnotatedModel(answer_service, report, config.input_token_budget, output, log, policy)
        wrapped.usage_details = getattr(answer_service, "usage_details", [])
        return wrapped, handle, answer_config

    def close(self):
        pass


def run(config: RunConfig, out_dir, model_dir, *, model=None, browser=None,
        guard=inspect_packet, field_timeout=120.0, total_timeout=600.0,
        service_factory=None):
    """Compatibility entry point retaining warning-only adaptive security routing."""
    from .evidence_workflow import run_workflow
    from .promptarmor_workflow import validate_endpoint
    validate_endpoint(config.base_url)
    if config.model != "gpt-4.1" or config.provider != "openai":
        raise ValueError("Adaptive Security Routing requires fixed Agent 1 gpt-4.1 / openai")
    adapter = PIGuardAdapter(model_dir, guard, field_timeout, total_timeout,
                             service_factory, injected_model=model)
    return run_workflow(config, out_dir, adapter, model=model, browser=browser)


def cli(args):
    config = RunConfig(url=args.url, question=args.question, model=args.model,
        provider="openai", base_url=getattr(args, "base_url", None) or "https://api.openai.com/v1", headed=args.headed,
        max_pages=args.max_pages, max_steps=args.max_steps, input_token_budget=args.input_budget,
        page_timeout_seconds=args.page_timeout, run_timeout_seconds=args.run_timeout)
    output = args.out or str(Path("runs")/time.strftime("piguard-%Y%m%d-%H%M%S"))
    result = run(config, output, args.model_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 1
