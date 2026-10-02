"""The Lab engine: a seeded discrete event simulation.

Logical time, a single ordered queue, and layer plugins wired from the
scenario. Same scenario and seed always produce the same trace. No wall
clock and no unseeded randomness exist inside a run.
"""

from __future__ import annotations

import heapq
import random
import uuid
from typing import Any, Callable

from ..layers import LAYER_NAMES, resolve
from ..records import TownEvent


class Engine:
    def __init__(self, spec, run_id: str | None = None):
        self.spec = spec
        self.run_id = run_id or "sim-" + uuid.uuid4().hex[:12]
        self.rng = random.Random(spec.seed)
        self.now = 0.0
        self._seq = 0
        self._eseq = 0
        self._queue: list[tuple[float, int, Callable[[], None]]] = []
        self.events: list[TownEvent] = []
        self.intents: list[dict[str, Any]] = []
        self.agents: dict[str, Any] = {}
        self.layers = {name: resolve(name, spec.layers[name])(self)
                       for name in LAYER_NAMES}
        self.layers["transport"].configure(
            [f.model_dump() for f in spec.faults])
        self.layers["privacy"].configure(spec.redact_fields)

    # -- world plumbing -------------------------------------------------

    def emit(self, observer: str, kind: str, subject: str,
             detail: dict[str, Any] | None = None) -> str:
        self._eseq += 1
        event = TownEvent(event_id=f"ev-{self._eseq}", run_id=self.run_id,
                          at=self.now, observer=observer, kind=kind,
                          subject=subject, detail=detail or {})
        self.events.append(event)
        return event.event_id

    def record_intent(self, actor: str, action: str,
                      payload: dict[str, Any]) -> None:
        self.intents.append({
            "intent_id": f"in-{len(self.intents) + 1}",
            "run_id": self.run_id, "at": self.now, "actor": actor,
            "action": action, "payload": payload,
        })

    def schedule(self, delay: float, fn: Callable[[], None]) -> None:
        self._seq += 1
        heapq.heappush(self._queue, (self.now + max(0.0, delay),
                                     self._seq, fn))

    def deliver(self, to: str, envelope: dict[str, Any]) -> None:
        agent = self.agents.get(to)
        if agent is None:
            self.emit("town", "delivery_failed", envelope["message_id"],
                      {"to": to, "reason": "unknown recipient"})
            return 
        # NEW: Integrity check (before auth check)
        # Verify message body hasn't been corrupted in transit
        comms = self.layers["communication"]
        if hasattr(comms, "verify"):  # Only if communication layer supports it
            if not comms.verify(envelope):
                # Body doesn't match checksum - message corrupted
                self.emit("town", "integrity_failed", envelope["message_id"],
                          {"to": to, "reason": "body checksum mismatch"})
                return
        auth = self.layers["auth"]
        if not auth.verify(envelope["sender"], envelope["body"],
                           envelope.get("signature", ""),
                           subject=envelope["message_id"]):
            self.emit("town", "delivery_failed", envelope["message_id"],
                      {"to": to, "reason": "bad signature"})
            return
        agent.on_message(envelope)

    # -- run ------------------------------------------------------------

    def add_agent(self, agent) -> None:
        self.agents[agent.name] = agent

    def run(self) -> None:
        self.emit("town", "run_created", self.run_id,
                  {"scenario": self.spec.name, "seed": self.spec.seed})
        for agent in self.agents.values():
            self.emit("town", "participant_joined", agent.name,
                      {"role": agent.role})
            agent.on_start()
        while self._queue and self.now <= self.spec.max_time:
            at, _, fn = heapq.heappop(self._queue)
            self.now = at
            fn()
        self.emit("town", "run_finished", self.run_id,
                  {"logical_time": self.now})
