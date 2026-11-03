# SPDX-License-Identifier: MIT
# Copyright (c) 2026 nanda-resilience contributors
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

from nandatown.records import StageResult, TownEvent
from nandatown.sim.scenario import ScenarioSpec
from nandatown.sim.validators import Trace, validator

from . import EVALUATOR
from .model import POLICIES, Provider, World, build_spec, world_from_spec
from .policies import DEPENDENCY_FAILURES, Candidate, choose


class InvalidEvidence(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidEvidence(message)


def same(actual: Any, expected: Any, context: str) -> None:
    # Equality alone accepts True == 1; JSON preserves that distinction.
    from nandatown.records import canonical_json
    require(canonical_json(actual) == canonical_json(expected),
            f"{context}: expected {expected!r}, observed {actual!r}")


@dataclass
class Attempt:
    task: str
    buyer: str
    provider: Provider
    group: str
    selected_at: float
    selected_index: int
    admitted_at: float | None = None
    backend_result: dict[str, Any] | None = None
    provider_result: dict[str, Any] | None = None
    finished: bool = False


@dataclass
class TaskEvidence:
    buyer: str
    created: float
    attempts: list[str] = field(default_factory=list)
    tried: set[str] = field(default_factory=set)
    failed_groups: set[str] = field(default_factory=set)
    paid: int = 0
    terminal: TownEvent | None = None


@dataclass
class Analysis:
    world: World
    policy: str
    tasks: dict[str, TaskEvidence]
    attempts: dict[str, Attempt]
    events: list[TownEvent]


def analyze(spec: ScenarioSpec, events: list[TownEvent],
            run_id: str | None = None) -> Analysis:
    world = world_from_spec(spec)
    settings = world.settings
    policy = spec.layers["coordination"].removeprefix("resilience.").removesuffix(".v1")
    require(policy in POLICIES, "unsupported coordination policy")
    expected_spec = build_spec(settings)
    expected_spec.layers["coordination"] = spec.layers["coordination"]
    require(spec.agents == expected_spec.agents, "population differs from declared world")
    require(spec.layers == expected_spec.layers and not spec.faults,
            "this evaluator supports only the declared layer stack and service outages")
    require(spec.max_time == world.end_time, "run horizon differs from declared world")
    require(bool(events), "empty trace")
    ids = [e.event_id for e in events]
    require(len(ids) == len(set(ids)), "duplicate event IDs")
    require(all(e.run_id == (run_id or events[0].run_id) for e in events),
            "cross-run event")
    require(all(math.isfinite(e.at) and e.at >= 0 for e in events),
            "invalid logical time")
    require(all(a.at <= b.at for a, b in zip(events, events[1:])),
            "events not in causal time order")
    started = [e for e in events if e.kind == "resilience.started"]
    require(len(started) == 1 and started[0].observer == "experiment"
            and started[0].subject == "experiment", "missing/duplicate experiment start")
    same(started[0].detail, {
        "settings": settings.model_dump(),
        "providers": [p.model_dump(mode="json") for p in world.providers],
    }, "experiment world")
    finished = [e for e in events if e.kind == "resilience.finished"]
    require(len(finished) == 1 and finished[0].at == world.end_time
            and finished[0].observer == "experiment", "missing/invalid experiment finish")
    require(events[-1].kind == "run_finished" and events[-1].at == world.end_time,
            "run did not finish at its declared horizon")
    providers = {p.name: p for p in world.providers}
    expected_availability = sorted(
        (name, at, down)
        for name, windows in (
            list(world.dependency_outages.items())
            + [(p.name, p.outages) for p in world.providers]
        )
        for start, end in windows
        for at, down in [(start, True), (end, False)]
    )
    actual_availability = []
    for e in events:
        if e.kind == "resilience.availability":
            require(e.observer == e.subject and type(e.detail.get("down")) is bool,
                    "malformed availability event")
            actual_availability.append((e.subject, e.at, e.detail["down"]))
    same(sorted(actual_availability), expected_availability, "fault schedule")

    sent: dict[tuple[str, str], tuple[int, TownEvent]] = {}
    delivered: dict[str, tuple[int, TownEvent]] = {}
    used_messages: set[tuple[str, str]] = set()
    for i, event in enumerate(events):
        if event.kind == "message_sent":
            kind = event.detail.get("kind")
            require(kind in {"service", "compute", "compute_result", "service_result"},
                    "unexpected message kind")
            body = event.detail.get("body")
            require(isinstance(body, dict) and isinstance(body.get("ref"), str),
                    "message lacks attempt reference")
            key = (body["ref"], kind)
            require(key not in sent, f"duplicate message {key}")
            sent[key] = (i, event)
        elif event.kind == "message_delivered":
            require(event.subject not in delivered, "duplicate message delivery")
            delivered[event.subject] = (i, event)

    def message(ref: str, kind: str, sender: str, to: str,
                body: dict[str, Any], before: int, after: int):
        key = (ref, kind)
        require(key in sent, f"missing {kind} for {ref}")
        send_index, send_event = sent[key]
        require(send_event.subject in delivered, f"undelivered {kind} for {ref}")
        delivery_index, delivery = delivered[send_event.subject]
        require(after < send_index < delivery_index < before,
                f"noncausal {kind} for {ref}")
        require(send_event.observer == sender and send_event.detail["to"] == to,
                f"wrong parties for {kind} {ref}")
        same(send_event.detail["body"], body, f"message body {ref} {kind}")
        same(delivery.detail, {"to": to, "kind": kind}, "delivery binding")
        require(delivery.observer == "town", "delivery observer is not town")
        used_messages.add(key)
        return delivery_index, delivery.at

    def down(name: str, at: float) -> bool:
        windows = (providers[name].outages if name in providers
                   else world.dependency_outages[name])
        return any(start <= at < end for start, end in windows)

    expected_tasks = {
        f"buyer-{buyer:02d}/task-{round_index:03d}":
            (f"buyer-{buyer:02d}", round_index,
             1.0 + round_index * settings.interval)
        for buyer in range(settings.buyers)
        for round_index in range(settings.rounds)
    }
    tasks: dict[str, TaskEvidence] = {}
    attempts: dict[str, Attempt] = {}
    registered: set[str] = set()
    inflight: Counter[str] = Counter()
    reservations: dict[str, str] = {}
    backend_active: Counter[str] = Counter()
    backend_closed: set[str] = set()
    result_indices: dict[tuple[str, str], int] = {}
    recognized = {
        "started", "finished", "availability", "task_created", "selected",
        "backend_admitted", "backend_result", "provider_result",
        "attempt_finished", "task_finished",
    }
    for index, event in enumerate(events):
        kind, ref, detail = event.kind, event.subject, event.detail
        if kind == "card_registered" and ref in providers:
            require(ref not in registered and detail.get("verified") is True,
                    "duplicate or unverified provider listing")
            registered.add(ref)
        if kind.startswith("resilience."):
            require(kind.split(".", 1)[1] in recognized, "unknown resilience event")
        if kind == "resilience.task_created":
            require(ref in expected_tasks and ref not in tasks, "unexpected/duplicate task")
            buyer, round_index, at = expected_tasks[ref]
            require(event.observer == buyer and event.at == at, "task attribution/time")
            same(detail, {"round": round_index}, "task creation")
            tasks[ref] = TaskEvidence(buyer, at)
        elif kind == "resilience.selected":
            task_id = detail.get("task")
            require(task_id in tasks, "selection before task creation")
            task = tasks[task_id]
            require(task.terminal is None and len(task.attempts) < settings.attempts,
                    "selection after termination or beyond retry budget")
            require(not task.attempts or attempts[task.attempts[-1]].finished,
                    "overlapping attempts for one task")
            require(registered == set(providers), "selection before provider discovery")
            number = len(task.attempts) + 1
            require(ref == f"{task_id}/attempt-{number}" and ref not in attempts,
                    "attempt identity/sequence")
            require(event.observer == task.buyer, "selection attribution")
            candidates = [
                Candidate(p.name, p.price_cents, 0,
                          p.dependency if p.disclosed or policy == "oracle" else None)
                for p in world.providers if p.name not in task.tried
            ]
            selected = choose(
                policy, candidates, seed=settings.seed, task=task_id,
                attempt=number, inflight=inflight, reservations=reservations,
                failed_groups=task.failed_groups,
            )
            same(detail, {
                "task": task_id, "attempt": number, "provider": selected.name,
                "price_cents": selected.price, "policy": policy,
                "visible_dependency": selected.dependency,
                "candidate_count": len(candidates),
            }, "policy decision")
            group = selected.dependency or "unknown"
            attempts[ref] = Attempt(task_id, task.buyer, providers[selected.name],
                                    group, event.at, index)
            task.attempts.append(ref)
            task.tried.add(selected.name)
            reservations[ref] = group
            inflight[selected.name] += 1
        elif kind in {"resilience.backend_admitted", "resilience.backend_result",
                      "resilience.provider_result", "resilience.attempt_finished"}:
            require(ref in attempts, "result without a selected attempt")
            attempt = attempts[ref]
            p = attempt.provider
            require(not attempt.finished, "result after attempt completion")
            if kind.startswith("resilience.backend_"):
                require(event.observer == p.dependency, "wrong backend attribution")
                service_index, _ = message(
                    ref, "service", attempt.buyer, p.name,
                    {"ref": ref, "task": attempt.task}, index, attempt.selected_index)
                _, arrival_at = message(
                    ref, "compute", p.name, p.dependency, {"ref": ref},
                    index, service_index)
                if kind == "resilience.backend_admitted":
                    require(attempt.admitted_at is None and ref not in backend_closed,
                            "duplicate/late admission")
                    require(not down(p.dependency, event.at), "admission during outage")
                    require(event.at == arrival_at, "admission did not follow request arrival")
                    backend_active[p.dependency] += 1
                    require(backend_active[p.dependency] <= settings.dependency_capacity,
                            "backend capacity exceeded")
                    same(detail, {"provider": p.name,
                                  "active": backend_active[p.dependency]}, "admission")
                    attempt.admitted_at = event.at
                else:
                    require(ref not in backend_closed, "duplicate backend result")
                    backend_closed.add(ref)
                    if attempt.admitted_at is not None:
                        require(math.isclose(event.at - attempt.admitted_at,
                                             settings.service_time, abs_tol=1e-9),
                                "incorrect service duration")
                        backend_active[p.dependency] -= 1
                        success = not down(p.dependency, event.at)
                        reason = "ok" if success else "unavailable"
                    else:
                        require(event.at == arrival_at, "rejection did not follow request arrival")
                        success = False
                        reason = "unavailable" if down(p.dependency, event.at) else "capacity"
                        require(reason == "unavailable" or
                                backend_active[p.dependency] >= settings.dependency_capacity,
                                "unjustified capacity rejection")
                    same(detail, {"provider": p.name, "success": success,
                                  "reason": reason}, "backend result")
                    attempt.backend_result = {"ref": ref, "success": success,
                                              "reason": reason}
                    result_indices[(ref, "backend")] = index
            elif kind == "resilience.provider_result":
                require(event.observer == p.name and attempt.provider_result is None,
                        "wrong/duplicate provider result")
                _, service_at = message(ref, "service", attempt.buyer, p.name,
                                        {"ref": ref, "task": attempt.task}, index,
                                        attempt.selected_index)
                if attempt.backend_result is not None:
                    _, response_at = message(
                        ref, "compute_result", p.dependency, p.name,
                        attempt.backend_result, index, result_indices[(ref, "backend")])
                    require(event.at == response_at, "provider result delayed or premature")
                    success = attempt.backend_result["success"]
                    reason = attempt.backend_result["reason"]
                    if down(p.name, event.at):
                        success, reason = False, "provider_unavailable"
                else:
                    require(event.at == service_at, "provider rejection before/after arrival")
                    require(down(p.name, event.at), "provider rejected without outage")
                    require((ref, "compute") not in sent, "rejected provider also forwarded")
                    success, reason = False, "provider_unavailable"
                same(detail, {"buyer": attempt.buyer, "success": success,
                              "reason": reason}, "provider result")
                attempt.provider_result = {
                    "ref": ref, "task": attempt.task,
                    "success": success, "reason": reason,
                }
                result_indices[(ref, "provider")] = index
            else:
                require(event.observer == attempt.buyer
                        and attempt.provider_result is not None, "unbound buyer result")
                _, response_at = message(
                    ref, "service_result", p.name, attempt.buyer,
                    attempt.provider_result, index, result_indices[(ref, "provider")])
                require(event.at == response_at, "buyer result delayed or premature")
                same(detail, {
                    "task": attempt.task, "provider": p.name,
                    "success": attempt.provider_result["success"],
                    "reason": attempt.provider_result["reason"],
                }, "attempt result")
                attempt.finished = True
                del reservations[ref]
                inflight[p.name] -= 1
                if not detail["success"] and detail["reason"] in DEPENDENCY_FAILURES:
                    tasks[attempt.task].failed_groups.add(attempt.group)
        elif kind == "payment_settled":
            require(ref in tasks, "payment for unknown task")
            task = tasks[ref]
            require(bool(task.attempts) and task.paid == 0, "duplicate/unbound payment")
            last = attempts[task.attempts[-1]]
            require(last.finished and last.provider_result["success"],
                    "payment without successful response")
            same(detail, {"from": task.buyer, "to": last.provider.name,
                          "cents": last.provider.price_cents}, "payment binding")
            require(event.observer == "town", "payment not observed by ledger")
            task.paid = last.provider.price_cents
        elif kind == "resilience.task_finished":
            require(ref in tasks, "completion without task")
            task = tasks[ref]
            require(task.terminal is None and bool(task.attempts), "duplicate/empty completion")
            last = attempts[task.attempts[-1]]
            require(last.finished and event.observer == task.buyer, "unbound task completion")
            success = last.provider_result["success"]
            require(success or len(task.attempts) == settings.attempts,
                    "premature task abandonment")
            require(task.paid == (last.provider.price_cents if success else 0),
                    "settlement missing or paid failed task")
            same(detail, {
                "success": success, "attempts": len(task.attempts),
                "latency": event.at - task.created, "price_cents": task.paid,
            }, "task completion")
            task.terminal = event
    require(set(tasks) == set(expected_tasks), "missing workload tasks")
    require(all(t.terminal is not None for t in tasks.values()), "unfinished tasks")
    require(not reservations and not any(backend_active.values()), "unfinished reservations")
    require(used_messages == set(sent), "extra/unconsumed messages")
    require(len(delivered) == len(sent), "orphan message delivery")
    return Analysis(world, policy, tasks, attempts, events)


@validator(EVALUATOR)
def evaluate(spec: ScenarioSpec, trace: Trace) -> list[StageResult]:
    complete = any(e.kind == "resilience.finished" for e in trace.events)
    if not complete:
        return [StageResult(
            name=EVALUATOR, status="not_enough_evidence",
            note="No experiment completion record; partial runs are not passes.",
        )]
    try:
        analysis = analyze(spec, trace.events, trace.run_id)
    except (InvalidEvidence, ValueError, KeyError, TypeError, IndexError) as exc:
        return [StageResult(
            name=EVALUATOR, status="failed",
            note=f"Evidence rejected: {type(exc).__name__}: {exc}",
        )]
    successful = sum(t.terminal.detail["success"] for t in analysis.tasks.values())
    return [StageResult(
        name=EVALUATOR, status="passed",
        evidence=[e.event_id for e in trace.events
                  if e.kind == "resilience.finished"],
        note=(f"Replayed {len(analysis.tasks)} tasks, {len(analysis.attempts)} attempts; "
              f"{successful} tasks succeeded. Pass means evidence is internally "
              "consistent, not that this policy is resilient or dependencies are truthful."),
    )]


def metrics(analysis: Analysis) -> dict[str, Any]:
    tasks = analysis.tasks
    success = [t for t in tasks.values() if t.terminal.detail["success"]]
    stress = [t for t in tasks.values() if 12.0 <= t.created < 28.0]
    rounds: dict[float, list[TaskEvidence]] = defaultdict(list)
    for task in tasks.values():
        rounds[task.created].append(task)
    timeline = [
        {"at": at, "completion": sum(t.terminal.detail["success"] for t in cohort) / len(cohort),
         "attempts_per_task": mean(len(t.attempts) for t in cohort)}
        for at, cohort in sorted(rounds.items())
    ]
    recovery = None
    last_end = analysis.world.last_outage_end
    if last_end is not None:
        after = [point for point in timeline if point["at"] >= last_end]
        for a, b in zip(after, after[1:]):
            if a["completion"] >= 0.9 and b["completion"] >= 0.9:
                recovery = a["at"] - last_end
                break
    pre_attempts = [a for a in analysis.attempts.values() if a.selected_at < 12.0]

    def hhi(values: list[str]) -> float:
        counts = Counter(values)
        return sum((n / len(values)) ** 2 for n in counts.values()) if values else 0.0

    latencies = sorted(t.terminal.detail["latency"] for t in success)
    repeated_dependency_retries = 0
    for task in tasks.values():
        seen = set()
        for ref in task.attempts:
            dependency = analysis.attempts[ref].provider.dependency
            repeated_dependency_retries += dependency in seen
            seen.add(dependency)
    return {
        "policy": analysis.policy,
        "condition": analysis.world.settings.condition,
        "disclosure": analysis.world.settings.disclosure,
        "actual_disclosure": sum(p.disclosed for p in analysis.world.providers) / 12,
        "seed": analysis.world.settings.seed,
        "tasks": len(tasks),
        "completed": len(success),
        "completion_rate": len(success) / len(tasks),
        "stress_completion_rate": (
            sum(t.terminal.detail["success"] for t in stress) / len(stress)
            if stress else None
        ),
        "attempts_per_task": len(analysis.attempts) / len(tasks),
        "repeated_dependency_retries": repeated_dependency_retries,
        "cost_per_success_cents": mean(t.paid for t in success) if success else None,
        "total_spend_cents": sum(t.paid for t in tasks.values()),
        "p95_success_latency": (
            latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None
        ),
        "recovery_after_outage": recovery,
        "agent_hhi_before_stress": hhi([a.provider.name for a in pre_attempts]),
        "dependency_hhi_before_stress": hhi([a.provider.dependency for a in pre_attempts]),
        "timeline": timeline,
    }
