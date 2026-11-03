# SPDX-License-Identifier: MIT
# Copyright (c) 2026 nanda-resilience contributors
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nandatown.sim.agents import SimAgent, role

from .model import world_from_spec
from .policies import DEPENDENCY_FAILURES, ResilienceCoordination


@role("resilience_clock")
class ExperimentClock(SimAgent):
    def on_start(self):
        world = world_from_spec(self.api._engine.spec)
        self.api.observe("resilience.started", self.name, {
            "settings": world.settings.model_dump(),
            "providers": [p.model_dump(mode="json") for p in world.providers],
        })
        self.api.later(world.end_time, lambda: self.api.observe(
            "resilience.finished", self.name, {}))


class OutageAgent(SimAgent):
    def schedule_outages(self):
        self.down = False
        for start, end in self.config["outages"]:
            self.api.later(start, lambda: self.set_down(True))
            self.api.later(end, lambda: self.set_down(False))

    def set_down(self, down: bool):
        self.down = down
        self.api.observe("resilience.availability", self.name, {"down": down})


@role("resilience_backend")
class Backend(OutageAgent):
    def on_start(self):
        self.schedule_outages()
        self.active = 0
        self.seen: set[str] = set()
        self.api.register(["resilience.compute"])

    def handle_compute(self, msg: dict[str, Any]):
        ref = msg["body"]["ref"]
        if ref in self.seen:
            raise ValueError(f"unexpected duplicate backend attempt {ref}")
        self.seen.add(ref)
        if self.down or self.active >= self.config["capacity"]:
            reason = "unavailable" if self.down else "capacity"
            self.finish(msg, False, reason)
            return
        self.active += 1
        self.api.observe("resilience.backend_admitted", ref, {
            "provider": msg["sender"], "active": self.active,
        })
        self.api.later(self.config["service_time"], lambda: self.complete(msg))

    def complete(self, msg):
        self.active -= 1
        self.finish(msg, not self.down, "unavailable" if self.down else "ok")

    def finish(self, msg, success: bool, reason: str):
        ref = msg["body"]["ref"]
        self.api.observe("resilience.backend_result", ref, {
            "provider": msg["sender"], "success": success, "reason": reason,
        })
        self.api.reply(msg, "compute_result", {
            "ref": ref, "success": success, "reason": reason,
        })


@role("resilience_provider")
class ProviderAgent(OutageAgent):
    def on_start(self):
        self.schedule_outages()
        self.pending: dict[str, dict[str, Any]] = {}
        self.seen: set[str] = set()
        facts = {"price_cents": self.config["price_cents"]}
        if self.config["disclosed"]:
            facts["dependency"] = self.config["dependency"]
        if not self.api.register(["resilience.service"], facts):
            raise ValueError(f"registration refused for {self.name}")

    def handle_service(self, msg):
        ref = msg["body"]["ref"]
        if ref in self.seen:
            raise ValueError(f"unexpected duplicate provider attempt {ref}")
        self.seen.add(ref)
        if self.down:
            self.respond(msg, False, "provider_unavailable")
            return
        self.pending[ref] = msg
        self.api.send(self.config["dependency"], "compute", {"ref": ref})

    def handle_compute_result(self, msg):
        if msg["sender"] != self.config["dependency"]:
            raise ValueError("compute result from unexpected backend")
        original = self.pending.pop(msg["body"]["ref"])
        if self.down:
            self.respond(original, False, "provider_unavailable")
        else:
            self.respond(original, msg["body"]["success"], msg["body"]["reason"])

    def respond(self, original, success: bool, reason: str):
        ref = original["body"]["ref"]
        self.api.observe("resilience.provider_result", ref, {
            "buyer": original["sender"], "success": success, "reason": reason,
        })
        self.api.reply(original, "service_result", {
            "ref": ref, "task": original["body"]["task"],
            "success": success, "reason": reason,
        })


@dataclass
class Task:
    round: int
    created: float
    tried: set[str] = field(default_factory=set)
    failed_groups: set[str] = field(default_factory=set)
    ref: str = ""
    provider: str = ""
    group: str = ""
    price: int = 0


@role("resilience_buyer")
class BuyerAgent(SimAgent):
    def on_start(self):
        self.tasks: dict[str, Task] = {}
        self.api.register(["resilience.buy"])
        coordinator = self.api._engine.layers["coordination"]
        if not isinstance(coordinator, ResilienceCoordination):
            raise ValueError("resilience buyers require a resilience coordination plugin")
        self.coordinator = coordinator
        for round_index in range(self.config["rounds"]):
            self.api.later(
                1.0 + round_index * self.config["interval"],
                lambda index=round_index: self.start_task(index),
            )

    def start_task(self, round_index: int):
        task_id = f"{self.name}/task-{round_index:03d}"
        self.tasks[task_id] = Task(round_index, self.api.now)
        self.api.observe("resilience.task_created", task_id, {"round": round_index})
        self.try_task(task_id)

    def try_task(self, task_id: str):
        task = self.tasks[task_id]
        cards = self.api.lookup("resilience.service")
        candidate, ref = self.coordinator.select(
            self.name, task_id, len(task.tried) + 1,
            cards, task.tried, task.failed_groups,
        )
        task.ref, task.provider = ref, candidate.name
        task.group, task.price = candidate.dependency or "unknown", candidate.price
        task.tried.add(candidate.name)
        self.api.send(candidate.name, "service", {"ref": ref, "task": task_id})

    def handle_service_result(self, msg):
        body = msg["body"]
        task_id = body["task"]
        task = self.tasks[task_id]
        if body["ref"] != task.ref or msg["sender"] != task.provider:
            raise ValueError("service result does not match the active attempt")
        success = body["success"]
        self.api.observe("resilience.attempt_finished", task.ref, {
            "task": task_id, "provider": task.provider, "success": success,
            "reason": body["reason"],
        })
        if success:
            self.api.pay(task.provider, task.price, memo=task_id)
            self.finish_task(task_id, True)
        elif len(task.tried) >= self.config["attempts"]:
            self.finish_task(task_id, False)
        else:
            if body["reason"] in DEPENDENCY_FAILURES:
                task.failed_groups.add(task.group)
            self.api.later(0.1, lambda: self.try_task(task_id))

    def finish_task(self, task_id: str, success: bool):
        task = self.tasks.pop(task_id)
        self.api.observe("resilience.task_finished", task_id, {
            "success": success, "attempts": len(task.tried),
            "latency": self.api.now - task.created,
            "price_cents": task.price if success else 0,
        })
