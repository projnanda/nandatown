"""VeriFlex (Bona) Lab pack: offline checks of the recorded evidence (always run) and the live run (skipped
unless BONA_HOME and BONA_PYTHON point at the Bona runtime).

offline_*: no `bona` or `bench` import (guarded). They re-verify Town's bundles (hashes, attestation, replay of the
recorded result with the pure validator), the hash-bound sidecars, every Bona receipt's Ed25519 signature, the
control's frozen failing set, and that tampering with an event, a sidecar file or a receipt is detected.
"""
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

PACK = pathlib.Path(__file__).resolve().parents[1] / "examples" / "bona_flex"
EVIDENCE = PACK / "evidence"
sys.path.insert(0, str(PACK))

LIVE = bool(os.environ.get("BONA_HOME") and os.environ.get("BONA_PYTHON"))
CASES = ("flex_clean", "flex_race_lost_response", "flex_race_naive_desk_control")
needs_evidence = pytest.mark.skipif(not (EVIDENCE / "summary.json").is_file(), reason="no recorded evidence")


def _ce():
    return importlib.import_module("check_evidence")


@needs_evidence
def test_offline_bundles_sidecars_and_binding_verify():
    ce = _ce()
    for c in CASES:
        assert ce.check_case(EVIDENCE / c) == [], c


@needs_evidence
def test_offline_import_guard_no_private_code():
    _ce()
    importlib.import_module("check_receipts")
    importlib.import_module("bona_flex_validator")
    assert not [m for m in sys.modules if m == "bona" or m.startswith(("bona.", "bench"))]


@needs_evidence
def test_offline_every_bona_receipt_verifies_and_mutations_fail():
    cr = importlib.import_module("check_receipts")
    for c in ("flex_clean", "flex_race_lost_response"):
        n, probs = cr.check(EVIDENCE / c)
        assert n > 0 and probs == [], (c, probs)


@needs_evidence
def test_offline_recorded_verdicts_and_frozen_control_set():
    exp = json.loads((PACK / "scenarios" / "expected_control_stages.json").read_text(encoding="utf-8"))
    for c in CASES:
        r = json.loads((EVIDENCE / c / "bundle" / "result.json").read_text(encoding="utf-8"))
        if c == exp["scenario"]:
            failed = sorted(s["name"] for s in r["stages"] if s["status"] == "failed")
            assert r["verdict"] == "failed" and failed == sorted(exp["expected_failed_stages"])
        else:
            assert r["verdict"] == "passed", (c, [(s["name"], s["status"]) for s in r["stages"]])


@needs_evidence
def test_offline_reruns_differ_only_in_declared_binding_fields():
    s = json.loads((EVIDENCE / "summary.json").read_text(encoding="utf-8"))
    assert s["reruns"], "no rerun recorded"
    for v in s["reruns"].values():
        assert v["masked_identical"] and v["only_masked_fields_differ"], v


@needs_evidence
@pytest.mark.parametrize("what", ["event", "sidecar_file", "receipt"])
def test_offline_tampering_is_detected(tmp_path, what):
    ce = _ce()
    case = tmp_path / "case"
    shutil.copytree(EVIDENCE / "flex_race_lost_response", case)
    if what == "event":
        p = case / "bundle" / "events.jsonl"
        b = p.read_bytes()
        p.write_bytes(b.replace(b'"w_granted": 100000', b'"w_granted": 100001', 1)
                      if b'"w_granted": 100000' in b else b.replace(b'"w_granted":100000', b'"w_granted":100001', 1))
    elif what == "sidecar_file":
        p = case / "bona_sidecar" / "clock_checks.json"
        p.write_bytes(p.read_bytes().replace(b"true", b"false", 1))
    else:
        p = sorted((case / "bona_sidecar" / "receipts").glob("*.json"))[0]
        r = json.loads(p.read_bytes())
        r["w"] = int(r.get("w") or 0) + 1
        p.write_bytes((json.dumps(r, indent=1, sort_keys=True) + "\n").encode())
    assert ce.check_case(case) != []


@pytest.mark.skipif(not LIVE, reason="live route: set BONA_HOME and BONA_PYTHON (private Bona runtime)")
def test_live_run_check_regenerates_the_results(tmp_path):
    p = subprocess.run([sys.executable, str(PACK / "run_check.py"), "--out", str(tmp_path / "run"), "--repeat", "2"],
                       capture_output=True, text=True, check=False, timeout=900)
    assert p.returncode == 0, p.stdout[-4000:] + p.stderr[-4000:]
    assert "RUN CHECK: OK" in p.stdout
