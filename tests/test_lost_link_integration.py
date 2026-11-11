"""Offline integration evidence plus an opt-in external-service reproduction."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from nandatown.bundle import load_bundle
from nandatown.sim.runner import load_plugin_files
from nandatown.sim.validators import evaluate_scenario

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "lost_link"


def runner():
    spec = importlib.util.spec_from_file_location("lostlink_integration_runner", EXAMPLE / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_bundle():
    load_plugin_files([str(EXAMPLE / "adapter.py")])
    summary = json.loads((EXAMPLE / "evidence" / "summary.json").read_text())
    return load_bundle(str(EXAMPLE / "evidence" / summary["results"]["clean"]["bundle"]))


def test_recorded_lost_link_evidence_replays():
    runner().verify_saved(EXAMPLE / "evidence")


def test_return_requires_an_observed_human_approval():
    bundle = clean_bundle()
    events = [e for e in bundle["events"] if not
              (e.kind == "lostlink.http" and e.detail.get("operation") == "human_approve")]
    result = evaluate_scenario(bundle["profile"], bundle["run"].run_id, events)
    stage = next(s for s in result.stages if s.name == "human_approval_releases_pickup")
    assert stage.status == "failed"
    assert result.verdict == "failed"


def test_a_wrong_retrieved_item_is_not_a_success():
    bundle = clean_bundle()
    events = [e.model_copy(deep=True) for e in bundle["events"]]
    for event in events:
        if event.kind == "lostlink.http" and event.detail.get("operation") == "find_matches":
            event.detail["response"]["candidates"][0]["report_id"] = "unrelated-object"
    result = evaluate_scenario(bundle["profile"], bundle["run"].run_id, events)
    assert next(s for s in result.stages if s.name == "target_retrieved").status == "failed"


@pytest.mark.skipif(not os.environ.get("LOSTLINK_PROJECT_ROOT"),
                    reason="set LOSTLINK_PROJECT_ROOT to the pinned external Lost-Link checkout")
def test_lost_link_live_http_workflows(tmp_path):
    result = runner().run_suite(os.environ["LOSTLINK_PROJECT_ROOT"], tmp_path)
    assert [r["actual_verdict"] for r in result["results"].values()] == ["passed", "passed", "incomplete"]
