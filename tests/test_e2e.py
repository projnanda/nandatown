"""End-to-end: real subprocesses, real HTTP, real faults."""

from nandatown.bundle import verify_bundle
from nandatown.runner import run_town


def stage(result, name):
    return next(s for s in result.stages if s.name == name)


def test_clean_run_end_to_end(tmp_path):
    bundle_dir, result = run_town("quote-clean", str(tmp_path))
    assert result.verdict == "passed", [
        (s.name, s.status, s.note) for s in result.stages]
    assert verify_bundle(bundle_dir) == []


def test_crash_restart_run_end_to_end(tmp_path):
    bundle_dir, result = run_town("quote-crash-restart", str(tmp_path))
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert stage(result, "recovered_after_restart").status == "passed", detail
    assert stage(result, "stale_fence_rejected").status == "passed", detail
    assert result.verdict == "passed", detail
    assert verify_bundle(bundle_dir) == []


def test_duplicate_delivery_run_end_to_end(tmp_path):
    bundle_dir, result = run_town("quote-duplicate-delivery", str(tmp_path))
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert stage(result, "duplicate_recognized").status == "passed", detail
    assert result.verdict == "passed", detail
    assert verify_bundle(bundle_dir) == []


def test_poison_request_run_end_to_end(tmp_path):
    bundle_dir, result = run_town("quote-poison-request", str(tmp_path))
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    assert verify_bundle(bundle_dir) == []


def test_poison_unbounded_control_fails_where_predicted(tmp_path):
    bundle_dir, result = run_town("quote-poison-unbounded", str(tmp_path),
                                  wait_timeout=25.0)
    assert result.verdict == "failed"
    assert [s.name for s in result.stages if s.status == "failed"] == [
        "custody_ended"]
    assert verify_bundle(bundle_dir) == []
