"""The Taste Twin example: replay runs, the duplicate-choice failure, live evidence.

Replay mode serves responses recorded from the live Taste Twin MCP server, so
these tests need no credentials or network. They check Town-side behaviour;
the committed live bundles are the compatibility evidence.
"""

import json
import os

import pytest

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files, run_lab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "examples", "tastetwin")
SCENARIO = os.path.join(EXAMPLE, "tastetwin_shared_taste.yaml")


def _stages(result):
    return {stage.name: stage.status for stage in result.stages}


@pytest.fixture
def replay(monkeypatch):
    monkeypatch.setenv("TASTETWIN_MODE", "replay")
    monkeypatch.delenv("TASTETWIN_FIXTURE", raising=False)
    monkeypatch.delenv("TASTETWIN_RECORD", raising=False)
    return monkeypatch


def test_replay_with_dedupe_passes_and_verifies(replay, tmp_path):
    replay.setenv("TASTETWIN_DEDUPE", "1")
    bundle, result = run_lab(SCENARIO, str(tmp_path))
    stages = _stages(result)
    assert stages["live_service"] == "not_tested"  # replay is never live evidence
    assert stages["server_auth"] == "not_tested"
    for name in ("discovery", "ranking_contract", "choice_binding",
                 "choice_applied_once", "shared_memory", "access_control", "privacy"):
        assert stages[name] == "passed", name
    assert verify_bundle(bundle) == []


def test_replay_without_dedupe_counts_the_choice_twice(replay, tmp_path):
    replay.setenv("TASTETWIN_DEDUPE", "0")
    bundle, result = run_lab(SCENARIO, str(tmp_path))
    stages = _stages(result)
    assert stages["choice_applied_once"] == "failed"
    assert stages["shared_memory"] == "passed"
    assert result.verdict == "failed"
    assert verify_bundle(bundle) == []


def test_an_agent_granted_by_mistake_is_caught(replay, tmp_path):
    """Granting the snooping agent must make access_control fail."""
    replay.setenv("TASTETWIN_DEDUPE", "1")
    with open(SCENARIO) as f:
        text = f.read()
    plugin = os.path.join(EXAMPLE, "tastetwin_plugin.py")
    text = text.replace("grants: [planner-a, planner-b]", "grants: [planner-a, planner-b, snoop]")
    text = text.replace("plugin_files: [tastetwin_plugin.py]", f"plugin_files: ['{plugin}']")
    broken = tmp_path / "granted_by_mistake.yaml"
    broken.write_text(text)
    _, result = run_lab(str(broken), str(tmp_path / "runs"))
    assert _stages(result)["access_control"] == "failed"


def test_private_words_never_reach_the_bundle(replay, tmp_path):
    replay.setenv("TASTETWIN_DEDUPE", "1")
    bundle, _ = run_lab(SCENARIO, str(tmp_path))
    for name in ("events.jsonl", "intents.jsonl", "profile.json"):
        with open(os.path.join(bundle, name)) as f:
            text = f.read()
        assert "too loud" not in text, name
        assert "loud cafes" not in text, name


@pytest.mark.parametrize("name, verdict", [("live-dedupe-on", "passed"),
                                           ("live-dedupe-off", "failed")])
def test_committed_live_bundles_verify(name, verdict):
    load_plugin_files([os.path.join(EXAMPLE, "tastetwin_plugin.py")])
    bundle = os.path.join(EXAMPLE, "evidence", name)
    assert verify_bundle(bundle) == []
    with open(os.path.join(bundle, "result.json")) as f:
        result = json.load(f)
    assert result["verdict"] == verdict
    stages = {s["name"]: s["status"] for s in result["stages"]}
    assert stages["live_service"] == "passed"
    assert stages["server_auth"] == "passed"
    assert stages["access_control"] == "passed"
