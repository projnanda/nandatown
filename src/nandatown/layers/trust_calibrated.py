"""Trust layer: reputation from stated confidence against rated outcomes.

An agent says how sure it is before an outcome, as an attributed event:

    api.observe("confidence_stated", decision_id,
                {"confidence": 0.9, "to": "buyer-1"})

A later rating of that agent by another settles its oldest open claim
addressed to the rater, else its oldest addressed to no one; "good" means
the claim held. Over the agent's settled (confidence, held) pairs, in ten
confidence bins:

    ece    = sum over bins of share * |mean confidence - share held|
    skill  = sqrt(max(0, 1 - brier / 0.25))
    weight = clamp((1 - shrunk ece) * skill, 0.1, 1)
    score  = round(100 * weight)

where the ece is first pulled toward 0.25 by thirty imaginary outcomes. An
agent with fewer than ten settled claims scores 10; so does one that is
sure of everything and right half the time. A rating of an agent with no
open claim moves nothing and says so. A second, different claim on one
decision is refused: the first stands.

Judged by the reputation_calibrated validator, not reputation_consistent,
which replays the unbounded sum.
"""

from __future__ import annotations

import math

from . import register

FORMULA = "reputation.calibrated.v1"
STATED_KIND = "confidence_stated"
REFUSED_KIND = "confidence_refused"
BINS = 10
WINDOW = 200
MIN_SETTLED = 10
ECE_PRIOR = 0.25
SHRINKAGE = 30.0
FLOOR = 0.1
OUTCOMES = {"good": 1, "bad": 0}


def bin_index(confidence: float) -> int:
    # One multiplication, so every implementation agrees on the edges.
    return min(int(confidence * BINS), BINS - 1)


def distribution(samples):
    """Per bin: [share of samples, mean confidence, share that held]."""
    count, said, held = [0.0] * BINS, [0.0] * BINS, [0.0] * BINS
    for confidence, outcome in samples:
        index = bin_index(confidence)
        count[index] += 1.0
        said[index] += confidence
        held[index] += outcome
    total = sum(count)
    return [[c / total, s / c, h / c] if c else [0.0, 0.0, 0.0]
            for c, s, h in zip(count, said, held)]


def weight_of(samples) -> float:
    """The weight of a record of (confidence, held) pairs."""
    samples = samples[-WINDOW:]
    if len(samples) < MIN_SETTLED:
        return FLOOR
    dist = distribution(samples)
    ece = round(sum(w * abs(c - a) for w, c, a in dist), 6)
    n = len(samples)
    shrunk = (n * ece + SHRINKAGE * ECE_PRIOR) / (n + SHRINKAGE)
    base = sum(w * a for w, _c, a in dist)
    brier = (sum(w * (c - a) ** 2 for w, c, a in dist)
             - sum(w * (a - base) ** 2 for w, _c, a in dist)
             + base * (1.0 - base))
    skill = math.sqrt(max(0.0, 1.0 - brier / 0.25))
    return max(FLOOR, min(1.0, max(FLOOR, 1.0 - shrunk) * skill))


def score_of(samples) -> int:
    return round(100 * weight_of(samples))


def claim_problem(observer, detail) -> str | None:
    """Why a stated confidence is not a claim, or None if it is one."""
    if not isinstance(detail, dict):
        return "a claim carries a confidence"
    confidence, to = detail.get("confidence"), detail.get("to")
    if (type(confidence) not in (int, float)
            or not 0.0 <= confidence <= 1.0):
        return "confidence must be a number from 0 to 1"
    if to is not None and (not isinstance(to, str) or not to):
        return "to must name an agent"
    if to == observer:
        return "a claim cannot be addressed to its own subject"
    return None


def pick_claim(open_claims, observer):
    """The claim a rating by `observer` settles: its own first, then the
    oldest addressed to no one."""
    for wanted in (observer, None):
        for claim in open_claims:
            if claim[2] == wanted:
                return claim
    return None


@register("trust", FORMULA)
class CalibratedReputation:
    """Reputation earned from stated confidence against rated outcomes."""

    subscribes_to = (STATED_KIND,)

    def __init__(self, engine):
        self.engine = engine
        # agent -> decision -> confidence, for every claim ever accepted
        self.claimed: dict[str, dict[str, float]] = {}
        # agent -> open claims, oldest first: (decision, confidence, to)
        self.open: dict[str, list[tuple]] = {}
        # agent -> settled (confidence, held) pairs, oldest first
        self.settled: dict[str, list[tuple[float, int]]] = {}

    def score(self, name: str) -> int:
        return score_of(self.settled.get(name, []))

    def on_event(self, event) -> None:
        if event.kind != STATED_KIND:
            return
        agent, decision = event.observer, event.subject
        problem = claim_problem(agent, event.detail)
        if problem is None:
            confidence = float(event.detail["confidence"])
            held = self.claimed.setdefault(agent, {})
            if decision not in held:
                held[decision] = confidence
                self.open.setdefault(agent, []).append(
                    (decision, confidence, event.detail.get("to")))
                return
            if held[decision] == confidence:
                return  # the same claim again is one claim
            problem = "a different claim is already on record for this decision"
        self.engine.emit(agent, REFUSED_KIND, decision,
                         {"reason": problem, "formula": FORMULA})

    def update(self, observer: str, subject: str, outcome: str,
               receipt_id: str) -> int:
        before = self.score(subject)
        detail = {"outcome": outcome, "receipt": receipt_id,
                  "formula": FORMULA}
        claim = pick_claim(self.open.get(subject, []), observer)
        if outcome not in OUTCOMES:
            detail["reason"] = "unknown_outcome"
        elif claim is None:
            detail["reason"] = "no_open_claim"
        else:
            self.open[subject].remove(claim)
            self.settled.setdefault(subject, []).append(
                (claim[1], OUTCOMES[outcome]))
            detail.update(reason="settled", decision=claim[0],
                          confidence=claim[1])
        after = self.score(subject)
        detail.update(delta=after - before, score=after)
        self.engine.emit(observer, "reputation_updated", subject, detail)
        return after
