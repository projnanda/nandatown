"""Step through a run's evidence, event by event."""

from __future__ import annotations

import json
from typing import Any


def render_replay(bundle: dict[str, Any], start: int = 0,
                  limit: int | None = None,
                  kind: str | None = None) -> str:
    from .report import shown_without_recorded_credentials

    bundle = shown_without_recorded_credentials(bundle)
    events = bundle["events"]
    if kind:
        events = [e for e in events if e.kind == kind]
    events = events[start:]
    if limit is not None:
        events = events[:limit]
    lines = [f"Replay of {bundle['run'].run_id}"
             f" ({bundle['profile'].name}, {len(bundle['events'])} events)"]
    for e in events:
        detail = json.dumps(e.detail, sort_keys=True) if e.detail else ""
        lines.append(f"t={e.at:8.2f}  {e.event_id:>7}  [{e.observer}]"
                     f" {e.kind} {e.subject} {detail}")
    lines.append("")
    lines.append(f"Verdict: {bundle['result'].verdict.upper()}")
    return "\n".join(lines) + "\n"
