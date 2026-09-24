"""Transport layer: moves envelopes between agents, injects faults."""

from __future__ import annotations

from typing import Any

from . import register


@register("transport", "memory.v1")
class MemoryTransport:
    """Deterministic in-memory delivery with declared drop, duplicate, and delay faults."""

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


@register("transport", "partitioned.v1")
class PartitionedTransport(MemoryTransport):
    """Network partition: agents in different groups cannot communicate.

    Extends memory.v1 with partition group support. Messages crossing
    group boundaries are silently blocked and recorded as
    ``message_partition_blocked`` events. All existing fault types
    (drop, duplicate, delay, drop_rate) still apply within each
    partition group.

    Configuration::

        - action: partition
          groups:
            - [proposer, acceptor-1]
            - [acceptor-2, acceptor-3]

    Agents not listed in any group are placed in their own singleton
    group and cannot reach agents in any declared group.
    """

    def __init__(self, engine):
        super().__init__(engine)
        self._agent_group: dict[str, int] = {}

    def configure(self, faults: list[dict[str, Any]]) -> None:
        rules = []
        for fault in faults:
            if fault.get("action") == "partition":
                groups = fault.get("groups", [])
                for gi, group in enumerate(groups):
                    for agent in group:
                        self._agent_group[agent] = gi
            else:
                rules.append(dict(fault))
        self.rules = rules
        self.counts = {i: 0 for i in range(len(self.rules))}

    def _partitioned(self, sender: str, to: str) -> bool:
        """Return True if sender and to are in different groups."""
        if not self._agent_group:
            return False
        sg = self._agent_group.get(sender)
        tg = self._agent_group.get(to)
        if sg is None or tg is None:
            return True
        return sg != tg

    def send(self, sender: str, to: str, envelope: dict[str, Any]) -> None:
        engine = self.engine
        engine.emit(sender, "message_sent", envelope["message_id"],
                    {"to": to, "kind": envelope["kind"],
                     "conversation": envelope.get("conversation"),
                     "body": envelope.get("body", {})})

        if self._partitioned(sender, to):
            engine.emit("town", "message_partition_blocked",
                        envelope["message_id"],
                        {"to": to, "kind": envelope["kind"],
                         "from": sender, "fault": "partition"})
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
