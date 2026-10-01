"""bound.v1: escrow names its payee, and goods are not taken back for free."""

import hashlib
import json
import os
from copy import deepcopy
from types import SimpleNamespace

import pytest

from nandatown.layers.payments import Ledger, PaymentError
from nandatown.layers.payments_bound_v1 import BoundV1
from nandatown.sim.agents import ROLES, Buyer
from nandatown.sim.runner import build_engine
from nandatown.sim.scenario import FaultRule, load_bundled
from nandatown.sim.validators import (
    Trace,
    evaluate_scenario,
    goods_paid_for,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "ledger_v1_main_digests.json")


class RecordingEngine:
    def __init__(self):
        self.events = []

    def emit(self, observer, kind, subject, detail=None):
        self.events.append(SimpleNamespace(
            observer=observer, kind=kind, subject=subject,
            detail=detail or {}))


def _make(cls):
    engine = RecordingEngine()
    pay = cls(engine)
    pay.open_account("buyer", 10000)
    pay.open_account("seller", 0)
    return pay, engine


@pytest.fixture()
def bound():
    return _make(BoundV1)


def _record_delivery(engine, ref, *, reached=True):
    engine.emit("seller", "message_sent", "m-1",
                {"to": "buyer", "kind": "delivery", "body": {"order_id": ref}})
    engine.emit("town", "message_delivered" if reached else "message_dropped",
                "m-1", {"to": "buyer", "kind": "delivery"})


def run(spec):
    engine = build_engine(spec)
    engine.run()
    return engine, evaluate_scenario(spec, engine.run_id, engine.events)


def with_payments(name, plugin):
    spec = load_bundled(name)
    return spec.model_copy(update={"layers": {**spec.layers,
                                              "payments": plugin}})


def only_agents(spec, names):
    return spec.model_copy(update={
        "agents": [a for a in spec.agents if a.name in names]})


def canonical_digest(spec):
    """Everything a run records except its random run id."""
    engine, result = run(spec)
    rid = engine.run_id

    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if k != "run_id"}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return "RUN" if value == rid else value

    blob = json.dumps({
        "events": [scrub(e.model_dump()) for e in engine.events],
        "intents": [scrub(i) for i in engine.intents],
        "stages": [scrub(s.model_dump()) for s in result.stages],
        "verdict": result.verdict,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def statuses(result):
    return {s.name: s.status for s in result.stages}


# -- the plugin ---------------------------------------------------------


def test_capability_is_an_explicit_flag():
    assert BoundV1.binds_payee is True
    assert not hasattr(Ledger, "binds_payee")


def test_release_to_non_payee_is_refused_and_recorded(bound):
    pay, engine = bound
    pay.hold("buyer", 3600, ref="order-1", payee="seller")
    before = (deepcopy(pay.balances), deepcopy(pay.escrow))

    with pytest.raises(PaymentError):
        pay.release("order-1", "buyer")

    assert (pay.balances, pay.escrow) == before
    assert pay.hold_status("order-1") == {
        "from": "buyer", "payee": "seller", "cents": 3600, "state": "held"}
    refused = engine.events[-1]
    assert refused.kind == "escrow_release_refused"
    assert refused.subject == "order-1"
    assert refused.detail["to"] == "buyer"
    assert refused.detail["payee"] == "seller"


def test_release_to_payee_settles_the_exact_total(bound):
    pay, engine = bound
    pay.hold("buyer", 3600, ref="order-1", payee="seller")
    pay.release("order-1", "seller")

    assert pay.balance("seller") == 3600
    assert pay.balance("buyer") == 6400
    assert pay.hold_status("order-1")["state"] == "released"
    assert pay.total() == 10000
    assert engine.events[-1].detail == {
        "from": "buyer", "to": "seller", "cents": 3600, "via": "escrow"}


@pytest.mark.parametrize("finish", [
    lambda pay: pay.release("order-1", "buyer"),
    lambda pay: pay.refund("order-1"),
], ids=["release-anywhere", "refund"])
def test_hold_without_payee_behaves_exactly_like_ledger_v1(finish):
    results = []
    for cls in (Ledger, BoundV1):
        pay, engine = _make(cls)
        pay.hold("buyer", 3600, ref="order-1")
        _record_delivery(engine, "order-1")
        finish(pay)
        results.append((pay.balances, pay.escrow,
                        [vars(e) for e in engine.events]))
    assert results[0] == results[1]


def test_refund_after_recorded_delivery_is_refused(bound):
    pay, engine = bound
    pay.hold("buyer", 3600, ref="order-1", payee="seller")
    _record_delivery(engine, "order-1")
    before = (deepcopy(pay.balances), deepcopy(pay.escrow))

    with pytest.raises(PaymentError):
        pay.refund("order-1")

    assert (pay.balances, pay.escrow) == before
    refused = engine.events[-1]
    assert refused.kind == "escrow_refund_refused"
    assert refused.detail["to"] == "buyer"
    assert refused.detail["payee"] == "seller"


@pytest.mark.parametrize("delivery", ["none", "dropped"])
def test_refund_is_allowed_when_no_delivery_reached_the_payer(bound,
                                                              delivery):
    pay, engine = bound
    pay.hold("buyer", 3600, ref="order-1", payee="seller")
    if delivery == "dropped":
        _record_delivery(engine, "order-1", reached=False)

    pay.refund("order-1")

    assert pay.balance("buyer") == 10000
    assert pay.hold_status("order-1")["state"] == "refunded"


def test_claim_after_recorded_delivery_pays_the_payee_once(bound):
    pay, engine = bound
    pay.hold("buyer", 3600, ref="order-1", payee="seller")
    _record_delivery(engine, "order-1")

    pay.claim("order-1", "seller")

    assert pay.balance("seller") == 3600
    assert pay.hold_status("order-1")["state"] == "released"
    settled = [e for e in engine.events if e.kind == "payment_settled"]
    assert [e.detail for e in settled] == [
        {"from": "buyer", "to": "seller", "cents": 3600, "via": "escrow"}]
    with pytest.raises(PaymentError):
        pay.claim("order-1", "seller")
    assert pay.balance("seller") == 3600


@pytest.mark.parametrize("case", [
    "no-delivery", "dropped-delivery", "non-payee", "unbound-hold"])
def test_claim_is_refused_and_recorded(bound, case):
    pay, engine = bound
    payee = None if case == "unbound-hold" else "seller"
    pay.hold("buyer", 3600, ref="order-1", payee=payee)
    if case != "no-delivery":
        _record_delivery(engine, "order-1",
                         reached=case != "dropped-delivery")
    claimant = "mallory" if case == "non-payee" else "seller"
    before = (deepcopy(pay.balances), deepcopy(pay.escrow))

    with pytest.raises(PaymentError):
        pay.claim("order-1", claimant)

    assert (pay.balances, pay.escrow) == before
    refused = engine.events[-1]
    assert refused.kind == "escrow_claim_refused"
    assert refused.detail["by"] == claimant


# -- the scenario on both plugins ---------------------------------------


def test_fair_exchange_on_ledger_v1_lets_both_buyers_keep_their_money():
    engine, result = run(with_payments("fair_exchange", "ledger.v1"))
    stages = statuses(result)
    assert result.verdict == "failed"
    assert stages["goods_paid_for"] == "failed"
    assert stages["escrow_bound"] == "failed"
    assert stages["ledger_conserved"] == "passed"
    trace = Trace(engine.events)
    assert [e.detail["to"] for e in trace.find(
        "payment_settled", subject="order-thief-1")] == ["thief"]
    assert trace.find("escrow_refunded", subject="order-refunder-1")
    payments = engine.layers["payments"]
    assert payments.balance("thief") == payments.balance("refunder") == 10000


def test_fair_exchange_on_bound_v1_pays_every_delivery_once():
    """Both take-backs are refused; the honest buyer pays by release and
    the seller claims the two held escrows against recorded deliveries."""
    engine, result = run(load_bundled("fair_exchange"))
    stages = statuses(result)
    assert result.verdict == "passed", stages
    trace = Trace(engine.events)
    assert len(trace.find("escrow_release_refused", to="thief")) == 1
    assert len(trace.find("escrow_refund_refused", to="refunder")) == 1
    for ref in ("order-buyer-1-1", "order-thief-1", "order-refunder-1"):
        settled = trace.find("payment_settled", subject=ref)
        assert [(e.detail["to"], e.detail["cents"]) for e in settled] == [
            ("seller-a", 3600)], ref
    assert not trace.find("escrow_claimed", subject="order-buyer-1-1")
    assert {e.subject for e in trace.find("escrow_claimed")} == {
        "order-thief-1", "order-refunder-1"}
    assert not trace.find("escrow_claim_refused")
    assert engine.layers["payments"].balance("seller-a") == 3 * 3600


def with_delivery_fault(names, fault):
    spec = only_agents(load_bundled("fair_exchange"), {"seller-a", *names})
    return spec.model_copy(update={"faults": [
        FaultRule(kind="delivery", nth=1, **fault)]})


@pytest.mark.parametrize("buyer", ["thief", "refunder", "buyer-1"])
def test_delayed_delivery_is_still_paid_exactly_once(buyer):
    """The delivery lands 2.6 logical seconds after shipping, after the
    first two claims (at 1.0 and 2.0) are refused for want of a recorded
    delivery; the retry at 3.0 settles it, or finds the buyer already
    released, and nothing stays held."""
    engine, result = run(with_delivery_fault({buyer},
                                             {"action": "delay",
                                              "delay": 2.5}))
    trace = Trace(engine.events)
    ref = f"order-{buyer}-1"
    assert trace.find("message_delayed", kind="delivery")
    settled = trace.find("payment_settled", subject=ref)
    assert [(e.detail["to"], e.detail["cents"]) for e in settled] == [
        ("seller-a", 3600)]
    assert [e.detail["reason"] for e in trace.find(
        "escrow_claim_refused", subject=ref)] == [
        "no recorded delivery reached the payer"] * 2
    payments = engine.layers["payments"]
    assert payments.hold_status(ref)["state"] == "released"
    assert payments.balance("seller-a") == 3600
    assert statuses(result)["goods_paid_for"] == "passed"


def test_claim_retries_are_bounded_when_the_delivery_never_arrives():
    engine, _ = run(with_delivery_fault({"thief"}, {"action": "drop"}))
    trace = Trace(engine.events)
    refused = trace.find("escrow_claim_refused", subject="order-thief-1")
    assert len(refused) == 5
    assert not trace.find("payment_settled", subject="order-thief-1")
    # Never delivered, so the money is not the seller's and the buyer may
    # still take it back.
    payments = engine.layers["payments"]
    assert payments.hold_status("order-thief-1")["state"] == "held"
    payments.refund("order-thief-1")
    assert payments.balance("thief") == 10000


def test_honest_trade_is_paid_on_bound_v1():
    """With no attacker the goods are paid for, and escrow_bound stays
    inconclusive rather than passing on no evidence."""
    spec = only_agents(load_bundled("fair_exchange"), {"seller-a", "buyer-1"})
    _, result = run(spec)
    stages = statuses(result)
    assert stages["goods_paid_for"] == "passed"
    assert stages["escrow_bound"] == "not_enough_evidence"


class MisboundBuyer(Buyer):
    """Holds escrow for someone other than the seller it orders from."""

    def _purchase(self, unit_cents):
        orig = self.api.escrow_hold
        self.api.escrow_hold = (lambda cents, ref, payee=None:
                                orig(cents, ref, payee="elsewhere"))
        try:
            super()._purchase(unit_cents)
        finally:
            self.api.escrow_hold = orig


def test_seller_does_not_ship_against_escrow_bound_elsewhere(monkeypatch):
    monkeypatch.setitem(ROLES, "misbound_buyer", MisboundBuyer)
    spec = only_agents(load_bundled("fair_exchange"), {"seller-a", "thief"})
    agents = [a.model_copy(update={"role": "misbound_buyer"})
              if a.name == "thief" else a for a in spec.agents]
    engine, result = run(spec.model_copy(update={"agents": agents}))
    trace = Trace(engine.events)
    assert trace.find("message_sent", kind="order_rejected")
    assert not trace.find("message_sent", kind="delivery")
    assert statuses(result)["goods_paid_for"] == "not_enough_evidence"


# -- tampered traces ----------------------------------------------------


def _tamper(events, kind, subject, mutate):
    out = [e.model_copy(deep=True) for e in events]
    hits = [e for e in out if e.kind == kind and e.subject == subject]
    assert hits, (kind, subject)
    for event in hits:
        mutate(event)
    return out


def _drop(events, kind, subject):
    return [e for e in events if not (e.kind == kind and e.subject == subject)]


ORDER = "order-buyer-1-1"


@pytest.mark.parametrize("tamper", [
    pytest.param(lambda ev: _tamper(
        ev, "payment_settled", ORDER,
        lambda e: e.detail.update(to="buyer-1")), id="paid-to-buyer"),
    pytest.param(lambda ev: _tamper(
        ev, "payment_settled", ORDER,
        lambda e: e.detail.update(cents=3599)), id="short-paid"),
    pytest.param(lambda ev: _tamper(
        ev, "payment_settled", ORDER,
        lambda e: e.detail.update(via=None)), id="paid-outside-escrow"),
    pytest.param(lambda ev: _drop(ev, "payment_settled", ORDER),
                 id="never-paid"),
    pytest.param(lambda ev: _drop(ev, "escrow_held", ORDER),
                 id="no-escrow-hold"),
])
def test_tampered_traces_fail_goods_paid_for(tamper):
    spec = only_agents(load_bundled("fair_exchange"), {"seller-a", "buyer-1"})
    engine, _ = run(spec)
    assert goods_paid_for(Trace(engine.events)).status == "passed"
    assert goods_paid_for(Trace(tamper(engine.events))).status == "failed"


# -- drop-in, ledger.v1 runs unchanged, determinism ---------------------


with open(FIXTURE) as _f:
    MAIN_DIGESTS = json.load(_f)["digests"]


@pytest.mark.parametrize("name", sorted(MAIN_DIGESTS))
def test_existing_scenarios_on_ledger_v1_match_main(name):
    """Events, intents, and stage results of every scenario that existed on
    main are unchanged when payments is ledger.v1."""
    spec = load_bundled(name)
    assert spec.layers["payments"] == "ledger.v1"
    assert canonical_digest(spec) == MAIN_DIGESTS[name]


@pytest.mark.parametrize("name", sorted(MAIN_DIGESTS))
def test_bound_v1_is_a_drop_in_for_existing_scenarios(name):
    _, ledger = run(with_payments(name, "ledger.v1"))
    _, bound = run(with_payments(name, "bound.v1"))
    assert statuses(bound) == statuses(ledger)
    assert bound.verdict == ledger.verdict


@pytest.mark.parametrize("plugin", ["bound.v1", "ledger.v1"])
def test_fair_exchange_is_deterministic(plugin):
    spec = with_payments("fair_exchange", plugin)
    assert canonical_digest(spec) == canonical_digest(spec)
