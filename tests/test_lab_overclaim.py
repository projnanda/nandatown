"""A seller that says it is certain and is not should not outrank one that
says what it will do and does it.

reputation.v1 sums good and bad ratings, so the seller with the most
customers is listed first even when it honours fewer of its orders.
reputation.calibrated.v1 scores a seller on how well the confidence it
stated before each order matched what arrived.
"""

import pytest

from nandatown.bundle import verify_bundle
from nandatown.layers.trust_calibrated import score_of
from nandatown.sim.api import TownAPI
from nandatown.sim.engine import Engine
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.scenario import load_bundled
from nandatown.sim.validators import (
    Trace, evaluate_scenario, reputation_calibrated, reputation_consistent,
)

CALIBRATED = "reputation.calibrated.v1"


def town(trust_plugin: str = CALIBRATED):
    spec = load_bundled("marketplace")
    spec.layers["trust"] = trust_plugin
    engine = Engine(spec)
    apis = {}
    for name in ("seller", "buyer", "other"):
        engine.layers["identity"].create(name)
        apis[name] = TownAPI(engine, name)
    return engine, apis


def trade(apis, number: int, confidence: float, outcome: str,
          buyer: str = "buyer"):
    apis["seller"].observe("confidence_stated", f"order-{number}",
                           {"confidence": confidence, "to": buyer})
    apis[buyer].rate("seller", outcome)


def updates(engine):
    return [e for e in engine.events if e.kind == "reputation_updated"]


def status(engine):
    return reputation_calibrated(Trace(engine.events)).status


# -- the formula, pinned by hand ----------------------------------------

def test_fewer_than_ten_settled_claims_scores_the_floor():
    assert score_of([]) == 10
    assert score_of([(0.9, 1)] * 9) == 10


def test_score_of_a_seller_that_does_what_it_says():
    """Twenty claims at 0.95, nineteen held. One bin: ece = |0.95 - 0.95|
    = 0, shrunk toward 0.25 by 30 of 50 = 0.15. brier = 0 + 0.95 * 0.05 =
    0.0475, skill = sqrt(1 - 0.19) = 0.9. 0.85 * 0.9 = 0.765 -> 76."""
    assert score_of([(0.95, 1)] * 19 + [(0.95, 0)]) == 76


def test_score_of_a_seller_that_is_sure_and_wrong():
    """Twenty claims at 0.99, fourteen held. brier = 0.29^2 + 0.7 * 0.3 =
    0.2941 > 0.25, so skill is 0 and the score is the floor."""
    assert score_of([(0.99, 1)] * 14 + [(0.99, 0)] * 6) == 10


def test_being_unsure_and_right_about_it_beats_being_sure_and_wrong():
    """Same seven in ten delivered as the test above, but said as 0.7:
    ece 0 shrunk to 0.15, brier = 0.21, skill = sqrt(0.16) = 0.4, and
    0.85 * 0.4 = 0.34 -> 34. Order within the record does not matter."""
    hedged = [(0.7, 1)] * 14 + [(0.7, 0)] * 6
    assert score_of(hedged) == score_of(hedged[::-1]) == 34
    # Knowing which orders will go wrong is worth more still.
    split = [(0.9, 1)] * 9 + [(0.9, 0)] + [(0.1, 0)] * 9 + [(0.1, 1)]
    assert score_of(split) > 50


def test_only_the_latest_two_hundred_claims_count():
    old = [(0.99, 0)] * 500
    recent = [(0.95, 1)] * 190 + [(0.95, 0)] * 10
    assert score_of(old + recent) == score_of(recent)


def test_bin_edges():
    from nandatown.layers.trust_calibrated import bin_index
    assert [bin_index(c) for c in (0.0, 0.1, 0.29, 0.3, 0.999, 1.0)] == \
        [0, 1, 2, 3, 9, 9]


# -- the layer in a town ------------------------------------------------

def test_scores_move_only_with_enough_settled_claims():
    engine, apis = town()
    for number in range(9):
        trade(apis, number, 0.95, "good")
    assert apis["buyer"].reputation("seller") == 10
    trade(apis, 9, 0.95, "good")
    assert apis["buyer"].reputation("seller") > 10
    assert status(engine) == "passed"


def test_a_rating_with_no_open_claim_moves_nothing_and_says_so():
    engine, apis = town()
    apis["buyer"].rate("seller", "bad")
    detail = updates(engine)[0].detail
    assert (detail["reason"], detail["delta"], detail["score"]) == \
        ("no_open_claim", 0, 10)
    assert status(engine) == "passed"


def test_a_rating_settles_the_claim_addressed_to_the_rater():
    engine, apis = town()
    apis["seller"].observe("confidence_stated", "for-other",
                           {"confidence": 0.2, "to": "other"})
    apis["seller"].observe("confidence_stated", "for-buyer",
                           {"confidence": 0.9, "to": "buyer"})
    apis["seller"].observe("confidence_stated", "for-anyone",
                           {"confidence": 0.5})
    apis["buyer"].rate("seller", "good")
    apis["buyer"].rate("seller", "good")
    apis["buyer"].rate("seller", "good")
    assert [(u.detail["reason"], u.detail.get("decision"))
            for u in updates(engine)] == [
        ("settled", "for-buyer"), ("settled", "for-anyone"),
        ("no_open_claim", None)]
    assert status(engine) == "passed"


@pytest.mark.parametrize("detail", [
    {}, {"confidence": 1.5}, {"confidence": -0.1}, {"confidence": "0.9"},
    {"confidence": True}, {"confidence": 0.9, "to": "seller"},
    {"confidence": 0.9, "to": ""},
])
def test_a_malformed_claim_is_refused_and_opens_nothing(detail):
    engine, apis = town()
    apis["seller"].observe("confidence_stated", "order-1", detail)
    assert [e.subject for e in engine.events
            if e.kind == "confidence_refused"] == ["order-1"]
    apis["buyer"].rate("seller", "good")
    assert updates(engine)[0].detail["reason"] == "no_open_claim"
    assert status(engine) == "passed"


def test_a_claim_cannot_be_restated_after_the_fact():
    """The first claim on a decision stands. Repeating it is harmless;
    changing it is refused, before or after the outcome."""
    engine, apis = town()
    claim = {"confidence": 0.9, "to": "buyer"}
    apis["seller"].observe("confidence_stated", "order-1", claim)
    apis["seller"].observe("confidence_stated", "order-1", claim)
    apis["seller"].observe("confidence_stated", "order-1",
                           {"confidence": 0.2, "to": "buyer"})
    apis["buyer"].rate("seller", "bad")
    apis["seller"].observe("confidence_stated", "order-1",
                           {"confidence": 0.1, "to": "buyer"})
    assert len([e for e in engine.events
                if e.kind == "confidence_refused"]) == 2
    assert [(u.detail["reason"], u.detail.get("confidence"))
            for u in updates(engine)] == [("settled", 0.9)]
    apis["buyer"].rate("seller", "bad")
    assert updates(engine)[1].detail["reason"] == "no_open_claim"
    assert status(engine) == "passed"


def test_an_agent_cannot_rate_itself_into_a_score():
    engine, apis = town()
    apis["seller"].observe("confidence_stated", "order-1",
                           {"confidence": 0.9})
    with pytest.raises(Exception):
        apis["seller"].rate("seller", "good")
    assert updates(engine) == []


def test_the_reference_check_does_not_accept_calibrated_scores():
    """The two formulas are judged by their own checks, not each other's."""
    engine, apis = town()
    for number in range(12):
        trade(apis, number, 0.95, "good")
    assert status(engine) == "passed"
    assert reputation_consistent(Trace(engine.events)).status == "failed"
    reference, apis = town("reputation.v1")
    for number in range(12):
        trade(apis, number, 0.95, "good")
    assert reputation_consistent(Trace(reference.events)).status == "passed"
    assert status(reference) == "failed"


# -- the check against a tampered trace ---------------------------------

def healthy():
    engine, apis = town()
    for number in range(14):
        trade(apis, number, 0.95, "good" if number % 7 else "bad")
    assert status(engine) == "passed"
    return engine


def tampered(change):
    engine = healthy()
    events = [e.model_copy(deep=True) for e in engine.events]
    change(events)
    return reputation_calibrated(Trace(events))


def last_update(events):
    return [e for e in events if e.kind == "reputation_updated"][-1]


@pytest.mark.parametrize("key,value", [
    ("score", 99), ("delta", 5), ("reason", "no_open_claim"),
    ("decision", "order-0"), ("confidence", 0.5), ("formula", "other"),
    ("outcome", "bad"), ("score", 76.0),
])
def test_a_changed_score_record_is_caught(key, value):
    def change(events):
        last_update(events).detail[key] = value
    assert tampered(change).status == "failed"


def test_a_raised_claim_is_caught():
    def change(events):
        first = next(e for e in events if e.kind == "confidence_stated")
        first.detail["confidence"] = 0.5
    assert tampered(change).status == "failed"


def test_a_removed_claim_is_caught():
    def change(events):
        events.remove(next(e for e in events
                           if e.kind == "confidence_stated"))
    assert tampered(change).status == "failed"


def test_a_score_resting_on_someone_elses_receipt_is_caught():
    def change(events):
        last_update(events).observer = "other"
    assert tampered(change).status == "failed"


def test_one_receipt_cannot_back_two_scores():
    def change(events):
        scores = [e for e in events if e.kind == "reputation_updated"]
        scores[-1].detail["receipt"] = scores[-2].detail["receipt"]
    assert tampered(change).status == "failed"


def test_a_receipt_recorded_after_its_score_is_caught():
    def change(events):
        score = last_update(events)
        receipt = next(e for e in events if e.kind == "receipt_attested"
                       and e.detail["record_id"] == score.detail["receipt"])
        events.remove(receipt)
        events.append(receipt)
    assert tampered(change).status == "failed"


def test_a_missing_receipt_is_not_enough_evidence():
    def change(events):
        score = last_update(events)
        events.remove(next(
            e for e in events if e.kind == "receipt_attested"
            and e.detail["record_id"] == score.detail["receipt"]))
    assert tampered(change).status == "not_enough_evidence"


def test_an_unrecorded_refusal_is_caught():
    def change(events):
        claim = next(e for e in events if e.kind == "confidence_stated")
        extra = claim.model_copy(deep=True)
        extra.event_id = "extra"
        extra.detail["confidence"] = 0.1
        events.append(extra)
    assert tampered(change).status == "failed"


def test_an_invented_refusal_is_caught():
    def change(events):
        claim = next(e for e in events if e.kind == "confidence_stated")
        extra = claim.model_copy(deep=True)
        extra.event_id, extra.kind = "extra", "confidence_refused"
        events.append(extra)
    assert tampered(change).status == "failed"


def test_duplicate_score_event_ids_are_caught():
    def change(events):
        scores = [e for e in events if e.kind == "reputation_updated"]
        scores[-1].event_id = scores[0].event_id
    assert tampered(change).status == "failed"


def test_no_scores_is_not_enough_evidence():
    assert reputation_calibrated(Trace([])).status == "not_enough_evidence"


# -- the scenario and its control ---------------------------------------

def stages_of(name, tmp_path):
    bundle_dir, result = run_lab(name, str(tmp_path))
    assert verify_bundle(bundle_dir) == []
    return result, {s.name: s.status for s in result.stages}


def test_overclaim_scenario_passes(tmp_path):
    result, stages = stages_of("overclaim", tmp_path)
    assert result.verdict == "passed", stages
    for name in ("claims_precede_outcomes", "ratings_follow_outcomes",
                 "overclaim_exercised", "calibrated_scores",
                 "shopper_well_served"):
        assert stages[name] == "passed"


def test_uncalibrated_control_fails_where_it_should(tmp_path):
    """Same town, reference trust: the busy seller is listed first and the
    shopper is let down. Everything the sellers and buyers did still
    checks out; only the trust layer differs."""
    result, stages = stages_of("overclaim_uncalibrated_control", tmp_path)
    assert result.verdict == "failed", stages
    assert stages["claims_precede_outcomes"] == "passed"
    assert stages["ratings_follow_outcomes"] == "passed"
    assert stages["overclaim_exercised"] == "passed"
    assert stages["calibrated_scores"] == "failed"
    assert stages["shopper_well_served"] == "failed"


def test_the_two_scenarios_differ_only_in_the_trust_layer():
    a = load_bundled("overclaim").model_dump()
    b = load_bundled("overclaim_uncalibrated_control").model_dump()
    assert a["layers"]["trust"] == CALIBRATED
    assert b["layers"]["trust"] == "reputation.v1"
    for spec in (a, b):
        spec["layers"].pop("trust")
        for key in ("name", "description", "validator"):
            spec.pop(key)
    assert a == b


def test_where_the_shopper_buys(tmp_path):
    def sellers(name):
        spec = load_bundled(name)
        engine = build_engine(spec)
        engine.run()
        return {e.detail["seller"] for e in engine.events
                if e.kind == "order_settled"
                and e.detail["chosen_by"] == "shopper"}
    assert sellers("overclaim") == {"seller-steady"}
    assert sellers("overclaim_uncalibrated_control") == {"seller-busy"}


def test_overclaim_needs_an_overclaimer():
    """With both sellers saying what they do, nothing is exercised and the
    scenario must not report a pass."""
    spec = load_bundled("overclaim")
    for agent in spec.agents:
        if agent.name == "seller-busy":
            agent.config["says"] = 0.7
    engine = build_engine(spec)
    engine.run()
    result = evaluate_scenario(spec, engine.run_id, engine.events)
    stages = {s.name: s.status for s in result.stages}
    assert stages["overclaim_exercised"] == "failed"
    assert result.verdict != "passed"
