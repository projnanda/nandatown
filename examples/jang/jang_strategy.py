"""Negotiation strategies for the JANG adapter roles.

``decide`` is a line-by-line Python port of JANG's deterministic negotiator
(agents/rule-strategy.ts at JANGmarket 4fb4eca), together with the helpers it
depends on (utility.ts, mandate.ts checkTermsAgainstMandate, priorities.ts
rankOrderCentroid). JANG's REST API has no server-side negotiator, so an agent
that talks to JANG must bring its own; this one makes the same moves as
JANG's own agents, which ``parity/`` checks against the TypeScript original.

``ClaudeStrategy`` asks Claude, through the ``claude`` CLI, for the next move.
JANG still enforces the mandate on whatever it proposes.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

Terms = dict[str, float]
MESO_BAND = 0.03


# -- JS-compatible number helpers ---------------------------------------


def js_round(x: float) -> int:
    """JavaScript Math.round: halves go toward +infinity."""
    return math.floor(x + 0.5)


def to_fixed6(x: float) -> float:
    return float(f"{x:.6f}")


def norm(x: float) -> float | int:
    """Integers stay integers, so terms serialise the way JANG writes them."""
    return int(x) if float(x).is_integer() else x


def norm_terms(terms: Terms) -> dict[str, float | int]:
    return {k: norm(v) for k, v in terms.items()}


# -- utility.ts / mandate.ts / priorities.ts ----------------------------


def dimension_score(value: float, ideal: float, worst: float) -> float:
    if ideal == worst:
        return 1.0
    n = (value - worst) / (ideal - worst)
    return 0.0 if n < 0 else 1.0 if n > 1 else n


def utility(terms: Terms, mandate: dict[str, Any]) -> float:
    weighted = 0.0
    total = 0.0
    for key, pref in mandate["preferences"].items():
        if key not in terms:
            continue
        weighted += pref["weight"] * dimension_score(terms[key], pref["ideal"], pref["worst"])
        total += pref["weight"]
    return 0.0 if total == 0 else weighted / total


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def check_terms(terms: Terms, mandate: dict[str, Any],
                now: datetime | None = None) -> dict[str, Any]:
    """checkTermsAgainstMandate: hard bounds, reservation utility, expiry."""
    now = now or datetime.now(timezone.utc)
    violations: list[dict[str, Any]] = []
    if _parse_iso(mandate["expiresAt"]) <= now:
        violations.append({"dimension": "*", "reason": "mandate expired"})
    for key, c in mandate.get("constraints", {}).items():
        if key not in terms:
            violations.append({"dimension": key, "reason": "term missing from offer"})
            continue
        if c.get("max") is not None and terms[key] > c["max"]:
            violations.append({"dimension": key, "reason": "above the maximum allowed"})
        if c.get("min") is not None and terms[key] < c["min"]:
            violations.append({"dimension": key, "reason": "below the minimum allowed"})
    if utility(terms, mandate) < mandate["reservationUtility"]:
        violations.append({"dimension": "*",
                           "reason": "package is worth less than the owner's reservation value"})
    return {"ok": not violations, "violations": violations}


def rank_order_centroid(order: list[str]) -> dict[str, float]:
    n = len(order)
    out = {}
    for i, key in enumerate(order):
        w = 0.0
        for k in range(i + 1, n + 1):
            w += 1 / k
        out[key] = w / n
    return out


def weights_from_order(order: list[str] | None, dimensions: list[dict]) -> dict[str, float] | None:
    keys = {d["key"] for d in dimensions}
    ranked = [k for k in (order or []) if k in keys]
    return rank_order_centroid(ranked) if ranked else None


def packages_of(offer: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"offerId": offer["offerId"], "terms": offer["terms"]},
            *(offer.get("alternatives") or [])]


# -- rule-strategy.ts ----------------------------------------------------


def _candidates(dim: dict, mandate: dict, grid: int, anchor: float | None = None) -> list[float]:
    c = mandate.get("constraints", {}).get(dim["key"], {})
    step = dim.get("step", 1)
    low = max(dim["min"], c["min"] if c.get("min") is not None else dim["min"])
    high = min(dim["max"], c["max"] if c.get("max") is not None else dim["max"])
    if high < low:
        return []

    def snap(v: float) -> float:
        stepped = js_round((v - low) / step) * step + low
        return min(high, max(low, to_fixed6(stepped)))

    values = {snap(low), snap(high)}
    for i in range(1, grid - 1):
        values.add(snap(low + ((high - low) * i) / (grid - 1)))
    pref = mandate["preferences"].get(dim["key"])
    if pref:
        values.add(snap(pref["ideal"]))
        values.add(snap(pref["worst"]))
    if anchor is not None and low <= anchor <= high:
        values.add(snap(anchor))
    return sorted(values)


def _best_possible(view: dict, grid: int) -> float:
    mandate = view["yourMandate"]
    ideal: Terms = {}
    for dim in view["itemSpec"]["dimensions"]:
        options = _candidates(dim, mandate, grid)
        best = options[0] if options else None
        best_score = -math.inf
        for value in options:
            score = utility({**ideal, dim["key"]: value}, mandate)
            if score > best_score:
                best_score, best = score, value
        if best is not None:
            ideal[dim["key"]] = best
    return utility(ideal, mandate)


def _distance(terms: Terms, ask: Terms | None, dims: list[dict],
              weights: dict[str, float] | None = None) -> float:
    if not ask:
        return 0.0
    total = 0.0
    for d in dims:
        span = (d["max"] - d["min"]) or 1
        gap = abs(terms.get(d["key"], 0) - ask.get(d["key"], 0)) / span
        total += gap * weights.get(d["key"], 0) if weights else gap
    return total if weights else total / len(dims)


def _best_package_for_me(packages: list[dict], mandate: dict, respect: bool) -> dict:
    eligible = [p for p in packages if check_terms(p["terms"], mandate)["ok"]] if respect else packages
    if not eligible:
        return packages[0]
    best = eligible[0]
    for p in eligible[1:]:
        if utility(p["terms"], mandate) > utility(best["terms"], mandate):
            best = p
    return best


def _best_counter(view: dict, target: float, grid: int, ask: Terms | None,
                  collect: bool, weights: dict[str, float] | None):
    mandate = view["yourMandate"]
    dims = view["itemSpec"]["dimensions"]
    axes = [(d, _candidates(d, mandate, grid, (ask or {}).get(d["key"]))) for d in dims]
    if any(not values for _, values in axes):
        return None
    best: dict | None = None
    pool: list[dict] = []
    current: Terms = {}

    def walk(i: int) -> None:
        nonlocal best
        if i == len(axes):
            score = utility(current, mandate)
            if score < target:
                return
            if collect:
                pool.append({"terms": dict(current), "score": score})
            distance = _distance(current, ask, dims, weights)
            if (best is None or distance < best["distance"] - 1e-9
                    or (abs(distance - best["distance"]) < 1e-9 and score > best["score"])):
                best = {"terms": dict(current), "distance": distance, "score": score}
            return
        dim, values = axes[i]
        for v in values:
            current[dim["key"]] = v
            walk(i + 1)
        current.pop(dim["key"], None)

    walk(0)
    return None if best is None else {"terms": best["terms"], "score": best["score"], "pool": pool}


def _best_counters(view: dict, target: float, grid: int, ask: Terms | None, count: int) -> list[Terms]:
    dims = view["itemSpec"]["dimensions"]
    primary = _best_counter(view, target, grid, ask, count > 1,
                            weights_from_order(view.get("counterpartyPriorities"), dims))
    if not primary:
        return []
    if count <= 1:
        return [primary["terms"]]
    mandate = view["yourMandate"]
    ceiling = max(target, primary["score"]) + MESO_BAND
    pool = [p for p in primary["pool"] if p["score"] <= ceiling]
    chosen = [primary["terms"]]
    while len(chosen) < count:
        nxt = None
        for p in pool:
            spread = min(_distance(p["terms"], c, dims) for c in chosen)
            if spread <= 1e-9:
                continue
            if nxt is None or spread > nxt["spread"] + 1e-9:
                nxt = {"terms": p["terms"], "spread": spread}
        if nxt is None:
            break
        chosen.append(nxt["terms"])
    return [t for t in chosen if check_terms(t, mandate)["ok"]]


def _note_for(view: dict, terms: Terms, ask: Terms | None) -> str:
    if not ask:
        return "Opening proposal. Tell me which of these terms matters most to you and I will trade against it."
    dims = view["itemSpec"]["dimensions"]
    label = {d["key"]: d["label"].lower() for d in dims}
    mine = next((o for o in reversed(view["offers"]) if o["fromAgentId"] == view["you"]["agentId"]), None)
    conceded = []
    if mine:
        for d in dims:
            k = d["key"]
            before, now_, theirs = mine["terms"].get(k), terms.get(k), ask.get(k)
            if before != now_ and abs(now_ - theirs) < abs(before - theirs):
                conceded.append(label[k])
    apart = [label[d["key"]] for d in dims if terms.get(d["key"]) != ask.get(d["key"])]
    if not apart:
        return "That works for me on every term."
    opening = (f"I have moved toward you on {' and '.join(conceded)}." if conceded
               else "I cannot take that package as it stands.")
    return f"{opening} We are still apart on {' and '.join(apart)}."


def decide(view: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Port of rule-strategy.ts decide(). Returns accept / offer / wait / stop."""
    options = options or {}
    toughness = options.get("toughness", 2.2)
    grid = options.get("grid", 9)
    respect = bool(options.get("respectBounds"))
    mandate = view["yourMandate"]

    if view["status"] != "open":
        return {"action": "stop", "reason": f"session is {view['status']}"}
    standing = view.get("standingOffer")
    if standing and standing["fromAgentId"] == view["you"]["agentId"]:
        return {"action": "wait"}
    theirs = _best_package_for_me(packages_of(standing), mandate, respect) if standing else None
    multi = standing is not None and len(packages_of(standing)) > 1
    pick = {"offerId": theirs["offerId"]} if theirs and multi else {}

    ceiling = _best_possible(view, grid)
    floor = mandate["reservationUtility"]
    elapsed = min(1, view["roundsUsed"] / max(1, view["maxRounds"] - 1))
    target = max(floor, ceiling - (ceiling - floor) * math.pow(elapsed, toughness))
    if options.get("deadlineAware") and view["roundsUsed"] >= view["maxRounds"] - 2:
        target = floor

    acceptable = theirs is None or not respect or check_terms(theirs["terms"], mandate)["ok"]
    on_table = utility(theirs["terms"], mandate) if theirs and acceptable else -math.inf
    if theirs and on_table >= target:
        return {"action": "accept", **pick,
                "reason": f"their package is worth {on_table:.3f} to me, at or above my target {target:.3f}"}

    counters = _best_counters(view, target, grid, theirs["terms"] if theirs else None,
                              3 if options.get("meso") else 1)
    if not counters:
        if theirs and on_table >= floor:
            return {"action": "accept", **pick,
                    "reason": f"nothing better is reachable and {on_table:.3f} still clears my reservation"}
        return {"action": "stop", "reason": "no package inside my mandate is reachable"}
    counter = counters[0]
    if theirs and on_table >= utility(counter, mandate):
        return {"action": "accept", **pick,
                "reason": "their package beats my own next move, so there is nothing to gain by countering"}
    alternatives = counters[1:]
    out: dict[str, Any] = {"action": "offer", "terms": norm_terms(counter), "targetUtility": target}
    if alternatives:
        out["alternatives"] = [norm_terms(t) for t in alternatives]
        lead = "I cannot take that package as it stands." if theirs else "Opening proposal."
        out["note"] = (f"{lead} Here are {len(alternatives) + 1} packages that work equally well "
                       "for me; pick whichever suits you best, or counter from the one closest to what you need.")
    else:
        out["note"] = _note_for(view, counter, theirs["terms"] if theirs else None)
    return out


class RuleStrategy:
    name = "rule"

    def __init__(self, options: dict[str, Any] | None = None):
        self.options = options or {}

    def decide(self, view: dict[str, Any]) -> dict[str, Any]:
        return decide(view, self.options)


# -- Claude ---------------------------------------------------------------

_PROMPT = """You are a negotiating agent acting for one owner inside JANG, a
multi-term negotiation service. Your owner's signed mandate (private to you)
and the session as JANG shows it to you are below. JANG will refuse any package
outside your mandate. Decide your next move.

Reply with ONLY one JSON object, no prose, in one of these shapes:
{{"action":"accept","offerId":"<id of the standing package you accept>"}}
{{"action":"offer","terms":{{...every dimension key: number on its step...}},"note":"<one sentence, never reveal your limits>"}}
{{"action":"stop","reason":"<why>"}}

Session view (JSON):
{view}
"""


class ClaudeStrategy:
    """Ask Claude for the move through the `claude` CLI (uses its own login)."""

    name = "claude"

    def __init__(self, model: str | None = None, timeout: float = 120.0,
                 fallback: RuleStrategy | None = None):
        self.model = model or os.environ.get("JANG_CLAUDE_MODEL", "")
        self.timeout = timeout
        self.fallback = fallback
        self.last_raw = ""

    def decide(self, view: dict[str, Any]) -> dict[str, Any]:
        standing = view.get("standingOffer")
        if view["status"] != "open":
            return {"action": "stop", "reason": f"session is {view['status']}"}
        if standing and standing["fromAgentId"] == view["you"]["agentId"]:
            return {"action": "wait"}
        cmd = ["claude", "-p", "--output-format", "text"]
        if self.model:
            cmd += ["--model", self.model]
        prompt = _PROMPT.format(view=json.dumps(view, indent=1))
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                             timeout=self.timeout, check=True)
        self.last_raw = out.stdout
        match = re.search(r"\{.*\}", out.stdout, re.S)
        if not match:
            raise ValueError("claude returned no JSON move")
        move = json.loads(match.group(0))
        if move.get("action") == "accept" and standing and not move.get("offerId"):
            move["offerId"] = standing["offerId"]
        if move.get("action") == "offer":
            move["terms"] = norm_terms({k: float(v) for k, v in move["terms"].items()})
        move.setdefault("note", "")
        return move
