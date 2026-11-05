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
        if c == "flex_race_lost_response":   # the race's purpose is exercised, not skipped (QA 95e5d91 L4)
            st = {s["name"]: s["status"] for s in r["stages"]}
            for s in ("retry_replays_original", "retry_sends_identical_request", "acceptance_order_respected",
                      "duplicate_no_extra_commitment"):
                assert st[s] == "passed", (s, st[s])


@needs_evidence
def test_offline_reruns_differ_only_in_declared_binding_fields():
    """Recomputed from the committed events.jsonl files, not read from summary.json (QA 5199c77 L8)."""
    rc = importlib.import_module("run_check")
    base = EVIDENCE / "flex_race_lost_response" / "bundle" / "events.jsonl"
    reruns = sorted((EVIDENCE / "reruns").glob("flex_race_lost_response_rep*/bundle/events.jsonl"))
    assert len(reruns) >= 1
    for r in reruns:
        v = rc.compare(base, r)
        assert v["masked_identical"] and v["only_masked_fields_differ"], (r, v)
        assert set(f.split(".")[-1] for f in v["differing_fields"]) <= set(rc.MASKED_FIELDS)


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


# ---- validator unit tests on synthetic Town events (no Bona needed) -----------------------------------------
def _ev(i, kind, subject, detail, observer="desk"):
    from nandatown.records import TownEvent
    return TownEvent(event_id=f"ev-{i}", run_id="t", at=float(i), observer=observer, kind=kind, subject=subject,
                     detail=detail)


def _commit(i, r, attempt, hold, w, seq, sha, outcome="hold"):
    return _ev(i, "flex_commit", r, {"source": "x", "action": "hold", "outcome": outcome, "hold": hold,
                                     "w_granted": w, "seq": seq, "receipt_sha256": sha, "attempt": attempt,
                                     "request_sha256": "q1"})


def _trace(events):
    from nandatown.sim.validators import Trace
    return Trace(events, "t")


def test_offline_validator_retry_replay_catches_a_fresh_grant_on_retry():
    """QA 5199c77 M1: a desk that mints a NEW hold (or new seq, or new W) on a retry fails the stage, receipts
    or not; a true replay of a signed receipt passes; a replay without any receipt fails (rev3 4.3 wording)."""
    import bona_flex_validator as v
    replay = [_commit(1, "r-1", 1, "h-1", 400, 1, "s1"), _commit(2, "r-1", 2, "h-1", 400, 1, "s1")]
    fresh_hold = [_commit(1, "r-1", 1, "h-1", 400, 1, "s1"), _commit(2, "r-1", 2, "h-2", 400, 2, "s2")]
    fresh_unsigned = [_commit(1, "r-1", 1, "h-1", 400, None, None), _commit(2, "r-1", 2, "h-2", 400, None, None)]
    replay_unsigned = [_commit(1, "r-1", 1, "h-1", 400, None, None), _commit(2, "r-1", 2, "h-1", 400, None, None)]
    assert v.retry_replays_original(_trace(replay)).status == "passed"
    assert v.retry_replays_original(_trace(fresh_hold)).status == "failed"
    assert v.retry_replays_original(_trace(fresh_unsigned)).status == "failed"
    assert v.retry_replays_original(_trace(replay_unsigned)).status == "failed"


def test_offline_validator_retry_identity_is_judged_from_the_wire():
    """QA 5199c77 M2: the buyer's own message hashes decide; a resend with other bytes fails."""
    import bona_flex_validator as v

    def sent(i, sha):
        return _ev(i, "message_sent", "m", {"to": "desk", "kind": "flex_hold",
                                            "body": {"request": "r-1", "request_sha256": sha}}, observer="buyer-1")
    commits = [_commit(2, "r-1", 1, "h-1", 400, 1, "s1"), _commit(4, "r-1", 2, "h-1", 400, 1, "s1")]
    same = [sent(1, "q1"), commits[0], sent(3, "q1"), commits[1]]
    other = [sent(1, "q1"), commits[0], sent(3, "q2"), commits[1]]
    assert v.retry_sends_identical_request(_trace(same)).status == "passed"
    assert v.retry_sends_identical_request(_trace(other)).status == "failed"


@needs_evidence
def test_offline_missing_attestation_and_wrong_pins_are_detected(tmp_path):
    """QA 5199c77 M3: attestation.json is required; a case whose keys differ from the committed pins fails."""
    ce = _ce()
    case = tmp_path / "case"
    shutil.copytree(EVIDENCE / "flex_race_lost_response", case)
    pins = next(r for r in json.loads((EVIDENCE / "summary.json").read_text(encoding="utf-8"))["runs"]
                if r["dir"] == "flex_race_lost_response")
    assert ce.check_case(case, pins) == []
    assert ce.check_case(case, dict(pins, bona_did="did:key:zOTHER")) != []
    (case / "bundle" / "attestation.json").unlink()
    assert any("attestation" in p for p in ce.check_case(case, pins))
