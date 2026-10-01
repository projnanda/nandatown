"""Privacy layer: redaction of declared fields from exported records.

The run still uses real values in memory; what leaves the run (the
bundle's profile and events) has the declared fields replaced. Public
reports exclude secrets by construction, not by promise.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import register

REDACTED = "[redacted]"


@register("privacy", "redact.v1")
class FieldRedaction:
    """Replaces declared field names with a marker in any nested record."""

    def __init__(self, engine):
        self.engine = engine
        self.fields: set[str] = set()

    def configure(self, fields: list[str]) -> None:
        self.fields = set(fields)

    def _scrub(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: (REDACTED if k in self.fields else self._scrub(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._scrub(v) for v in obj]
        return obj

    def redact(self, record: Any) -> Any:
        if not self.fields:
            return record
        return self._scrub(deepcopy(record))


@register("privacy", "redact.values.v1")
class ValueRedaction(FieldRedaction):
    """Also removes declared secret text values wherever they are copied."""

    MIN_SECRET_CHARS = 8

    def __init__(self, engine):
        super().__init__(engine)
        self.secrets: list[str] = []

    def configure(self, fields: list[str]) -> None:
        super().configure(fields)
        found = {
            value
            for agent in self.engine.spec.agents
            for key, value in agent.config.items()
            if key in self.fields and isinstance(value, str)
            and len(value) >= self.MIN_SECRET_CHARS
        }
        # Longest first, so a secret that contains another is removed whole.
        self.secrets = sorted(found, key=lambda s: (-len(s), s))

    def _scrub(self, obj: Any) -> Any:
        if isinstance(obj, str):
            for secret in self.secrets:
                obj = obj.replace(secret, REDACTED)
            return obj
        return super()._scrub(obj)
