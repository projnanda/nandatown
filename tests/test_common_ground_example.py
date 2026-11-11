"""The Common Ground integration in replay mode: no room, no model, no login.

Replay answers each room call and each Claude Code agent decision from
fixtures recorded in a live run, so these tests check the Town side (roles,
faults, the repair step, the validator) and the committed evidence, not live
compatibility.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os

import pytest

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files, run_lab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "examples", "integrations", "common-ground")


def _stages(result):
    return {s.name: s.status for s in result.stages}


@pytest.fixture(autouse=True)
def replay_mode(monkeypatch):
    monkeypatch.setenv("CG_MODE", "replay")
    monkeypatch.delenv("CG_FIXTURE", raising=False)


def test_clean_meetup_passes(tmp_path):
    _, result = run_lab(os.path.join(DIR, "cg_meetup.yaml"), str(tmp_path))
    stages = _stages(result)
    assert result.verdict == "passed"
    for name in ("room_interface", "discovery", "plan_created", "all_joined",
                 "posts_accepted", "same_plan_for_everyone",
                 "slot_in_every_window", "fairness_consistent", "privacy"):
        assert stages[name] == "passed", name
    assert stages["live_room"] == "not_tested"
    assert stages["live_agents"] == "not_tested"
    assert stages["fairest_option"] == "not_tested"


def test_duplicate_post_is_contained(tmp_path):
    _, result = run_lab(os.path.join(DIR, "cg_meetup_duplicate.yaml"),
                        str(tmp_path))
    stages = _stages(result)
    assert result.verdict == "passed"
    assert stages["duplicate_contained"] == "passed"
    assert stages["same_plan_for_everyone"] == "passed"


def test_dropout_fails_on_purpose(tmp_path):
    _, result = run_lab(os.path.join(DIR, "cg_meetup_dropout.yaml"),
                        str(tmp_path))
    stages = _stages(result)
    assert result.verdict == "failed"
    assert stages["dropout_reported"] == "passed"
    assert stages["same_plan_for_everyone"] == "not_enough_evidence"


def test_refused_post_is_repaired_by_the_agent(tmp_path):
    # In the recorded dropout run, Sergio's agent first posted overlapping
    # windows; the room refused them and the agent fixed them once.
    bundle, _ = run_lab(os.path.join(DIR, "cg_meetup_dropout.yaml"),
                        str(tmp_path))
    with open(os.path.join(bundle, "events.jsonl")) as f:
        kinds = [json.loads(line)["kind"] for line in f]
    assert "cg_agent_repair" in kinds
    assert kinds.count("cg_agent_decided") == 4  # three agents + one repair


def test_replay_refuses_a_changed_call(tmp_path, monkeypatch):
    # A fixture recorded for another scenario does not match these calls.
    monkeypatch.setenv("CG_FIXTURE",
                       os.path.join(DIR, "fixtures", "cg_meetup_dropout.json"))
    _, result = run_lab(os.path.join(DIR, "cg_meetup.yaml"), str(tmp_path))
    assert result.verdict != "passed"


@pytest.mark.parametrize("bundle", sorted(glob.glob(
    os.path.join(DIR, "evidence", "*", "sim-*"))))
def test_committed_evidence_verifies(bundle):
    load_plugin_files([os.path.join(DIR, "cg_plugin.py")])
    assert verify_bundle(bundle) == []


def test_runner_is_importable():
    spec = importlib.util.spec_from_file_location(
        "cg_run_check", os.path.join(DIR, "run_check.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.CASES) == {"clean", "duplicate", "dropout"}
