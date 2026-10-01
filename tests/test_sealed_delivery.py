"""Hash-locked escrow: payment and the goods move together.

A deadline refund alone (deadline.v1, the idea of open PR #291) keeps
money from being stranded, but goods that arrive late can still be
opened: the payer ends up with the goods and the money.
hashlock.v1 sells sealed goods. The payer pays holding the sealed box;
the payee is paid only by handing the ledger the key, which the ledger
checks (deadline, key digest, AES-GCM decryption, listed content) and
reveals to the payer in the same step.
"""

import heapq

import pytest

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.layers import resolve
from nandatown.layers.payments import PaymentError, digest, seal, unseal
from nandatown.records import TownEvent
from nandatown.sim.engine import Engine
from nandatown.sim.runner import run_lab
from nandatown.sim.scenario import ScenarioSpec, load_bundled
from nandatown.sim.validators import Trace, sealed_delivery

KEY = bytes(range(16))
NONCE = bytes(range(12))
DATA = b"junction,hour,vehicles\nJ1,08,412\n"


def _ledger(payments="hashlock.v1"):
    spec = ScenarioSpec(name="t", layers={"payments": payments},
                        agents=[{"name": "a", "role": "buyer"}])
    engine = Engine(spec)
    ledger = engine.layers["payments"]
    ledger.open_account("buyer", 5000)
    ledger.open_account("seller", 0)
    return engine, ledger


def _lock(ledger, box=None, content=DATA):
    box = seal(KEY, NONCE, content) if box is None else box
    ledger.hold_locked("buyer", "seller", 1500, "o", digest(KEY),
                       digest(DATA), box, NONCE)


def _drain(engine):
    while engine._queue:
        at, _, fn = heapq.heappop(engine._queue)
        engine.now = at
        fn()


def _refusal(engine):
    return [e.detail["reason"] for e in engine.events
            if e.kind == "claim_refused"]


# -- the time half: deadline.v1, which hashlock.v1 extends ----------------


@pytest.mark.parametrize("payments", ["deadline.v1", "hashlock.v1"])
def test_an_unreleased_hold_is_refunded_at_its_deadline(payments):
    engine, ledger = _ledger(payments)
    ledger.hold("buyer", 300, "p")
    _drain(engine)
    assert ledger.balance("buyer") == 5000
    assert [e.kind for e in engine.events if e.subject == "p"] == [
        "escrow_held", "escrow_deadline_set", "escrow_expired",
        "escrow_refunded"]
    assert engine.events[-1].at == ledger.TIMEOUT


@pytest.mark.parametrize("payments", ["deadline.v1", "hashlock.v1"])
def test_a_release_before_the_deadline_leaves_the_timer_a_no_op(payments):
    engine, ledger = _ledger(payments)
    ledger.hold("buyer", 300, "p")
    ledger.release("p", "seller")
    _drain(engine)
    assert ledger.balance("seller") == 300
    assert not [e for e in engine.events if e.kind == "escrow_expired"]


def test_a_release_after_the_refund_is_refused_and_moves_nothing():
    engine, ledger = _ledger("deadline.v1")
    ledger.hold("buyer", 300, "p")
    _drain(engine)
    ledger.release("p", "seller")
    assert (ledger.balance("buyer"), ledger.balance("seller")) == (5000, 0)
    assert engine.events[-1].kind == "escrow_release_refused"


def test_deadline_keeps_ledger_errors_and_hold_validation():
    _, ledger = _ledger("deadline.v1")
    with pytest.raises(PaymentError):
        ledger.release("never-held", "seller")
    with pytest.raises(PaymentError):
        ledger.hold("buyer", 9999, "too-much")
    assert ledger.escrow == {}


# -- the ledger ----------------------------------------------------------


def test_seal_detects_tampering():
    box = seal(KEY, NONCE, DATA)
    assert unseal(KEY, NONCE, box) == DATA
    assert unseal(KEY, NONCE, bytes([box[0] ^ 1]) + box[1:]) is None
    assert unseal(bytes(16), NONCE, box) is None


def test_hashlock_is_registered_and_keeps_the_deadline():
    cls = resolve("payments", "hashlock.v1")
    assert cls.TIMEOUT == resolve("payments", "deadline.v1").TIMEOUT


def test_a_valid_claim_pays_and_reveals_the_key_in_one_step():
    engine, ledger = _ledger()
    _lock(ledger)
    assert ledger.claim("o", "seller", KEY) is True
    assert ledger.balance("seller") == 1500
    assert ledger.revealed["o"] == KEY
    kinds = [e.kind for e in engine.events if e.subject == "o"]
    assert kinds[-3:] == ["escrow_released", "payment_settled",
                          "key_revealed"]


@pytest.mark.parametrize("claimant,key,box,content,reason", [
    ("seller", bytes(16), None, DATA, "key does not match its digest"),
    ("mallory", KEY, None, DATA, "claimant is not the payee"),
    ("seller", KEY, b"\x00" * 40, DATA, "box does not decrypt"),
    ("seller", KEY, None, b"other bytes", "content does not match"),
])
def test_a_bad_claim_pays_nothing_and_reveals_nothing(claimant, key, box,
                                                      content, reason):
    engine, ledger = _ledger()
    _lock(ledger, box=box, content=content)
    assert ledger.claim("o", claimant, key) is False
    assert reason in _refusal(engine)[0]
    assert ledger.balance("seller") == 0
    assert "o" not in ledger.revealed
    _drain(engine)
    assert ledger.balance("buyer") == 5000


def test_a_claim_after_the_deadline_is_refused():
    engine, ledger = _ledger()
    _lock(ledger)
    _drain(engine)
    assert ledger.claim("o", "seller", KEY) is False
    assert _refusal(engine) == ["deadline passed"]
    assert ledger.balance("seller") == 0
    assert ledger.balance("buyer") == 5000
    assert "o" not in ledger.revealed


def test_plain_release_cannot_bypass_the_key():
    engine, ledger = _ledger()
    _lock(ledger)
    ledger.release("o", "seller")
    assert ledger.balance("seller") == 0
    assert ledger.escrow["o"]["state"] == "held"
    assert any(e.kind == "escrow_release_refused" for e in engine.events)


def test_a_buyer_refund_before_the_claim_never_yields_the_goods():
    """The buyer may cancel before the seller claims, for instance while
    the claim is still in transit. It gets its money back but never the
    key, so it cannot end up with both: the box it holds stays sealed.
    The seller's later claim is refused and the deadline refunds nothing
    a second time."""
    engine, ledger = _ledger()
    _lock(ledger)
    ledger.refund("o")
    assert ledger.balance("buyer") == 5000
    assert ledger.claim("o", "seller", KEY) is False
    assert _refusal(engine) == ["escrow refunded"]
    assert ledger.balance("seller") == 0
    assert "o" not in ledger.revealed
    _drain(engine)
    assert ledger.balance("buyer") == 5000
    assert [e.kind for e in engine.events].count("escrow_refunded") == 1


def test_claim_on_an_unlocked_ref_raises():
    _, ledger = _ledger()
    ledger.hold("buyer", 100, "plain")
    with pytest.raises(PaymentError):
        ledger.claim("plain", "seller", KEY)


# -- the scenario and its negative control -------------------------------


def _stages(result):
    return {s.name: s for s in result.stages}


def test_sealed_delivery_passes_and_verifies(tmp_path):
    bundle_dir, result = run_lab("sealed_delivery", str(tmp_path))
    stages = _stages(result)
    assert result.verdict == "passed", {
        n: (s.status, s.note) for n, s in stages.items()}
    assert verify_bundle(bundle_dir) == []
    events = load_bundle(bundle_dir)["events"]
    refused = {e.subject: e.detail["reason"] for e in events
               if e.kind == "claim_refused"}
    assert refused == {
        "order-buyer-1-1": "deadline passed",
        "order-buyer-4-1": "content does not match the listing"}
    opened = sorted(e.subject for e in events if e.kind == "goods_opened")
    assert opened == ["order-buyer-2-1", "order-buyer-3-1"]


def test_without_hashlock_late_goods_are_opened_unpaid(tmp_path):
    """The negative control: deadline.v1, so the seller sends the key
    itself. The late key reaches buyer-1 after its refund; it opens the
    goods it no longer pays for."""
    bundle_dir, result = run_lab("sealed_delivery_no_hashlock",
                                 str(tmp_path))
    stages = _stages(result)
    assert result.verdict == "failed"
    assert stages["atomic_exchange"].status == "failed"
    assert stages["atomic_exchange"].note == (
        "order-buyer-1-1 goods opened without payment")
    assert stages["escrow_resolved"].status == "passed"
    assert stages["ledger_conserved"].status == "passed"
    for name in ["wrong_content_refused", "late_claim_refused"]:
        assert stages[name].status == "not_enough_evidence", name
    assert verify_bundle(bundle_dir) == []


def test_the_control_differs_only_in_the_payments_layer():
    on = load_bundled("sealed_delivery")
    off = load_bundled("sealed_delivery_no_hashlock")
    assert (on.layers["payments"], off.layers["payments"]) == (
        "hashlock.v1", "deadline.v1")
    assert on.agents == off.agents and on.faults == off.faults
    assert off.validator == "sealed_delivery"


# -- the validator re-checks the record ----------------------------------


def _ev(i, kind, subject, observer="town", **detail):
    return TownEvent(event_id=f"ev-{i}", run_id="r", at=float(i),
                     observer=observer, kind=kind, subject=subject,
                     detail=detail)


def _judge(events):
    spec = load_bundled("sealed_delivery")
    return {s.name: s for s in sealed_delivery(spec, Trace(events, "r"))}


def _trade(key_hex):
    return [
        _ev(1, "box_accepted", "o", observer="b", box_digest="sha256:x"),
        _ev(2, "escrow_held", "o", **{"from": "b", "cents": 5}),
        _ev(3, "escrow_hashlocked", "o", payee="s",
            key_digest=digest(KEY)),
        _ev(4, "escrow_released", "o", to="s", cents=5),
        _ev(5, "key_revealed", "o", to="b", key_hex=key_hex),
        _ev(6, "goods_opened", "o", observer="b"),
    ]


def test_validator_accepts_a_key_that_hashes_to_its_digest():
    assert _judge(_trade(KEY.hex()))["atomic_exchange"].status == "passed"


def test_validator_rehashes_the_revealed_key():
    stages = _judge(_trade(bytes(16).hex()))
    assert stages["atomic_exchange"].status == "failed"
    assert "does not match its digest" in stages["atomic_exchange"].note


def test_validator_fails_payment_before_a_verified_box():
    events = _trade(KEY.hex())[1:]
    stages = _judge(events)
    assert stages["paid_only_for_verified_box"].status == "failed"


def test_validator_fails_payment_without_the_goods_opening():
    events = _trade(KEY.hex())[:-1]
    stages = _judge(events)
    assert stages["atomic_exchange"].status == "failed"
    assert "paid without the goods opening" in stages[
        "atomic_exchange"].note


# -- regressions found in review -----------------------------------------


def _spec_with(faults=(), buyer_balance=5000):
    return ScenarioSpec.model_validate({
        "name": "probe", "validator": "sealed_delivery", "seed": 42,
        "layers": {"payments": "hashlock.v1"},
        "agents": [
            {"name": "seller-a", "role": "data-seller",
             "config": {"sku": "t", "price_cents": 1500,
                        "dataset": "a,b\n1,2\n", "balance_cents": 0}},
            {"name": "buyer-1", "role": "data-buyer",
             "config": {"sku": "t", "max_cents": 2000,
                        "balance_cents": buyer_balance, "start_at": 0.5}}],
        "faults": list(faults), "max_time": 60})


def _run_spec(spec):
    from nandatown.sim.runner import build_engine
    engine = build_engine(spec)
    engine.run()
    return engine


def test_a_stale_box_after_a_retry_still_trades():
    """Box 1 is delayed until after the timeout re-request but before
    box 2. The buyer pays for box 1, so the seller must claim with box
    1's key, not the latest one it issued."""
    engine = _run_spec(_spec_with(
        [{"action": "delay", "kind": "sealed_box", "nth": 1,
          "delay": 1.9}]))
    kinds = [e.kind for e in engine.events]
    assert "claim_refused" not in kinds
    assert "key_revealed" in kinds and "goods_opened" in kinds


def test_a_buyer_that_cannot_pay_gives_up_instead_of_crashing():
    engine = _run_spec(_spec_with(buyer_balance=100))
    gave_up = [e for e in engine.events if e.kind == "buyer_gave_up"]
    assert gave_up and gave_up[0].detail["reason"] == "insufficient funds"
    assert not [e for e in engine.events if e.kind == "escrow_held"]


def test_a_non_bytes_key_is_refused_not_raised():
    engine, ledger = _ledger()
    _lock(ledger)
    assert ledger.claim("o", "seller", KEY.hex()) is False
    assert _refusal(engine) == ["key is not bytes"]


def test_a_notice_for_an_unknown_key_is_recorded_not_raised():
    engine = _run_spec(_spec_with())
    seller = engine.agents["seller-a"]
    seller.on_message({"message_id": "m-x", "conversation": "c-x",
                       "sender": "buyer-1", "to": "seller-a",
                       "kind": "escrow_locked",
                       "body": {"order_id": "nope", "key_digest": "x"}})
    assert engine.events[-1].kind == "claim_skipped"


def test_validator_ignores_ledger_events_an_agent_asserted():
    """api.observe lets an agent record any kind. Money and key facts
    count only when the town observed them."""
    events = _trade(KEY.hex())
    forged = [e.model_copy(update={"observer": "b"})
              if e.kind in ("escrow_released", "key_revealed") else e
              for e in events]
    stages = _judge(forged)
    assert stages["escrow_resolved"].status == "failed"
    assert stages["atomic_exchange"].status == "failed"


def test_validator_ignores_goods_opened_asserted_by_someone_else():
    events = [e.model_copy(update={"observer": "s"})
              if e.kind == "goods_opened" else e
              for e in _trade(KEY.hex())]
    assert _judge(events)["atomic_exchange"].status == "failed"


@pytest.mark.parametrize("scenario", [
    "marketplace", "auction", "voting", "consensus", "supply_chain",
    "capability_spoofing"])
def test_hashlock_is_a_drop_in_for_every_bundled_scenario(tmp_path,
                                                          scenario):
    """Plain holds behave as on ledger.v1 plus a deadline none of these
    scenarios reaches, so every stage verdict is unchanged."""
    _, base = run_lab(scenario, str(tmp_path / "base"))
    _, swapped = run_lab(scenario, str(tmp_path / "swap"),
                         layer_overrides={"payments": "hashlock.v1"})
    assert [(s.name, s.status) for s in swapped.stages] == [
        (s.name, s.status) for s in base.stages]


def test_a_fault_the_scenario_never_declares_is_not_tested():
    """Fault stages count only when the scenario sets the fault up; a
    run without it neither passes nor blocks on that stage."""
    engine = _run_spec(_spec_with())
    spec = _spec_with()
    stages = {s.name: s for s in sealed_delivery(
        spec, Trace(engine.events, engine.run_id))}
    for name in ["damaged_box_refused", "wrong_content_refused",
                 "late_claim_refused"]:
        assert stages[name].status == "not_tested", name
    assert stages["atomic_exchange"].status == "passed"


def test_a_declared_fault_that_never_fired_is_missing_evidence():
    spec = _spec_with([{"action": "delay", "kind": "escrow_locked",
                        "nth": 5, "delay": 10}])
    engine = _run_spec(spec)
    stages = {s.name: s for s in sealed_delivery(
        spec, Trace(engine.events, engine.run_id))}
    assert stages["late_claim_refused"].status == "not_enough_evidence"
