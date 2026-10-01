"""leased.v1: escrow holds that refund themselves when their lease ends,
and the lost_delivery scenario that puts the rule under a dropped message."""

import pytest

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.compare import run_comparison
from nandatown.layers import resolve
from nandatown.layers.payments import PaymentError
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.scenario import FaultRule, bundled_scenarios, load_bundled
from nandatown.sim.validators import Trace, evaluate_scenario, ledger_conserved

from test_layers import FakeEngine
from test_sim import ALL_SCENARIOS, trace_of

CONTROL_STAGES = {
    "delivery_dropped": "passed",
    "hold_leased": "failed",
    "refund_on_schedule": "not_enough_evidence",
    "no_hold_outlives_run": "failed",
    "payer_made_whole": "failed",
    "completed_trade_untouched": "passed",
    "ledger_conserved": "passed",
    "privacy": "passed",
}


def stages(result):
    return {s.name: s.status for s in result.stages}


def stage(result, name):
    return next(s for s in result.stages if s.name == name)


def leased_engine():
    eng = FakeEngine()
    eng.layers["payments"] = resolve("payments", "leased.v1")(eng)
    eng.layers["payments"].open_account("buyer", 10000)
    eng.layers["payments"].open_account("seller", 0)
    return eng


def lost_delivery_events():
    spec = load_bundled("lost_delivery")
    engine = build_engine(spec)
    engine.run()
    return spec, engine.run_id, engine.events


def test_leased_ledger_is_a_registered_payments_plugin():
    assert resolve("payments", "leased.v1").LEASE == 5.0
    assert {"lost_delivery", "lost_delivery_no_lease"} <= set(bundled_scenarios())


def test_a_hold_records_its_lease_and_refunds_itself_when_it_ends():
    eng = leased_engine()
    pay = eng.layers["payments"]
    pay.hold("buyer", 3990, ref="order-1")
    lease = next(e for e in eng.events if e["kind"] == "escrow_leased")
    assert lease["subject"] == "order-1"
    assert lease["detail"] == {"from": "buyer", "cents": 3990,
                               "expires_at": 5.0}
    assert pay.balance("buyer") == 6010
    eng.drain()
    assert eng.now == 5.0
    assert eng.kinds()[-2:] == ["escrow_expired", "escrow_refunded"]
    expired = next(e for e in eng.events if e["kind"] == "escrow_expired")
    assert expired["at"] == 5.0
    assert expired["detail"] == {"from": "buyer", "cents": 3990,
                                 "expires_at": 5.0}
    assert pay.balance("buyer") == 10000
    assert pay.total() == 10000
    with pytest.raises(PaymentError):
        pay.refund("order-1")


def test_a_released_hold_never_expires():
    eng = leased_engine()
    pay = eng.layers["payments"]
    pay.hold("buyer", 3990, ref="order-1")
    pay.release("order-1", "seller")
    eng.drain()
    assert "escrow_expired" not in eng.kinds()
    assert "escrow_refunded" not in eng.kinds()
    assert pay.balance("seller") == 3990
    assert pay.total() == 10000


def test_a_release_after_the_lease_is_refused_and_recorded():
    eng = leased_engine()
    pay = eng.layers["payments"]
    pay.hold("buyer", 3990, ref="order-1")
    eng.drain()
    pay.release("order-1", "seller")
    rejected = [e for e in eng.events if e["kind"] == "escrow_release_rejected"]
    assert [r["subject"] for r in rejected] == ["order-1"]
    assert rejected[0]["detail"] == {"to": "seller", "cents": 3990,
                                     "reason": "lease expired",
                                     "expires_at": 5.0}
    assert "payment_settled" not in eng.kinds()
    assert pay.balance("buyer") == 10000
    assert pay.balance("seller") == 0


def test_a_release_landing_exactly_at_the_lease_end_is_refused():
    spec = load_bundled("lost_delivery")
    spec.agents = []
    engine = build_engine(spec)
    pay = engine.layers["payments"]
    pay.open_account("buyer", 10000)
    pay.open_account("seller", 0)
    pay.hold("buyer", 3990, ref="order-1")
    engine.schedule(pay.LEASE, lambda: pay.release("order-1", "seller"))
    engine.run()
    assert [(e.kind, e.at) for e in engine.events
            if e.subject == "order-1"] == [
        ("escrow_held", 0.0), ("escrow_leased", 0.0),
        ("escrow_expired", 5.0), ("escrow_refunded", 5.0),
        ("escrow_release_rejected", 5.0)]
    assert pay.balance("buyer") == 10000
    assert pay.balance("seller") == 0

def test_the_ledger_rules_still_hold_under_a_lease():
    eng = leased_engine()
    pay = eng.layers["payments"]
    with pytest.raises(PaymentError):
        pay.hold("buyer", 99999, ref="too-much")
    assert "payment_rejected" in eng.kinds()
    eng.drain()
    assert "escrow_leased" not in eng.kinds()
    assert "escrow_expired" not in eng.kinds()
    pay.hold("buyer", 60, ref="order-1")
    with pytest.raises(PaymentError):
        pay.hold("buyer", 10, ref="order-1")
    assert eng.kinds().count("escrow_leased") == 1


def test_lost_delivery_passes_and_verifies(tmp_path):
    bundle_dir, result = run_lab("lost_delivery", str(tmp_path))
    assert result.verdict == "passed", stages(result)
    assert verify_bundle(bundle_dir) == []
    events = load_bundle(bundle_dir)["events"]
    dropped = [e for e in events if e.kind == "message_dropped"]
    assert [(e.detail["kind"], e.detail["to"]) for e in dropped] == [
        ("delivery", "buyer-1")]
    lease = next(e for e in events if e.kind == "escrow_leased"
                 and e.subject == "order-buyer-1-1")
    expired = next(e for e in events if e.kind == "escrow_expired")
    refunded = next(e for e in events if e.kind == "escrow_refunded")
    assert expired.subject == refunded.subject == "order-buyer-1-1"
    assert expired.at == refunded.at == lease.detail["expires_at"]
    assert lease.detail["expires_at"] == lease.at + 5.0
    assert refunded.detail == {"to": "buyer-1", "cents": 3590}
    assert [e.subject for e in events if e.kind == "escrow_released"] == [
        "order-buyer-2-1"]
    assert stage(result, "payer_made_whole").note == (
        "buyer-1 opened with 10000 cents and finished with 10000")
    assert stage(result, "hold_leased").note == (
        "leased order-buyer-1-1 until 6.7, order-buyer-2-1 until 6.7")
    assert stage(result, "refund_on_schedule").note == (
        "order-buyer-1-1 expired at 6.7 and refunded 3590 cents to buyer-1")
    assert stage(result, "no_hold_outlives_run").note == (
        "2 holds settled before the run finished: 1 released, 1 refunded")
    assert stage(result, "completed_trade_untouched").note == (
        "order-buyer-2-1 paid 3590 cents to seller-a once")


def test_the_control_fails_exactly_where_a_lease_would_have_acted(tmp_path):
    bundle_dir, result = run_lab("lost_delivery_no_lease", str(tmp_path))
    assert stages(result) == CONTROL_STAGES
    assert result.verdict == "failed"
    assert verify_bundle(bundle_dir) == []
    assert stage(result, "hold_leased").note == (
        "2 of 2 holds carry no lease: order-buyer-1-1, order-buyer-2-1")
    assert stage(result, "no_hold_outlives_run").note == (
        "still held when the run finished: order-buyer-1-1 (3590 cents)")
    assert stage(result, "payer_made_whole").note == (
        "order-buyer-1-1: 3590 cents held by buyer-1 never came back to it;"
        " buyer-1 opened with 10000 cents and finished with 6410")
    kinds = {e.kind for e in load_bundle(bundle_dir)["events"]}
    assert not kinds & {"escrow_leased", "escrow_expired", "escrow_refunded"}


def test_compare_names_only_the_lease_stages(tmp_path):
    _, comparison = run_comparison("lost_delivery", {"payments": "ledger.v1"},
                                   str(tmp_path))
    assert comparison["variants"]["baseline"]["verdict"] == "passed"
    assert comparison["variants"]["swapped"]["verdict"] == "failed"
    assert comparison["differences"] == [
        "hold_leased", "no_hold_outlives_run", "payer_made_whole",
        "refund_on_schedule"]


def test_same_seed_same_events():
    assert trace_of(load_bundled("lost_delivery")) == trace_of(
        load_bundled("lost_delivery"))


def test_an_early_refund_fails_only_the_schedule_stage():
    spec, run_id, events = lost_delivery_events()
    healthy = stages(evaluate_scenario(spec, run_id, events))
    early = [e.model_copy(update={"at": e.at - 1.0})
             if e.kind in ("escrow_expired", "escrow_refunded") else e
             for e in events]
    result = evaluate_scenario(spec, run_id, early)
    assert stages(result) == {**healthy, "refund_on_schedule": "failed"}
    assert stage(result, "refund_on_schedule").note == (
        "order-buyer-1-1 expired at 5.7, not at its lease end 6.7")


def test_removing_the_expiry_records_leaves_the_money_stranded():
    spec, run_id, events = lost_delivery_events()
    without = [e for e in events
               if e.kind not in ("escrow_expired", "escrow_refunded")]
    result = evaluate_scenario(spec, run_id, without)
    assert stages(result) == {**CONTROL_STAGES, "hold_leased": "passed"}


def test_a_refund_without_a_recorded_lease_is_not_on_schedule():
    spec, run_id, events = lost_delivery_events()
    without = [e for e in events if e.kind != "escrow_leased"]
    result = evaluate_scenario(spec, run_id, without)
    assert stage(result, "hold_leased").status == "failed"
    assert stage(result, "refund_on_schedule").status == "failed"
    assert stage(result, "refund_on_schedule").note == (
        "order-buyer-1-1 expired without a matching lease")
    assert stage(result, "payer_made_whole").status == "passed"
    assert stage(result, "no_hold_outlives_run").status == "passed"


def test_a_refund_to_the_wrong_party_conserves_money_and_still_fails():
    spec, run_id, events = lost_delivery_events()
    misdirected = [e.model_copy(update={"detail": {**e.detail, "to": "seller-a"}})
                   if e.kind == "escrow_refunded" else e for e in events]
    result = evaluate_scenario(spec, run_id, misdirected)
    assert stage(result, "ledger_conserved").status == "passed"
    assert stage(result, "refund_on_schedule").status == "failed"
    assert stage(result, "refund_on_schedule").note == (
        "order-buyer-1-1 expired without one refund of the leased cents to"
        " the payer")
    assert stage(result, "payer_made_whole").status == "failed"
    assert stage(result, "payer_made_whole").note == (
        "order-buyer-1-1: 3590 cents held by buyer-1 never came back to it;"
        " buyer-1 opened with 10000 cents and finished with 6410")
    assert stage(result, "no_hold_outlives_run").status == "passed"


def _rewrite(kind, subject=None, detail=None, **fields):
    def mutate(events):
        out = []
        for e in events:
            if e.kind == kind and subject in (None, e.subject):
                update = dict(fields)
                if detail:
                    update["detail"] = {**e.detail, **detail}
                e = e.model_copy(update=update)
            out.append(e)
        return out
    return mutate


def _without(kind, subject):
    return lambda events: [e for e in events
                           if not (e.kind == kind and e.subject == subject)]


def _twice(kind):
    def mutate(events):
        out = []
        for e in events:
            out.append(e)
            if e.kind == kind:
                out.append(e.model_copy(update={"event_id": e.event_id + "b"}))
        return out
    return mutate


def _strip_dropped_order(events):
    dropped = {e.subject for e in events if e.kind == "message_dropped"}
    return [e.model_copy(update={"detail": {**e.detail, "body": {}}})
            if e.kind == "message_sent" and e.subject in dropped else e
            for e in events]


@pytest.mark.parametrize("label, mutate, stage_name, status", [
    ("lease end is not a number",
     _rewrite("escrow_leased", detail={"expires_at": "soon"}),
     "hold_leased", "failed"),
    ("lease recorded at another time than its hold",
     _rewrite("escrow_leased", at=0.0), "hold_leased", "failed"),
    ("expiry names other terms than the lease",
     _rewrite("escrow_expired", detail={"cents": 3589}),
     "refund_on_schedule", "failed"),
    ("refund is short", _rewrite("escrow_refunded", detail={"cents": 3589}),
     "refund_on_schedule", "failed"),
    ("refund recorded twice", _twice("escrow_refunded"),
     "refund_on_schedule", "failed"),
    ("completed trade paid the wrong party",
     _rewrite("escrow_released", detail={"to": "buyer-1"}),
     "completed_trade_untouched", "failed"),
    ("expiry recorded by the buyer, not the town",
     _rewrite("escrow_expired", observer="buyer-1"),
     "refund_on_schedule", "failed"),
    ("refund recorded by the buyer, not the town",
     _rewrite("escrow_refunded", observer="buyer-1"),
     "payer_made_whole", "failed"),
    ("the drop recorded by the buyer, not the town",
     _rewrite("message_dropped", observer="buyer-1"),
     "delivery_dropped", "not_enough_evidence"),
    ("the deliveries recorded by a buyer, not the town",
     _rewrite("message_delivered", observer="buyer-2"),
     "completed_trade_untouched", "not_enough_evidence"),
    ("no hold recorded at all",
     lambda events: [e for e in events if e.kind != "escrow_held"],
     "hold_leased", "not_enough_evidence"),
    ("dropped delivery carries no order", _strip_dropped_order,
     "payer_made_whole", "not_enough_evidence"),
    ("the stranded hold's own record is missing",
     _without("escrow_held", "order-buyer-1-1"),
     "payer_made_whole", "not_enough_evidence"),
])
def test_tampered_lease_records_never_pass(label, mutate, stage_name, status):
    spec, run_id, events = lost_delivery_events()
    result = evaluate_scenario(spec, run_id, mutate(events))
    assert result.verdict != "passed", label
    assert stage(result, stage_name).status == status, label


def test_a_delivery_that_outlives_the_lease_cannot_release_the_refund(tmp_path):
    spec = load_bundled("lost_delivery")
    spec.faults = [FaultRule(action="delay", kind="delivery", nth=1,
                             delay=6.0)]
    engine = build_engine(spec)
    engine.run()
    kinds = [e.kind for e in engine.events]
    rejected = [e for e in engine.events
                if e.kind == "escrow_release_rejected"]
    assert [r.subject for r in rejected] == ["order-buyer-1-1"]
    assert rejected[0].detail["reason"] == "lease expired"
    assert kinds.index("escrow_expired") < kinds.index(
        "escrow_release_rejected")
    assert [e.subject for e in engine.events
            if e.kind == "payment_settled"] == ["order-buyer-2-1"]
    pay = engine.layers["payments"]
    assert pay.balance("buyer-1") == 10000
    assert pay.balance("seller-a") == 3590
    assert pay.total() == 20000
    assert ledger_conserved(Trace(engine.events)).status == "passed"


@pytest.mark.parametrize("payments, closing", [("ledger.v1", 6410),
                                               ("leased.v1", 10000)])
def test_an_out_of_stock_reply_strands_a_hold_unless_it_is_leased(payments, closing):
    spec = load_bundled("lost_delivery")
    spec.faults = []
    spec.layers["payments"] = payments
    spec.agents = [a.model_copy(update={"config": {**a.config, "stock": 2}})
                   if a.role == "seller" else a for a in spec.agents]
    engine = build_engine(spec)
    engine.run()
    rejected = [e for e in engine.events if e.kind == "message_sent"
                and e.detail.get("kind") == "order_rejected"]
    assert len(rejected) == 1
    buyer = rejected[0].detail["to"]
    assert any(e.kind == "message_unhandled" and e.observer == buyer
               and e.detail.get("kind") == "order_rejected"
               for e in engine.events)
    pay = engine.layers["payments"]
    assert pay.balance(buyer) == closing
    assert pay.total() == 20000


def _lease_plugin(tmp_path, lease):
    path = tmp_path / "short_lease.py"
    path.write_text(
        "from nandatown.layers import register\n"
        "from nandatown.layers.payments import LeasedLedger\n\n\n"
        '@register("payments", "leased.short.v1")\n'
        "class ShortLease(LeasedLedger):\n"
        f"    LEASE = {lease}\n")
    return str(path)


def test_a_scenario_can_bring_its_own_lease_through_a_plugin_file(tmp_path):
    plugin = _lease_plugin(tmp_path, 1.0)
    bundle_dir, result = run_lab(
        "lost_delivery", str(tmp_path / "runs"), plugins=[plugin],
        layer_overrides={"payments": "leased.short.v1"})
    assert result.verdict == "passed", stages(result)
    assert verify_bundle(bundle_dir) == []
    bundle = load_bundle(bundle_dir)
    lease = next(e for e in bundle["events"] if e.kind == "escrow_leased")
    expired = next(e for e in bundle["events"] if e.kind == "escrow_expired")
    assert lease.detail["expires_at"] == lease.at + 1.0
    assert expired.at == lease.at + 1.0
    assert bundle["run"].config["layers"]["payments"] == "leased.short.v1"
    assert bundle["run"].config["rerun_command"].endswith(
        f"--plugin {plugin} --layer payments=leased.short.v1")


def test_a_lease_shorter_than_an_honest_delivery_fails_the_completed_trade(tmp_path):
    plugin = _lease_plugin(tmp_path, 0.1)
    bundle_dir, result = run_lab(
        "lost_delivery", str(tmp_path / "runs"), plugins=[plugin],
        layer_overrides={"payments": "leased.short.v1"})
    assert result.verdict == "failed"
    assert stages(result) == {
        "delivery_dropped": "passed", "hold_leased": "passed",
        "refund_on_schedule": "passed", "no_hold_outlives_run": "passed",
        "payer_made_whole": "passed", "completed_trade_untouched": "failed",
        "ledger_conserved": "passed", "privacy": "passed"}
    assert stage(result, "completed_trade_untouched").note == (
        "a completed trade did not pay its seller once and only once:"
        " order-buyer-2-1")
    events = load_bundle(bundle_dir)["events"]
    rejected = [e for e in events if e.kind == "escrow_release_rejected"]
    assert [r.subject for r in rejected] == ["order-buyer-2-1"]
    assert not [e for e in events if e.kind == "payment_settled"]
    assert verify_bundle(bundle_dir) == []


@pytest.mark.parametrize("name",
                         [n for n in ALL_SCENARIOS if n != "lost_delivery"])
def test_leased_ledger_is_a_drop_in_for_the_bundled_scenarios(name, tmp_path):
    _, default = run_lab(name, str(tmp_path / "default"))
    bundle_dir, leased = run_lab(name, str(tmp_path / "leased"),
                                 layer_overrides={"payments": "leased.v1"})
    assert leased.verdict == "passed"
    assert stages(leased) == stages(default)
    kinds = {e.kind for e in load_bundle(bundle_dir)["events"]}
    assert not kinds & {"escrow_expired", "escrow_release_rejected"}
    assert verify_bundle(bundle_dir) == []
