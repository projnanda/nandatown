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
    for name in ("discovery", "ranking_contract", "choice_binding",
                 "choice_applied_once", "shared_memory", "privacy"):
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


def test_private_words_never_reach_the_bundle(replay, tmp_path):
    replay.setenv("TASTETWIN_DEDUPE", "1")
    bundle, _ = run_lab(SCENARIO, str(tmp_path))
    for name in ("events.jsonl", "intents.jsonl", "profile.json"):
        with open(os.path.join(bundle, name)) as f:
            assert "too loud" not in f.read(), name


@pytest.mark.parametrize("name, verdict", [("live-dedupe-on", "passed"),
                                           ("live-dedupe-off", "failed")])
def test_committed_live_bundles_verify(name, verdict):
    load_plugin_files([os.path.join(EXAMPLE, "tastetwin_plugin.py")])
    bundle = os.path.join(EXAMPLE, "evidence", name)
    assert verify_bundle(bundle) == []
    with open(os.path.join(bundle, "result.json")) as f:
        result = json.load(f)
    assert result["verdict"] == verdict
    live = next(s for s in result["stages"] if s["name"] == "live_service")
    assert live["status"] == "passed"
