"""Memory layer: expiring memory, declared keys stop being served after a limit.

The limit is set per agent in the scenario: memory_ttl: {key: ticks}.
Events carry keys and times only, never values.
"""

from __future__ import annotations

from . import register
from .memory import KeyValueMemory


@register("memory", "ttl.v1")
class ExpiringMemory(KeyValueMemory):
    """Per-agent memory whose declared keys stop being returned after a limit."""

    enforce = True

    def __init__(self, engine):
        super().__init__(engine)
        self.expires_at: dict[tuple[str, str], float] = {}

    def _limit(self, agent, key):
        for a in self.engine.spec.agents:
            if a.name == agent:
                return a.config.get("memory_ttl", {}).get(key)
        return None

    def remember(self, agent, key, value):
        self.stores.setdefault(agent, {})[key] = value
        detail = {"key": key}
        limit = self._limit(agent, key)
        if limit is None:
            self.expires_at.pop((agent, key), None)
        else:
            deadline = self.engine.now + limit
            self.expires_at[(agent, key)] = deadline
            detail["expires_at"] = deadline
        self.engine.emit(agent, "memory_written", agent, detail)

    def recall(self, agent, key):
        deadline = self.expires_at.get((agent, key))
        if deadline is None:
            return super().recall(agent, key)
        stale = self.engine.now >= deadline
        if stale and self.enforce:
            self.stores.get(agent, {}).pop(key, None)
            del self.expires_at[(agent, key)]
            self.engine.emit(agent, "memory_expired", agent,
                             {"key": key, "expires_at": deadline})
            return None
        self.engine.emit(agent, "memory_recalled", agent,
                         {"key": key, "expires_at": deadline, "stale": stale})
        return self.stores.get(agent, {}).get(key)


@register("memory", "ttl_off.v1")
class ExpiringMemoryNotEnforced(ExpiringMemory):
    """Negative control: records the limit but still returns expired entries."""

    enforce = False
