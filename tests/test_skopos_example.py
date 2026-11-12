"""SKOPOS Golden Dome scenario: made-up data, no network, no keys."""
import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parents[1] / "examples/skopos"
sys.path.insert(0, str(HERE))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


scenario = _load("scenario")
run_demo = _load("run_demo")
P = scenario.Params()


def test_failed_satellite_watches_two_missiles_each_also_seen_elsewhere():
    watched = scenario.COVERAGE[scenario.FAILED_SATELLITE]
    assert len(watched) == 2
    for m in watched:
        others = [s for s, ms in scenario.COVERAGE.items()
                  if m in ms and s != scenario.FAILED_SATELLITE]
        assert others


def test_without_trust_check_trusting_everything_puts_two_at_risk():
    r = scenario.simulate("trust_all", P)
    assert sorted(r.missiles_at_risk) == ["M2", "M4"]
    assert not r.engagement_halted and r.blocked_satellites == []


def test_without_trust_check_stopping_everything_loses_the_fight():
    r = scenario.simulate("stop_all", P)
    assert r.engagement_halted and r.missiles_tracked_correctly == 0


def test_with_trust_check_bad_satellite_blocked_and_all_five_tracked():
    r = scenario.simulate("guardian", P)
    assert r.blocked_satellites == ["S3"]
    assert r.missiles_tracked_correctly == 5
    assert r.alert_seconds_after_failure == 0.0  # same 1-second update


def test_good_satellites_are_never_blocked_before_the_failure():
    early = scenario.Params(fail_tick=P.ticks)  # failure never happens
    r = scenario.simulate("guardian", early)
    assert r.blocked_satellites == [] and r.missiles_tracked_correctly == 5


def test_commander_must_approve():
    r = scenario.simulate("guardian", P, commander=lambda plan: False)
    assert r.plans_approved_by_commander == 0
    assert len(r.missiles_at_risk) == 5


def test_no_fire_command_exists():
    allowed = {"track_report", "trust_alert", "engagement_plan",
               "approval_request", "approved"}
    for mode in scenario.MODES:
        bus = scenario.MemoryBus()
        kinds = set()
        send = bus.send
        bus.send = lambda f, t, k, b: (kinds.add(k), send(f, t, k, b))
        assert scenario.simulate(mode, P, bus=bus).fire_commands == 0
        assert kinds <= allowed


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        scenario.simulate("fire_everything", P)


def test_random_trials_are_repeatable():
    a, b = scenario.run_trials(50), scenario.run_trials(50)
    assert a == b
    assert a["guardian"]["all_5_tracked_correctly_pct"] == 100.0
    assert a["stop_all"]["engagement_halted_pct"] == 100.0


def test_same_result_through_real_town_mailbox(tmp_path):
    bus = run_demo.TownBus(tmp_path)
    names = {p["name"] for p in bus.participants()}
    assert names == set(run_demo.AGENTS)
    town = scenario.simulate("guardian", P, bus=bus)
    memory = scenario.simulate("guardian", P)
    assert town.missiles_tracked_correctly == memory.missiles_tracked_correctly
    assert town.blocked_satellites == memory.blocked_satellites
    kinds = {e["kind"] for e in bus.events()}
    assert "participant_joined" in kinds
