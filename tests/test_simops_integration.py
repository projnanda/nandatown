"""SimOps AI integration (examples/simops): recorded evidence verifies offline; a live check when SIMOPS_URL is set.

The offline test needs no network: it loads the plugin, verifies every recorded bundle with Town's own
verify_bundle, re-runs the cross-check against SimOps's saved log, and checks each case's recorded outcome
against what run_check.py expects of it. The live test plays the clean Week 9 case against the SimOps server
at SIMOPS_URL and skips when it is not set.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "simops")
EVIDENCE = os.path.join(HERE, "evidence")


def run_check():
    if "simops_run_check" not in sys.modules:
        spec = importlib.util.spec_from_file_location("simops_run_check", os.path.join(HERE, "run_check.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules["simops_run_check"] = module
        spec.loader.exec_module(module)
    return sys.modules["simops_run_check"]


@pytest.mark.skipif(not os.path.isdir(EVIDENCE), reason="no recorded SimOps evidence")
def test_recorded_simops_evidence_verifies_offline(capsys):
    assert run_check().verify_evidence(EVIDENCE) == 0, capsys.readouterr().out


@pytest.mark.skipif(not os.path.isdir(EVIDENCE), reason="no recorded SimOps evidence")
def test_recorded_cases_cover_a_failure_and_a_boundary():
    rc = run_check()
    kinds = {c["kind"] for c in rc.CASES if os.path.isdir(os.path.join(EVIDENCE, c["id"]))}
    assert {"baseline", "fault", "boundary", "control"} <= kinds


@pytest.mark.skipif(not os.environ.get("SIMOPS_URL"), reason="set SIMOPS_URL to a SimOps server to run live")
def test_clean_week9_live_against_simops(tmp_path, monkeypatch):
    rc = run_check()
    monkeypatch.chdir(rc.REPO)
    monkeypatch.setenv("NANDATOWN_HOME", str(tmp_path / "home"))
    case = next(c for c in rc.CASES if c["id"] == "clean")
    bundle, result, site = rc.play(case, str(tmp_path / "runs"))
    assert site is not None and site.ok, getattr(site, "error", None)
    check = rc.save(case, bundle, result, site, str(tmp_path / "evidence"), rc.versions_of([site]))
    failed = [r for r in check["rows"] if not r["ok"]]
    assert check["as_expected"], failed
    assert check["cross_check"]["ok"], check["cross_check"]["problems"]
    recorded = os.path.join(EVIDENCE, "clean", "check.json")
    if os.path.exists(recorded):
        import json

        with open(recorded, encoding="utf-8") as f:
            then = json.load(f)
        if then["versions"]["simops"] == check["versions"]["simops"]:
            # Same SimOps build, same scenario, same rule agents: SimOps logs the same calls with the same results.
            assert check["cross_check"]["log_digest"] == then["cross_check"]["log_digest"]
