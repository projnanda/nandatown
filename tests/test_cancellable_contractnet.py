from copy import deepcopy

from nandatown.bundle import verify_bundle
from nandatown.layers.coordination_contractnet_cancel_v1 import (
    CancellableContractNet,
    NonterminalCancellationControl,
)
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.scenario import FaultRule, load_bundled
from nandatown.sim.validators import evaluate_scenario
from test_layers import FakeEngine


def _coordination():
    return CancellableContractNet(FakeEngine())


def _stage(result, name):
    return next(stage for stage in result.stages if stage.name == name)


def test_cancelled_open_task_rejects_late_bid_and_award():
    engine = FakeEngine()
    coordination = CancellableContractNet(engine)
    coordination.announce(
        "issuer", "task-1", {"work": "inspect"}, rule="lowest"
    )

    assert coordination.cancel("task-1", "issuer") is True
    assert coordination.bid("task-1", "bidder", 400) is False
    assert coordination.award("task-1") is None

    assert "task_cancelled" in engine.kinds()
    assert "task_awarded" not in engine.kinds()


def test_nonissuer_cannot_cancel_an_open_task():
    coordination = _coordination()
    coordination.announce("issuer", "task-1", {"work": "inspect"})

    assert coordination.cancel("task-1", "intruder") is False
    assert coordination.tasks["task-1"]["state"] == "open"
    rejection = coordination.engine.events[-1]
    assert rejection["kind"] == "task_cancel_rejected"
    assert rejection["detail"] == {
        "actor": "intruder", "issuer": "issuer", "reason": "not issuer",
    }


def test_bid_after_cancellation_is_rejected_for_that_reason():
    coordination = _coordination()
    coordination.announce("issuer", "task-1", {"work": "inspect"})
    coordination.cancel("task-1", "issuer")

    assert coordination.bid("task-1", "bidder", 400) is False
    assert coordination.engine.events[-1]["detail"]["reason"] == "task cancelled"


def test_award_after_cancellation_has_no_actionable_winner():
    coordination = _coordination()
    coordination.announce("issuer", "task-1", {"work": "inspect"})
    coordination.cancel("task-1", "issuer")

    assert coordination.award("task-1") is None
    assert coordination.engine.events[-1]["kind"] == "award_rejected"
    assert coordination.engine.events[-1]["detail"]["reason"] == "task cancelled"


def test_uncancelled_contract_net_behavior_is_preserved():
    coordination = _coordination()
    coordination.announce("issuer", "task-1", {"work": "inspect"})

    assert coordination.bid("task-1", "bidder-b", 500) is True
    assert coordination.bid("task-1", "bidder-a", 400) is True
    assert coordination.award("task-1") == ("bidder-a", 400)


def test_cancel_after_award_does_not_rewrite_the_outcome():
    coordination = _coordination()
    coordination.announce("issuer", "task-1", {"work": "inspect"})
    coordination.bid("task-1", "bidder", 400)
    assert coordination.award("task-1") == ("bidder", 400)

    assert coordination.cancel("task-1", "issuer") is False
    assert coordination.tasks["task-1"]["state"] == "closed"
    assert coordination.tasks["task-1"]["winner"] == ("bidder", 400)
    assert coordination.engine.events[-1]["detail"]["reason"] == "task closed"


def test_cancelled_task_id_cannot_be_reannounced():
    coordination = _coordination()
    coordination.announce("issuer", "task-1", {"work": "inspect"})
    coordination.cancel("task-1", "issuer")

    assert coordination.announce(
        "issuer", "task-1", {"work": "replacement"}
    ) is False
    assert coordination.tasks["task-1"]["state"] == "cancelled"
    assert coordination.tasks["task-1"]["spec"] == {"work": "inspect"}
    assert coordination.engine.events[-1]["kind"] == "task_announce_rejected"
    assert coordination.engine.events[-1]["detail"]["reason"] == "task cancelled"


def test_non_cancelled_task_id_keeps_stock_reannouncement_behavior():
    coordination = _coordination()
    coordination.announce("first", "task-1", {"work": "inspect"})
    coordination.bid("task-1", "bidder", 400)

    assert coordination.announce(
        "replacement", "task-1", {"work": "replace"}, rule="highest"
    ) is None
    assert coordination.tasks["task-1"] == {
        "issuer": "replacement",
        "spec": {"work": "replace"},
        "rule": "highest",
        "bids": {},
        "state": "open",
        "winner": None,
    }
    assert coordination.engine.events[-1]["kind"] == "task_announced"


def test_faulted_cancellation_scenario_is_deterministic():
    traces = []
    for _ in range(2):
        spec = load_bundled("cancellable_contractnet")
        engine = build_engine(spec)
        engine.run()
        traces.append([
            event.model_dump(exclude={"run_id"})
            for event in engine.events
            if event.kind != "run_created" and event.kind != "run_finished"
        ])

    assert traces[0] == traces[1]


def test_positive_and_nonterminal_negative_control_bundles_verify(tmp_path):
    positive_bundle, positive = run_lab(
        "cancellable_contractnet", str(tmp_path / "positive")
    )
    negative_bundle, negative = run_lab(
        "cancellable_contractnet", str(tmp_path / "negative"),
        layer_overrides={
            "coordination": "contractnet.cancel.nonterminal.v1",
        },
    )

    assert positive.verdict == "passed"
    assert verify_bundle(positive_bundle) == []
    assert negative.verdict == "failed"
    assert _stage(negative, "fault_exercised").status == "passed"
    assert _stage(negative, "task_cancelled").status == "passed"
    assert _stage(negative, "late_bid_rejected").status == "failed"
    assert _stage(negative, "cancelled_task_not_awarded").status == "failed"
    assert verify_bundle(negative_bundle) == []


def test_validator_reports_missing_cancellation_as_insufficient():
    spec = load_bundled("cancellable_contractnet")
    engine = build_engine(spec)
    engine.run()
    events = [deepcopy(event) for event in engine.events
              if event.kind != "task_cancelled"]

    result = evaluate_scenario(spec, engine.run_id, events)

    assert _stage(result, "task_cancelled").status == "not_enough_evidence"
    assert result.verdict != "passed"


def test_validator_rejects_cancellation_attributed_to_another_actor():
    spec = load_bundled("cancellable_contractnet")
    engine = build_engine(spec)
    engine.run()
    events = [deepcopy(event) for event in engine.events]
    cancellation = next(event for event in events
                        if event.kind == "task_cancelled")
    cancellation.observer = "intruder"

    result = evaluate_scenario(spec, engine.run_id, events)

    assert _stage(result, "task_cancelled").status == "failed"
    assert result.verdict == "failed"


def test_validator_requires_declared_task_bid_delay_fault():
    spec = load_bundled("cancellable_contractnet")
    engine = build_engine(spec)
    engine.run()
    without_fault = spec.model_copy(update={"faults": []})

    result = evaluate_scenario(without_fault, engine.run_id, engine.events)

    assert _stage(result, "fault_exercised").status == "failed"
    assert result.verdict == "failed"


def test_validator_binds_delay_evidence_to_declared_duration():
    spec = load_bundled("cancellable_contractnet")
    engine = build_engine(spec)
    engine.run()
    other_delay = FaultRule(
        action="delay", kind="task_bid", nth=1, delay=2.0
    )
    changed_fault = spec.model_copy(update={"faults": [other_delay]})

    result = evaluate_scenario(changed_fault, engine.run_id, engine.events)

    assert _stage(result, "fault_exercised").status == "failed"
    assert result.verdict == "failed"


def test_negative_control_is_intentionally_nonterminal():
    engine = FakeEngine()
    coordination = NonterminalCancellationControl(engine)
    coordination.announce("issuer", "task-1", {"work": "inspect"})

    assert coordination.cancel("task-1", "issuer") is True
    assert coordination.bid("task-1", "bidder", 400) is True
    assert coordination.award("task-1") == ("bidder", 400)
