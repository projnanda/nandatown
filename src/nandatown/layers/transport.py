"""Transport layer: moves envelopes between agents, injects faults."""

from __future__ import annotations

from typing import Any

from . import register


@register("transport", "memory.v1")
class MemoryTransport:
    """Deterministic in-memory delivery with declared drop, duplicate, delay,
    and partition faults."""

    LATENCY = 0.1

    def __init__(self, engine):
        self.engine = engine
        self.rules: list[dict[str, Any]] = []
        self.counts: dict[int, int] = {}
        self.partition: dict[str, Any] | None = None
        self._group: dict[str, int] = {}

    def configure(self, faults: list[dict[str, Any]]) -> None:
        self.rules = [dict(r) for r in faults]
        self.counts = {i: 0 for i in range(len(self.rules))}
        self.partition = next((r for r in self.rules
                               if r.get("action") == "partition"), None)
        if self.partition is not None:
            self._group = {name: index
                           for index, group in enumerate(self.partition["groups"])
                           for name in group}
            self._schedule_partition_events()

    def _schedule_partition_events(self) -> None:
        """Record when the cut starts and heals, as the town observes it."""
        engine, rule = self.engine, self.partition
        detail = {"groups": [list(group) for group in rule["groups"]],
                  "start": rule["start"], "heal": rule["heal"]}
        for kind, at in (("partition_started", rule["start"]),
                         ("partition_healed", rule["heal"])):
            def record(kind=kind):
                engine.emit("town", kind, "partition", dict(detail))
            engine.schedule(at - engine.now, record)

    def _partitioned(self, sender: str, to: str) -> bool:
        """True while the cut is active and separates sender from recipient.
        Judged when the message is sent."""
        rule = self.partition
        if rule is None or not (rule["start"] <= self.engine.now < rule["heal"]):
            return False
        return self._group[sender] != self._group[to]

    def _match(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        for i, rule in enumerate(self.rules):
            if rule.get("action") == "partition":
                continue
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
        if self._partitioned(sender, to):
            engine.emit("town", "message_dropped", envelope["message_id"],
                        {"from": sender, "to": to, "kind": envelope["kind"],
                         "fault": "partition"})
            return
        rule = self._match(envelope)
        latency = self.LATENCY

        def deliver():
            engine.emit("town", "message_delivered", envelope["message_id"],
                        {"to": to, "kind": envelope["kind"]})
            engine.deliver(to, envelope)

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
