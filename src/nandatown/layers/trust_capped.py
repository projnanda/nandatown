"""Trust layer: reputation with a per-observer influence cap.

The formula, recomputed from the reports seen so far on every update:

    score(subject) = sum over distinct observers of
                     weight(observer) * clamp(net(observer, subject), -1, +1)

    net    = goods minus bads that observer reported about subject
    weight = 1 if the observer was party to a settled trade, else 0

One observer moves a score by at most 1 however many receipts it files,
an observer with no settled trade of its own moves nothing, and distinct
observers still accumulate. Judged by the reputation_capped validator,
not reputation_consistent, which replays the unbounded sum.
"""

from __future__ import annotations

from . import register


def clamp(value: int, low: int = -1, high: int = 1) -> int:
    return max(low, min(high, value))


@register("trust", "reputation.capped.v1")
class CappedReputation:
    """Reputation capped at +/-1 per observer, zero without trade history."""

    def __init__(self, engine):
        self.engine = engine
        # observer -> subject -> net goods minus bads, unclamped
        self.reports: dict[str, dict[str, int]] = {}

    def traded(self, observer: str) -> bool:
        """True if a settled payment recorded so far names the observer."""
        for event in self.engine.events:
            if event.kind != "payment_settled":
                continue
            detail = event.detail if isinstance(event.detail, dict) else {}
            if observer in (detail.get("from"), detail.get("to")):
                return True
        return False

    def weight(self, observer: str) -> int:
        return 1 if self.traded(observer) else 0

    def score(self, name: str) -> int:
        total = 0
        for observer, subjects in self.reports.items():
            if name in subjects:
                total += self.weight(observer) * clamp(subjects[name])
        return total

    def update(self, observer: str, subject: str, outcome: str,
               receipt_id: str) -> int:
        before = self.score(subject)
        net = self.reports.setdefault(observer, {})
        net[subject] = net.get(subject, 0) + (1 if outcome == "good" else -1)
        after = self.score(subject)

        weight = self.weight(observer)
        if weight == 0:
            reason = "observer_has_no_settled_trade"
        elif after == before:
            reason = "observer_cap_reached"
        else:
            reason = "counted"

        # A discarded report is still recorded, with delta 0 and a reason.
        self.engine.emit(observer, "reputation_updated", subject,
                         {"outcome": outcome, "delta": after - before,
                          "score": after, "receipt": receipt_id,
                          "formula": "reputation.capped.v1",
                          "observer_weight": weight,
                          "observer_net": clamp(net[subject]),
                          "reason": reason})
        return after
