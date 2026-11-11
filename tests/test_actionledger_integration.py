"""ActionLedger integration (examples/actionledger).

The recorded evidence is checked offline on every run: each bundle must
verify, and each summary must agree with the bundles it describes, for the
deterministic seller (evidence/<case>/) and the Claude runs
(evidence/claude-v1/, claude-v2/). The live compatibility check starts an
ActionLedger checkout and is skipped unless ACTIONLEDGER_HOME points at one;
it uses the deterministic seller only, so it never spends model tokens.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from nandatown.bundle import verify_bundle

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "actionledger"
EVIDENCE = EXAMPLE / "evidence"
RECORDED = sorted(p.parent.name for p in EVIDENCE.glob("*/manifest.json"))


def _summary() -> dict:
    return json.loads((EVIDENCE / "summary.json").read_text(encoding="utf-8"))


def _case(name: str) -> dict:
    return next(c for c in _summary()["cases"] if c["case"] == name)


def _run_check():
    spec = importlib.util.spec_from_file_location(
        "actionledger_run_check", EXAMPLE / "run_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module here
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("case", RECORDED)
def test_recorded_bundle_verifies(case):
    assert verify_bundle(str(EVIDENCE / case)) == []


def test_summary_describes_the_recorded_bundles():
    summary = _summary()
    assert sorted(c["case"] for c in summary["cases"]) == RECORDED
    for entry in summary["cases"]:
        assert entry["passed"], entry["case"]
        assert all(c["ok"] for c in entry["checks"]), entry["case"]
        result = json.loads((EVIDENCE / entry["case"] / "result.json")
                            .read_text(encoding="utf-8"))
        assert result["run_id"] == entry["observed"]["run_id"]
        assert result["verdict"] == entry["observed"]["verdict"]


def test_recorded_control_passes_town_with_a_duplicate_hold():
    """The finding the integration exists for: Town's verdict cannot see
    an external write applied twice; only the tracker shows it."""
    direct, ledger = _case("amnesia_direct"), _case("amnesia")
    assert direct["observed"]["verdict"] == "passed"
    assert len(direct["observed"]["tracker_holds"]) == 2
    assert ledger["observed"]["verdict"] == "passed"
    assert len(ledger["observed"]["tracker_holds"]) == 1
    holds = {a["hold"] for a in ledger["observed"]["seller_applications"]}
    assert len(ledger["observed"]["seller_applications"]) == 2
    assert holds == {ledger["observed"]["tracker_holds"][0]["resource_id"]}


MODEL_RUNS = sorted(p.parent for p in EVIDENCE.glob("claude-*/*/manifest.json"))


@pytest.mark.parametrize("bundle", MODEL_RUNS,
                         ids=[f"{p.parent.name}/{p.name}" for p in MODEL_RUNS])
def test_recorded_model_bundle_verifies(bundle):
    assert verify_bundle(str(bundle)) == []


@pytest.mark.parametrize("harness", sorted(
    p.name for p in EVIDENCE.glob("claude-*") if p.is_dir()))
def test_model_summary_describes_its_bundles(harness):
    """The summary of the Claude runs matches the bundles beside it, and
    every run's transcript is there. Pass or fail is whatever was recorded:
    the v1 baseline holds failures on purpose."""
    root = EVIDENCE / harness
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    labels = sorted(c["label"] for c in summary["cases"])
    assert labels == sorted(p.parent.name
                            for p in root.glob("*/manifest.json"))
    for entry in summary["cases"]:
        result = json.loads((root / entry["label"] / "result.json")
                            .read_text(encoding="utf-8"))
        assert result["run_id"] == entry["observed"]["run_id"]
        assert result["verdict"] == entry["observed"]["verdict"]
        assert entry["passed"] == all(c["ok"] for c in entry["checks"])
        assert (root / f"{entry['label']}.transcript.jsonl").exists()


@pytest.mark.parametrize("harness", sorted(
    p.name for p in EVIDENCE.glob("claude-*") if p.is_dir()))
def test_model_runs_never_duplicate_a_hold_through_the_ledger(harness):
    """Whatever the model decided, a request whose holds went through
    ActionLedger has at most one hold in the tracker."""
    summary = json.loads((EVIDENCE / harness / "summary.json")
                         .read_text(encoding="utf-8"))
    for entry in summary["cases"]:
        if entry["seller_path"] == "actionledger":
            assert len(entry["observed"]["tracker_holds"]) <= 1, \
                entry["label"]


@pytest.mark.skipif(not os.environ.get("ACTIONLEDGER_HOME"),
                    reason="set ACTIONLEDGER_HOME to an ActionLedger checkout")
@pytest.mark.parametrize("name", [
    "clean", "town_lost_ack", "town_duplicate", "crash_restart", "amnesia",
    "amnesia_direct", "tracker_lost_ack", "amnesia_tracker_lost_ack",
    "lookup_down"])
def test_live_compatibility(name, tmp_path):
    check = _run_check()
    home = Path(os.environ["ACTIONLEDGER_HOME"]).resolve()
    venv = home / ".venv" / "bin" / "python"
    python = os.environ.get("ACTIONLEDGER_PYTHON") or (
        str(venv) if venv.exists() else None)
    if python is None:
        pytest.skip("no ActionLedger interpreter: set ACTIONLEDGER_PYTHON")
    case = next(c for c in check.CASES if c.name == name)
    observed, _ = check.run_case(case, home, python, tmp_path / name)
    failed = [c for c in check.checks(case, observed) if not c["ok"]]
    assert failed == []
