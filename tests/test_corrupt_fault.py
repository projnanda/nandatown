"""The corrupt transport fault: a signed body changed in transit.

hmac.v1 must reject the changed message and the proposer must recover;
with plain.v1 the change is accepted and consensus splits.
"""

import hashlib
import json
import os

import pytest
from pydantic import ValidationError

from nandatown.bundle import verify_bundle
from nandatown.sim.engine import Engine
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.scenario import FaultRule, load_bundled, load_scenario_text
from nandatown.sim.validators import evaluate_scenario

# SHA-256 of the consensus events.jsonl on main (36e7f6c), with the random
# run id replaced by "RUN". Pins the existing scenario byte for byte.
CONSENSUS_EVENTS_ON_MAIN = (
    "d96f6bfc9e72b0c6e9b6ab5fba3886e6be33235813e8ff43386b6055c201cfc5")


def _events_digest(bundle: str) -> str:
    run_id = os.path.basename(bundle)
    with open(os.path.join(bundle, "events.jsonl")) as f:
        text = f.read().replace(run_id, "RUN")
    return hashlib.sha256(text.encode()).hexdigest()


def test_corrupt_fault_declares_the_field_it_rewrites():
    rule = FaultRule.model_validate(
        {"action": "corrupt", "kind": "commit", "nth": 2,
         "field": "value", "value": "v666"})

    assert rule.action == "corrupt"
    assert rule.field == "value" and rule.value == "v666"


def test_corrupt_fault_without_a_field_is_refused():
    with pytest.raises(ValidationError):
        FaultRule.model_validate({"action": "corrupt", "kind": "commit"})


def _empty_engine():
    spec = load_scenario_text("name: empty\nagents: []\n")
    return Engine(spec)


def test_corrupt_rewrites_a_copy_and_names_the_field():
    engine = _empty_engine()
    transport = engine.layers["transport"]
    transport.configure([{"action": "corrupt", "kind": "commit", "nth": 1,
                          "field": "value", "value": "v666"}])
    envelope = {"message_id": "m-1", "conversation": "c-1", "sender": "p",
               "to": "a", "kind": "commit", "body": {"value": "v42"},
               "signature": "sig"}
    original_body = envelope["body"]

    transport.send("p", "a", envelope)

    assert envelope["body"] is original_body
    assert original_body["value"] == "v42"
    sent = [e for e in engine.events if e.kind == "message_sent"][0]
    assert sent.detail["body"]["value"] == "v42"
    corrupted = [e for e in engine.events if e.kind == "message_corrupted"][0]
    assert corrupted.detail["field"] == "value"
    assert corrupted.subject == "m-1"


def test_consensus_events_are_unchanged(tmp_path):
    bundle, _ = run_lab("consensus", str(tmp_path))

    assert _events_digest(bundle) == CONSENSUS_EVENTS_ON_MAIN


def test_consensus_flag_off_emits_no_commit_confirmation(tmp_path):
    bundle, _ = run_lab("consensus", str(tmp_path))
    with open(os.path.join(bundle, "events.jsonl")) as f:
        kinds = [json.loads(line)["kind"] for line in f]

    assert "commit_ack" not in kinds
    assert "commit_retry" not in kinds


def test_consensus_corrupt_recovers_under_hmac(tmp_path):
    bundle, result = run_lab("consensus_corrupt", str(tmp_path))
    stages = {s.name: s.status for s in result.stages}

    assert result.verdict == "passed", stages
    assert stages == {
        "quorum_commit": "passed", "agreement": "passed",
        "corruption_injected": "passed", "corruption_rejected": "passed",
        "corruption_recovered": "passed", "ledger_conserved": "passed",
        "privacy": "not_tested",
    }


def test_consensus_corrupt_weak_auth_splits_consensus(tmp_path):
    bundle, result = run_lab("consensus_corrupt_weak_auth", str(tmp_path))
    failed = {s.name for s in result.stages if s.status == "failed"}

    assert result.verdict == "failed"
    assert failed == {"agreement", "corruption_rejected", "corruption_recovered"}


def test_both_corrupt_bundles_verify(tmp_path):
    hmac_bundle, _ = run_lab("consensus_corrupt", str(tmp_path / "hmac"))
    weak_bundle, _ = run_lab("consensus_corrupt_weak_auth",
                             str(tmp_path / "weak"))

    assert verify_bundle(hmac_bundle) == []
    assert verify_bundle(weak_bundle) == []


def test_removing_the_rejection_event_only_fails_corruption_rejected():
    spec = load_bundled("consensus_corrupt")
    engine = build_engine(spec)
    engine.run()
    healthy = evaluate_scenario(spec, engine.run_id, engine.events)
    healthy_status = {s.name: s.status for s in healthy.stages}
    assert healthy_status["corruption_rejected"] == "passed"

    mutated = [e for e in engine.events if e.kind != "delivery_failed"]
    result = evaluate_scenario(spec, engine.run_id, mutated)
    mutated_status = {s.name: s.status for s in result.stages}

    changed = {name for name in healthy_status
              if healthy_status[name] != mutated_status[name]}
    assert changed == {"corruption_rejected"}
    assert mutated_status["corruption_rejected"] == "failed"


def test_same_seed_gives_the_same_events(tmp_path):
    bundle_a, _ = run_lab("consensus_corrupt", str(tmp_path / "a"))
    bundle_b, _ = run_lab("consensus_corrupt", str(tmp_path / "b"))

    assert _events_digest(bundle_a) == _events_digest(bundle_b)


def test_noop_corruption_is_honestly_not_enough_evidence():
    spec = load_bundled("consensus_corrupt")
    spec.faults = [FaultRule(action="corrupt", kind="commit", nth=2,
                             field="value", value="v42")]
    engine = build_engine(spec)
    engine.run()

    assert not [e for e in engine.events if e.kind == "message_corrupted"]
    result = evaluate_scenario(spec, engine.run_id, engine.events)
    stage = next(s for s in result.stages if s.name == "corruption_injected")
    assert stage.status == "not_enough_evidence"


def test_commit_retry_is_bounded_by_max_commit_retries(tmp_path):
    spec = load_bundled("consensus_corrupt")
    spec.faults = list(spec.faults) + [
        FaultRule(action="drop_rate", kind="commit_ack", rate=1.0)]
    engine = build_engine(spec)
    engine.run()

    retries = [e for e in engine.events if e.kind == "commit_retry"]
    max_commit_retries = 3
    assert len(retries) == max_commit_retries


def test_consensus_corrupt_without_the_fault_has_no_injection_evidence():
    spec = load_bundled("consensus_corrupt")
    spec.faults = []
    engine = build_engine(spec)
    engine.run()

    result = evaluate_scenario(spec, engine.run_id, engine.events)
    stage = next(s for s in result.stages if s.name == "corruption_injected")
    assert stage.status == "not_enough_evidence"
