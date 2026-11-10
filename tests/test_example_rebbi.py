"""The Rebbi integration example (examples/rebbi).

The recorded bundles verify offline with the example's validators, carry
the verdicts the README reports, and name the Rebbi commit and the exact
adapter that produced them. The live check needs a Rebbi checkout, so it
runs only when REBBI_PROJECT points at one.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "rebbi"
EVIDENCE = EXAMPLE / "evidence"

BEFORE_FIX = "3db016d06ad1b0ef093a0a2ae7fe54703e060edd"
AFTER_FIX = "07da0565da42d74a224dcd2148c03eac0105d37a"

# bundle -> (Rebbi commit, verdict, stages that must fail)
EXPECTED = {
    "before-fix-lost-reply": (BEFORE_FIX, "failed", {
        "one_loan_per_request", "loans_settled", "retry_gets_original"}),
    "before-fix-duplicate": (BEFORE_FIX, "failed", {
        "one_loan_per_request", "loans_settled", "duplicate_gets_original"}),
    "after-fix-lend": (AFTER_FIX, "passed", set()),
    "after-fix-lost-reply": (AFTER_FIX, "passed", set()),
    "after-fix-duplicate": (AFTER_FIX, "passed", set()),
    "after-fix-no-room": (AFTER_FIX, "passed", set()),
}


def _digest(names: list[str]) -> str:
    h = hashlib.sha256()
    for name in names:
        data = (EXAMPLE / name).read_bytes().replace(b"\r\n", b"\n")
        h.update(name.encode() + b"\0" + data)
    return "sha256:" + h.hexdigest()


def _events(bundle: Path) -> list[dict]:
    with open(bundle / "events.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture(scope="module", autouse=True)
def plugin():
    load_plugin_files([str(EXAMPLE / "rebbi_plugin.py")])


def test_every_recorded_bundle_is_listed():
    recorded = {p.name for p in EVIDENCE.iterdir() if p.is_dir()}
    assert recorded == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_recorded_bundle_verifies(name):
    assert verify_bundle(str(EVIDENCE / name)) == []


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_recorded_verdict_and_failures(name):
    _, verdict, must_fail = EXPECTED[name]
    with open(EVIDENCE / name / "result.json", encoding="utf-8") as f:
        result = json.load(f)
    assert result["verdict"] == verdict
    failed = {s["name"] for s in result["stages"] if s["status"] == "failed"}
    assert failed == must_fail


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_bundle_names_the_rebbi_commit_and_this_adapter(name):
    commit, _, _ = EXPECTED[name]
    project = [e for e in _events(EVIDENCE / name)
               if e["kind"] == "rebbi_project"]
    assert len(project) == 1
    detail = project[0]["detail"]
    assert (detail["commit"], detail["dirty"]) == (commit, False)
    assert detail["adapter_digest"] == _digest(
        ["rebbi_plugin.py", "mcp_stdio.py", "rebbi_meter.py"])
    assert detail["fleet_digest"] == _digest(["fleet.toml"])


def test_evidence_holds_no_local_paths():
    for path in EVIDENCE.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            for marker in (":\\\\", "/home/", "/Users/", "/private/",
                           "/tmp/"):
                assert marker not in text, f"{marker!r} in {path.name}"


@pytest.mark.skipif(not os.environ.get("REBBI_PROJECT"),
                    reason="needs a Rebbi checkout in REBBI_PROJECT")
def test_live_cases_pass_against_rebbi(tmp_path):
    done = subprocess.run(
        [sys.executable, str(EXAMPLE / "run.py"), "--project",
         os.environ["REBBI_PROJECT"], "--case", "all", "--out",
         str(tmp_path)], cwd=ROOT, capture_output=True, text=True,
        timeout=600)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
