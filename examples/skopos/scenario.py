"""SKOPOS: a made-up Golden Dome style missile-warning scenario.

Five missiles. Four watch satellites. One satellite is hacked partway
through and starts reporting false positions for the two missiles it
watches. Agents pass messages to each other: watch agents report, an
aim agent plans, a battle agent asks the commander to approve.

Three ways to run it:

  trust_all  No trust check. Every report is used.
  stop_all   No trust check. When reports disagree, stop everything.
  guardian   A trust check sits between the watch agents and the
             aim agent. It blocks reports from a satellite once that
             satellite sends a position no missile could reach.

The trust check here is a simple, textbook plausibility rule written
for this class demo. It shows WHERE a trust layer sits in the
workflow. It is not Guardian Veritas and does not show how Guardian
Veritas works.

Everything is synthetic: flat 2-D kilometres, straight-line flight,
Gaussian sensor noise. Nothing here models real sensors, real
weapons or real performance. No message can fire anything. The
"commander" is a scripted stand-in for a human approval click.

Standard library only.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Protocol

MISSILES = ("M1", "M2", "M3", "M4", "M5")
# Which satellite watches which missiles. S3 is the one that is hacked.
# Every missile S3 watches is also watched by one other satellite.
COVERAGE = {
    "S1": ("M1", "M2"),
    "S2": ("M3", "M4"),
    "S3": ("M2", "M4"),
    "S4": ("M5", "M1"),
}
FAILED_SATELLITE = "S3"
MODES = ("trust_all", "stop_all", "guardian")

TICK_SECONDS = 1.0
MISS_KM = 5.0            # aim error above this counts as "at risk"
CONFLICT_KM = 10.0       # stop_all halts when two reports differ by this
PLAUSIBLE_MARGIN_KM = 3.0  # trust check slack around the predicted spot


@dataclass(frozen=True)
class Params:
    ticks: int = 20
    fail_tick: int = 8
    spoof_km: float = 40.0
    spoof_angle: float = 1.0
    noise_km: float = 0.3
    speeds: tuple[float, ...] = (3.0, 2.5, 3.5, 2.8, 3.2)


def random_params(rng: random.Random) -> Params:
    return Params(
        ticks=20,
        fail_tick=rng.randint(4, 14),
        spoof_km=rng.uniform(15.0, 60.0),
        spoof_angle=rng.uniform(0, 2 * math.pi),
        noise_km=rng.uniform(0.2, 0.5),
        speeds=tuple(rng.uniform(2.0, 4.0) for _ in MISSILES),
    )


def truth(params: Params, missile: str, tick: int) -> tuple[float, float]:
    i = MISSILES.index(missile)
    speed = params.speeds[i]
    heading = 0.4 + 0.3 * i
    x0, y0 = 1000.0 + 50 * i, 200.0 - 30 * i
    t = tick * TICK_SECONDS
    return (x0 - speed * t * math.cos(heading),
            y0 - speed * t * math.sin(heading))


class Bus(Protocol):
    def send(self, frm: str, to: str, kind: str, body: dict) -> None: ...
    def drain(self, name: str) -> list[dict]: ...


class MemoryBus:
    """In-process mailbox with the same send / drain shape as the
    Nanda Town bus in run_demo.py."""

    def __init__(self) -> None:
        self.boxes: dict[str, list[dict]] = {}
        self.count = 0

    def send(self, frm: str, to: str, kind: str, body: dict) -> None:
        self.count += 1
        self.boxes.setdefault(to, []).append(
            {"from": frm, "kind": kind, "body": body})

    def drain(self, name: str) -> list[dict]:
        return self.boxes.pop(name, [])


def _r(v: float) -> float:
    return round(v, 3)


@dataclass
class Result:
    mode: str
    missiles_tracked_correctly: int = 0
    missiles_at_risk: list[str] = field(default_factory=list)
    engagement_halted: bool = False
    alert_seconds_after_failure: float | None = None
    blocked_satellites: list[str] = field(default_factory=list)
    plans_approved_by_commander: int = 0
    fire_commands: int = 0   # always 0: no such message exists
    messages: int = 0
    timeline: list[str] = field(default_factory=list)


def simulate(mode: str, params: Params, seed: int = 0,
             bus: Bus | None = None,
             commander: Callable[[dict], bool] = lambda plan: True
             ) -> Result:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    rng = random.Random(seed)
    bus = bus or MemoryBus()
    res = Result(mode=mode)
    log = res.timeline.append
    blocked: set[str] = set()
    last_good: dict[str, tuple[int, float, float]] = {}
    aim_points: dict[str, tuple[float, float]] = {}
    approved_plan: dict | None = None
    halted = False
    first = "guardian" if mode == "guardian" else "aim"

    for tick in range(params.ticks):
        hacked = tick >= params.fail_tick
        if tick == params.fail_tick:
            log(f"t={tick}s {FAILED_SATELLITE} starts sending false "
                f"positions for {', '.join(COVERAGE[FAILED_SATELLITE])}")

        # Watch agents report what they see.
        for sat, watched in COVERAGE.items():
            for m in watched:
                x, y = truth(params, m, tick)
                x += rng.gauss(0, params.noise_km)
                y += rng.gauss(0, params.noise_km)
                if sat == FAILED_SATELLITE and hacked:
                    x += params.spoof_km * math.cos(params.spoof_angle)
                    y += params.spoof_km * math.sin(params.spoof_angle)
                bus.send(sat, first, "track_report",
                         {"satellite": sat, "missile": m, "tick": tick,
                          "x_km": _r(x), "y_km": _r(y)})

        # Trust check (guardian mode only).
        if mode == "guardian":
            for msg in bus.drain("guardian"):
                rep = msg["body"]
                sat, m = rep["satellite"], rep["missile"]
                if sat in blocked:
                    continue
                if m in last_good:
                    t0, px, py = last_good[m]
                    i = MISSILES.index(m)
                    reach = params.speeds[i] * (rep["tick"] - t0) \
                        * TICK_SECONDS + PLAUSIBLE_MARGIN_KM
                    # Predicted spot is unknown in direction, so check
                    # distance against the most the missile could move.
                    if math.dist((px, py), (rep["x_km"], rep["y_km"])) \
                            > reach:
                        blocked.add(sat)
                        if res.alert_seconds_after_failure is None:
                            res.alert_seconds_after_failure = \
                                (tick - params.fail_tick) * TICK_SECONDS
                        bus.send("guardian", "commander", "trust_alert",
                                 {"satellite": sat, "tick": tick,
                                  "reason": "position no missile could "
                                            "reach", "action": "blocked"})
                        log(f"t={tick}s trust check blocks {sat}; "
                            f"commander alerted")
                        continue
                bus.send("guardian", "aim", "track_report", rep)

        # Aim agent fuses reports and plans.
        reports = bus.drain("aim")
        by_missile: dict[str, list[dict]] = {}
        for msg in reports:
            rep = msg["body"]
            if rep["satellite"] in blocked:
                continue
            by_missile.setdefault(rep["missile"], []).append(rep)
        for m, reps in by_missile.items():
            xs = [r["x_km"] for r in reps]
            ys = [r["y_km"] for r in reps]
            aim_points[m] = (sum(xs) / len(xs), sum(ys) / len(ys))
            last_good[m] = (tick, *aim_points[m])
            spread = max(math.dist((a["x_km"], a["y_km"]),
                                   (b["x_km"], b["y_km"]))
                         for a in reps for b in reps)
            if mode == "stop_all" and spread > CONFLICT_KM and not halted:
                halted = True
                log(f"t={tick}s reports for {m} disagree; nobody knows "
                    f"which is bad; all engagements stopped")
        plan = {m: {"interceptor": f"I{MISSILES.index(m) + 1}",
                    "aim_x_km": _r(p[0]), "aim_y_km": _r(p[1])}
                for m, p in sorted(aim_points.items())}
        bus.send("aim", "battle", "engagement_plan",
                 {"tick": tick, "plan": plan})

        # Battle agent asks the commander. Nothing proceeds without a yes.
        for msg in bus.drain("battle"):
            if halted:
                continue
            body = msg["body"]
            bus.send("battle", "commander", "approval_request", body)
        for msg in bus.drain("commander"):
            if msg["kind"] == "approval_request":
                if commander(msg["body"]):
                    approved_plan = msg["body"]
                    res.plans_approved_by_commander += 1
                    bus.send("commander", "battle", "approved",
                             {"tick": msg["body"]["tick"]})
        bus.drain("battle")

    res.engagement_halted = halted
    res.blocked_satellites = sorted(blocked)
    final = params.ticks - 1
    for m in MISSILES:
        if halted or approved_plan is None or m not in approved_plan["plan"]:
            res.missiles_at_risk.append(m)
            continue
        p = approved_plan["plan"][m]
        err = math.dist((p["aim_x_km"], p["aim_y_km"]),
                        truth(params, m, final))
        if err > MISS_KM:
            res.missiles_at_risk.append(m)
    res.missiles_tracked_correctly = len(MISSILES) - len(res.missiles_at_risk)
    res.messages = getattr(bus, "count", 0)
    log(f"end: {res.missiles_tracked_correctly} of 5 missiles tracked correctly "
        f"(aim within 5 km)" + (" (engagement stopped)" if halted else ""))
    return res


def run_trials(n: int = 1000, seed: int = 2026) -> dict:
    """Many random versions of the same attack, for each mode."""
    rng = random.Random(seed)
    trials = [(random_params(rng), rng.randrange(1 << 30)) for _ in range(n)]
    summary = {}
    for mode in MODES:
        results = [simulate(mode, p, s) for p, s in trials]
        alerts = [r.alert_seconds_after_failure for r in results
                  if r.alert_seconds_after_failure is not None]
        summary[mode] = {
            "trials": n,
            "all_5_tracked_correctly_pct": round(100 * sum(
                r.missiles_tracked_correctly == 5 for r in results) / n, 1),
            "mean_missiles_tracked_correctly": round(sum(
                r.missiles_tracked_correctly for r in results) / n, 2),
            "engagement_halted_pct": round(100 * sum(
                r.engagement_halted for r in results) / n, 1),
            "max_alert_seconds_after_failure":
                max(alerts) if alerts else None,
            "fire_commands": sum(r.fire_commands for r in results),
        }
    return summary
