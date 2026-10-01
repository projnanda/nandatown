from nandatown.bundle import load_bundle, verify_bundle
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.scenario import FaultRule, load_bundled
from nandatown.sim.validators import evaluate_scenario


def test_delayed_ballot_waits_for_one_verified_result(tmp_path):
    bundle_dir, result = run_lab("voting_finality", str(tmp_path))
    assert result.verdict == "passed"
    assert verify_bundle(bundle_dir) == []

    events = load_bundle(bundle_dir)["events"]
    delayed = next(e for e in events if e.kind == "message_delayed"
                   and e.detail["kind"] == "ballot")
    arrival = next(e for e in events if e.kind == "message_delivered"
                   and e.subject == delayed.subject)
    pending = [e for e in events if e.kind == "vote_pending"]
    final = [e for e in events if e.kind == "vote_result"
             and e.subject == "vote"]

    assert len(pending) == len(final) == 1
    assert pending[0].detail == {
        "received": 3, "expected": 4, "missing": ["voter-1"]}
    assert events.index(arrival) < events.index(final[0])
    assert final[0].detail == {
        "counts": {"apricot": 2, "plum": 2}, "total": 4,
        "complete": True, "missing": []}


def test_stock_box_is_a_verified_failing_control(tmp_path):
    protected = load_bundled("voting_finality").model_dump()
    control = load_bundled("voting_finality_unguarded").model_dump()
    for spec in (protected, control):
        spec.pop("name")
        spec.pop("description")
    control["agents"][0]["role"] = "deferred_ballot_box"
    assert control == protected

    bundle_dir, result = run_lab("voting_finality_unguarded", str(tmp_path))
    stages = {stage.name: stage.status for stage in result.stages}
    assert result.verdict == "failed"
    assert stages["bounded_finality"] == "failed"
    assert stages["tally_integrity"] == "failed"
    assert verify_bundle(bundle_dir) == []

    events = load_bundle(bundle_dir)["events"]
    final = next(e for e in events if e.kind == "vote_result"
                 and e.subject == "vote")
    late_cast = next(e for e in events if e.kind == "ballot_cast"
                     and e.subject == "voter-1")
    assert events.index(final) < events.index(late_cast)
    assert sum(final.detail["counts"].values()) != final.detail["total"]


def test_ballot_after_grace_is_recorded_without_changing_result():
    spec = load_bundled("voting_finality")
    spec.faults = [FaultRule(action="delay", kind="ballot", nth=1,
                            delay=6.0)]
    engine = build_engine(spec)
    engine.run()
    result = evaluate_scenario(spec, engine.run_id, engine.events)
    assert result.verdict == "passed"

    events = engine.events
    delayed = next(e for e in events if e.kind == "message_delayed"
                   and e.detail["kind"] == "ballot")
    arrival = next(e for e in events if e.kind == "message_delivered"
                   and e.subject == delayed.subject)
    final = next(e for e in events if e.kind == "vote_result"
                 and e.subject == "vote")
    rejected = next(e for e in events if e.kind == "ballot_rejected"
                    and e.detail["reason"] == "poll closed")

    assert final.at == 5.5
    assert final.detail == {
        "counts": {"apricot": 1, "plum": 2}, "total": 3,
        "complete": False, "missing": ["voter-1"]}
    assert events.index(final) < events.index(arrival) < events.index(rejected)
    assert rejected.subject == "voter-1"
    assert rejected.detail["message_id"] == delayed.subject
    assert rejected.detail["choice"] == "apricot"
