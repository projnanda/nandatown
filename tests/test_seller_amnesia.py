"""crash_amnesia: the seller answers, loses its journal and dies before
acknowledging. Its restart applies the work again; only the town's
message identity keeps that from becoming a second response."""

from nandatown.bundle import verify_bundle
from nandatown.runner import run_town


def stage(result, name):
    return next(s for s in result.stages if s.name == name)


def test_forgotten_work_is_not_answered_twice(tmp_path):
    bundle_dir, result = run_town("quote-amnesia-restart", str(tmp_path))
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert stage(result, "amnesia_survived").status == "passed", detail
    assert result.verdict == "passed", detail
    assert verify_bundle(bundle_dir) == []


def test_fresh_response_ids_answer_twice(tmp_path):
    # Negative control: same fault, response identity minted per
    # application. It must fail, and fail on the second response.
    bundle_dir, result = run_town("quote-amnesia-fresh-ids", str(tmp_path))
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "failed", detail
    assert stage(result, "response").status == "failed", detail
    assert "2 distinct quote responses" in stage(result, "response").note
    assert stage(result, "amnesia_survived").status == "failed", detail
    assert verify_bundle(bundle_dir) == []
