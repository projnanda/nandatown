"""Transport layer: moves envelopes between agents, injects faults."""

from __future__ import annotations

from typing import Any

from . import register


@register("transport", "memory.v1")
class MemoryTransport:
    """Deterministic in-memory delivery with declared drop, duplicate, delay, and corrupt faults."""

    LATENCY = 0.1

    def __init__(self, engine):
        self.engine = engine
        self.rules: list[dict[str, Any]] = []
        self.counts: dict[int, int] = {}

    def configure(self, faults: list[dict[str, Any]]) -> None:
        self.rules = [dict(r) for r in faults]
        self.counts = {i: 0 for i in range(len(self.rules))}

    def _match(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        for i, rule in enumerate(self.rules):
            if rule.get("kind") and rule["kind"] != envelope["kind"]:
                continue
            if rule.get("action") == "drop_rate":
                if self.engine.rng.random() < rule.get("rate", 0.0):
                    return rule
                continue
            self.counts[i] += 1
            if self.counts[i] == rule.get("nth", 1):
                return rule
        return None

    def send(self, sender: str, to: str, envelope: dict[str, Any]) -> None:
        engine = self.engine
        engine.emit(sender, "message_sent", envelope["message_id"],
                    {"to": to, "kind": envelope["kind"],
                     "conversation": envelope.get("conversation"),
                     "body": envelope.get("body", {})})
        rule = self._match(envelope)
        latency = self.LATENCY

        def deliver(env=envelope):
            engine.emit("town", "message_delivered", env["message_id"],
                        {"to": to, "kind": env["kind"]})
            engine.deliver(to, env)

        if rule is None:
            engine.schedule(latency, deliver)
            return
        action = rule.get("action")
        if action == "drop_rate":
            engine.emit("town", "message_dropped", envelope["message_id"],
                        {"to": to, "kind": envelope["kind"],
                         "fault": "drop_rate", "rate": rule.get("rate")})
            return
        if action == "drop":
            engine.emit("town", "message_dropped", envelope["message_id"],
                        {"to": to, "kind": envelope["kind"], "fault": "drop"})
            return
        if action == "corrupt":
            field, value = rule["field"], rule.get("value", "")
            if envelope.get("body", {}).get(field) == value:
                engine.schedule(latency, deliver)
                return
            tampered = dict(envelope, body={**envelope.get("body", {}),
                                            field: value})
            engine.emit("town", "message_corrupted", envelope["message_id"],
                        {"to": to, "kind": envelope["kind"], "fault": "corrupt",
                         "field": field})
            engine.schedule(latency, lambda: deliver(tampered))
            return
        if action == "duplicate":
            engine.emit("town", "message_duplicated", envelope["message_id"],
                        {"to": to, "kind": envelope["kind"],
                         "fault": "duplicate"})
            engine.schedule(latency, deliver)
            engine.schedule(latency + self.LATENCY, deliver)
            return
        if action == "delay":
            extra = float(rule.get("delay", 1.0))
            engine.emit("town", "message_delayed", envelope["message_id"],
                        {"to": to, "kind": envelope["kind"], "fault": "delay",
                         "delay": extra})
            engine.schedule(latency + extra, deliver)
            return
        engine.schedule(latency, deliver)
