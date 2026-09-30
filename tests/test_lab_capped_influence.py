"""A single observer must not be able to move a reputation without limit.

reputation.v1 sums every receipt it is handed, so one agent that never
trades can drive a counterparty's score arbitrarily low, and
reputation_consistent approves each step. reputation.capped.v1 caps any
one observer at 1 and gives an observer with no settled trade no weight.
"""

import pytest

from nandatown.sim.api import TownAPI
from nandatown.bundle import verify_bundle
from nandatown.sim.engine import Engine
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.scenario import load_bundled
from nandatown.sim.validators import (
    Trace, evaluate_scenario, reputation_capped, reputation_consistent,
)

CAPPED = "reputation.capped.v1"


def slander(trust_plugin: str, times: int = 5):
    """One observer with no settled trade rates one seller bad, repeatedly."""
    spec = load_bundled("marketplace")
    spec.layers["trust"] = trust_plugin
    engine = Engine(spec)
    engine.layers["identity"].create("slanderer")
    api = TownAPI(engine, "slanderer")
    for _ in range(times):
        api.rate("seller-a", "bad")
    return engine, api


@pytest.mark.parametrize("trust_plugin,want", [
    (CAPPED, 0),           # no settled trade: the reports carry no weight
    ("reputation.v1", -5),  # the gap this change addresses, kept pinned
])
def test_one_observer_with_no_trade_history(trust_plugin, want):
    """Five reports from one voice. Bounded moves nothing; v1 moves five."""
    _, api = slander(trust_plugin)
    assert api.reputation("seller-a") == want


def test_unbounded_slander_still_satisfies_the_reference_arithmetic_check():
    """reputation_consistent replays the +1/-1 formula and finds the
    slander correct, so it cannot detect the attack."""
    engine, _ = slander("reputation.v1")
    assert reputation_consistent(Trace(engine.events)).status == "passed"


def test_an_established_observer_is_still_capped_at_one_point():
    """The weight rule alone would let one trading agent shout."""
    spec = load_bundled("marketplace")
    spec.layers["trust"] = CAPPED
    engine = Engine(spec)
    engine.layers["identity"].create("buyer-real")
    engine.emit("town", "payment_settled", "order-x",
                {"from": "buyer-real", "to": "seller-a", "cents": 10,
                 "via": "escrow"})
    api = TownAPI(engine, "buyer-real")
    for _ in range(4):
        api.rate("seller-a", "good")
    assert api.reputation("seller-a") == 1
    deltas = [e.detail["delta"] for e in engine.events
              if e.kind == "reputation_updated"]
    assert deltas == [1, 0, 0, 0], "only the first report may move the score"


def test_distinct_observers_still_accumulate():
    """The cap is per observer, not a cap on the score itself."""
    spec = load_bundled("marketplace")
    spec.layers["trust"] = CAPPED
    engine = Engine(spec)
    for who in ("buyer-x", "buyer-y"):
        engine.layers["identity"].create(who)
        engine.emit("town", "payment_settled", f"order-{who}",
                    {"from": who, "to": "seller-a", "cents": 10,
                     "via": "escrow"})
        TownAPI(engine, who).rate("seller-a", "good")
    assert engine.layers["trust"].score("seller-a") == 2


def test_a_discarded_report_is_still_recorded():
    """A report that did not count must not vanish from the trace."""
    engine, _ = slander(CAPPED, times=2)
    updates = [e for e in engine.events if e.kind == "reputation_updated"]
    assert len(updates) == 2
    for event in updates:
        assert event.detail["delta"] == 0
        assert event.detail["observer_weight"] == 0
        assert event.detail["reason"] == "observer_has_no_settled_trade"


# -- the companion validator --------------------------------------------


def mixed_trace(trust_plugin: str):
    """One observer that settled a trade, one that never did."""
    spec = load_bundled("marketplace")
    spec.layers["trust"] = trust_plugin
    engine = Engine(spec)
    for who in ("buyer-real", "slanderer"):
        engine.layers["identity"].create(who)
    engine.emit("town", "payment_settled", "order-x",
                {"from": "buyer-real", "to": "seller-a", "cents": 10,
                 "via": "escrow"})
    TownAPI(engine, "buyer-real").rate("seller-a", "good")
    api = TownAPI(engine, "slanderer")
    for _ in range(5):
        api.rate("seller-a", "bad")
    return engine


def test_capped_check_passes_a_capped_trace():
    engine = mixed_trace(CAPPED)
    stage = reputation_capped(Trace(engine.events))
    assert stage.status == "passed", stage.note
    assert engine.layers["trust"].score("seller-a") == 1


def test_capped_check_names_the_full_movement_it_rejects():
    """The note must carry the magnitude, not just the first point of it."""
    stage = reputation_capped(Trace(mixed_trace("reputation.v1").events))
    assert stage.status == "failed"
    assert "-5" in stage.note, stage.note
    assert "slanderer" in stage.note


def test_empty_capped_check_is_missing_not_success():
    assert reputation_capped(Trace([])).status == "not_enough_evidence"


def test_capped_check_does_not_judge_the_reference_formula():
    """The two checks stay separate: neither validates the other's rules."""
    events = mixed_trace(CAPPED).events
    # A capped trace fails the unbounded arithmetic check by construction,
    # which is exactly why reputation_consistent was left alone.
    assert reputation_consistent(Trace(events)).status == "failed"
    assert reputation_capped(Trace(events)).status == "passed"


# -- the scenario and its negative control ------------------------------


def capped_trace(name="capped_influence"):
    spec = load_bundled(name)
    engine = build_engine(spec)
    engine.run()
    return spec, [event.model_copy(deep=True) for event in engine.events]


def stage_of(spec, events, name="influence_capped"):
    result = evaluate_scenario(spec, events[0].run_id, events)
    return next(s for s in result.stages if s.name == name)


def test_capped_influence_scenario_passes_and_verifies(tmp_path):
    bundle_dir, result = run_lab("capped_influence", str(tmp_path))
    stages = {s.name: s.status for s in result.stages}
    assert result.verdict == "passed", stages
    assert stages["influence_capped"] == "passed"
    assert stages["honest_signal_survives"] == "passed"
    assert verify_bundle(bundle_dir) == []


def test_unbounded_control_breaks_the_town(tmp_path):
    """Identical but for the trust layer, and must fail: one voice with no
    trade history buries a seller the buyer actually paid."""
    bundle_dir, result = run_lab("capped_influence_uncapped_control",
                                 str(tmp_path))
    stages = {s.name: s.status for s in result.stages}
    assert result.verdict == "failed", stages
    assert stages["influence_capped"] == "failed"
    assert stages["honest_signal_survives"] == "failed"
    assert stages["slander_filed"] == "passed", "the attack must have run"
    assert stages["honest_trade_completed"] == "passed", "the trade still closes"
    assert verify_bundle(bundle_dir) == []
    note = next(s for s in result.stages if s.name == "influence_capped").note
    assert "-5" in note, note


def test_the_two_scenarios_differ_only_in_the_trust_layer():
    capped = load_bundled("capped_influence")
    control = load_bundled("capped_influence_uncapped_control")
    assert capped.layers["trust"] == CAPPED
    assert control.layers["trust"] == "reputation.v1"
    assert capped.agents == control.agents
    assert capped.seed == control.seed
    differing = {layer for layer in capped.layers
                 if capped.layers[layer] != control.layers[layer]}
    assert differing == {"trust"}


@pytest.mark.parametrize("case,want", [
    ("clean", "passed"),
    ("missing_receipts", "not_enough_evidence"),
    ("reused_receipt", "failed"),
    ("wrong_observer", "failed"),
    ("self_report", "failed"),
    ("wrong_claim", "failed"),
    ("forged_weight_claim", "failed"),
    ("inflated_delta", "failed"),
    ("inflated_score", "failed"),
    ("string_delta", "failed"),
    ("unknown_outcome", "failed"),
])
def test_capped_check_rejects_tampered_traces(case, want):
    """Removing any of these checks must break one of these regressions."""
    spec, events = capped_trace()
    receipts = [e for e in events if e.kind == "receipt_attested"]
    updates = [e for e in events if e.kind == "reputation_updated"]
    counted = next(e for e in updates if e.detail["delta"] != 0)
    discarded = next(e for e in updates if e.detail["delta"] == 0)

    if case == "missing_receipts":
        events = [e for e in events if e.kind != "receipt_attested"]
    elif case == "reused_receipt":
        updates[1].detail["receipt"] = updates[0].detail["receipt"]
    elif case == "wrong_observer":
        counted.observer = "unrelated-buyer"
    elif case == "self_report":
        receipts[0].observer = counted.observer = counted.subject
    elif case == "wrong_claim":
        receipts[0].detail["claim"] = "endpoint.live"
    elif case == "forged_weight_claim":
        # An observer with no settled trade claiming its report counted.
        discarded.detail.update(delta=-1, score=0, observer_weight=1,
                                reason="counted")
    elif case == "inflated_delta":
        counted.detail["delta"] = 2
    elif case == "inflated_score":
        counted.detail["score"] = 99
    elif case == "string_delta":
        counted.detail["delta"] = "1"
    elif case == "unknown_outcome":
        counted.detail["outcome"] = "uncertain"

    assert stage_of(spec, events).status == want
