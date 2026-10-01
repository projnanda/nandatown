"""Coordination layer: one-shot contract-net.

One task_id names one task lifecycle. The first announcement creates the
task; an identical re-announcement is a replay and changes nothing; a
re-announcement with different terms is rejected and changes nothing.
The task is finalized at most once: after it closes, award() is rejected
and returns None, so a repeated trigger yields no second actionable award.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import register
from ..records import canonical_json
from .coordination import ContractNet


@register("coordination", "contractnet.once.v1")
class OnceContractNet(ContractNet):
    """One task_id, one task lifecycle, finalized at most once."""

    def announce(self, issuer: str, task_id: str, spec: dict[str, Any],
                 rule: str = "lowest") -> None:
        task = self.tasks.get(task_id)
        if task is None:
            # The task and its task_announced event each keep their own
            # copy of the terms, apart from the caller and each other.
            super().announce(issuer, task_id, deepcopy(spec), rule)
            self.tasks[task_id]["spec"] = deepcopy(spec)
            return
        # Identity is Town's canonical JSON, where True, 1 and 1.0 differ.
        replay = (canonical_json([task["issuer"], task["spec"], task["rule"]])
                  == canonical_json([issuer, spec, rule]))
        terms = {"issuer": issuer, "spec": deepcopy(spec), "rule": rule}
        if replay:
            self.engine.emit("town", "announce_replayed", task_id,
                             {**terms, "state": task["state"]})
            return
        self.engine.emit("town", "announce_rejected", task_id,
                         {**terms,
                          "reason": "task_id reused with different terms"})

    def award(self, task_id: str) -> tuple[str, int] | None:
        task = self.tasks[task_id]
        if task["state"] == "closed":
            self.engine.emit("town", "award_rejected", task_id,
                             {"reason": "task closed"})
            return None
        return super().award(task_id)
