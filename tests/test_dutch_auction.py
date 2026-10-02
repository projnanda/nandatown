"""dutch.v1: a descending clock where a delayed accept keeps its price.

The plugin tests drive the layer directly. The naive twin is the
negative control: the same calls, but it reprices to the clock's current
price, which is the failure the scenario's delay fault exposes.
"""

import pytest

from nandatown.bundle import verify_bundle
from nandatown.layers import resolve
from nandatown.layers.negotiation import NegotiationError
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.scenario import load_bundled
from nandatown.sim.validators import evaluate_scenario


class RecordingEngine:
    def __init__(self):
        self.events = []

    def emit(self, observer, kind, subject, detail=None):
        self.events.append((observer, kind, subject, detail or {}))

    def kinds(self, kind):
        return [e for e in self.events if e[1] == kind]


def clock(plugin="dutch.v1"):
    engine = RecordingEngine()
    neg = resolve("negotiation", plugin)(engine)
    nid = neg.open("seller", "widget", 2000, 50, 1800)
    return engine, neg, nid


def test_dutch_plugins_are_registered():
    assert resolve("negotiation", "dutch.v1").plugin_id == "dutch.v1"
    assert resolve("negotiation", "dutch.naive.v1").plugin_id == \
        "dutch.naive.v1"


def test_clock_falls_one_step_per_tick_and_stops_at_the_floor():
    engine, neg, nid = clock()
    posted = []
    while (p := neg.post(nid, "seller")) is not None:
        posted.append(p)
    assert posted == [(1, 2000), (2, 1950), (3, 1900), (4, 1850), (5, 1800)]
    assert len(engine.kinds("clock_ended")) == 1


def test_an_ended_clock_reports_its_end_once():
    engine, neg, nid = clock()
    for _ in range(20):
        neg.post(nid, "seller")
    assert len(engine.kinds("price_posted")) == 5
    assert len(engine.kinds("clock_ended")) == 1


def test_only_the_seller_posts():
    _, neg, nid = clock()
    with pytest.raises(NegotiationError):
        neg.post(nid, "buyer")


def test_a_late_accept_buys_at_the_tick_it_named():
    engine, neg, nid = clock()
    neg.post(nid, "seller")              # tick 1: 2000
    neg.post(nid, "seller")              # tick 2: 1950  <- buyer presses
    neg.post(nid, "seller")              # tick 3: 1900  (accept in flight)
    neg.post(nid, "seller")              # tick 4: 1850
    assert neg.accept(nid, "buyer", 2, 1950) == 1950
    assert neg.agreed_price(nid) == 1950
    [(_, _, _, detail)] = engine.kinds("offer_accepted")
    assert detail == {"buyer": "buyer", "tick": 2, "cents": 1950}
    assert neg.post(nid, "seller") is None    # a sale stops the clock


def test_naive_twin_reprices_a_late_accept_to_the_current_price():
    _, neg, nid = clock("dutch.naive.v1")
    for _ in range(4):
        neg.post(nid, "seller")
    assert neg.accept(nid, "buyer", 2, 1950) == 1850


@pytest.mark.parametrize("tick, cents", [
    (2, 1900),        # a real tick, but not the price posted at it
    (9, 1950),        # a tick that was never posted
    (2, 1950.0),      # not integer cents
    (2, "1950"),
    (True, 2000),     # bool is not an int tick
])
def test_an_accept_must_name_a_posted_price(tick, cents):
    engine, neg, nid = clock()
    neg.post(nid, "seller")
    neg.post(nid, "seller")
    assert neg.accept(nid, "buyer", tick, cents) is None
    assert engine.kinds("offer_accepted") == []
    [(_, _, _, detail)] = engine.kinds("accept_rejected")
    assert detail["reason"] == "not a posted price"


def test_a_repeated_accept_is_recognized_and_sells_once():
    engine, neg, nid = clock()
    neg.post(nid, "seller")
    assert neg.accept(nid, "buyer", 1, 2000) == 2000
    assert neg.accept(nid, "buyer", 1, 2000) == 2000
    assert len(engine.kinds("offer_accepted")) == 1
    assert len(engine.kinds("duplicate_recognized")) == 1


def test_after_a_sale_a_different_accept_is_rejected():
    engine, neg, nid = clock()
    neg.post(nid, "seller")
    neg.post(nid, "seller")
    neg.accept(nid, "buyer", 1, 2000)
    assert neg.accept(nid, "rival", 2, 1950) is None
    [(_, _, _, detail)] = engine.kinds("accept_rejected")
    assert detail["reason"] == "already sold"


def test_haggle_calls_fail_loudly_instead_of_passing_by_accident():
    _, neg, nid = clock()
    with pytest.raises(NegotiationError):
        neg.start("buyer", "seller", "widget")
    with pytest.raises(NegotiationError):
        neg.offer(nid, "buyer", 1600)
    with pytest.raises(NegotiationError):
        neg.accept(nid, "buyer")


@pytest.mark.parametrize("start, step, floor", [
    (2000, 0, 1500), (1500, 50, 2000), (2000, 50, -1), (2000.0, 50, 1500),
])
def test_open_refuses_a_malformed_clock(start, step, floor):
    neg = resolve("negotiation", "dutch.v1")(RecordingEngine())
    with pytest.raises(NegotiationError):
        neg.open("seller", "widget", start, step, floor)


# -- the scenario and its validator ------------------------------------

DUTCH_STAGES = ["clock", "acceptance", "payment", "delayed_accept"]


def judged(name, edit=None, spec_edit=None):
    spec = load_bundled(name)
    if spec_edit:
        spec = spec_edit(spec)
    engine = build_engine(spec)
    engine.run()
    events = edit(list(engine.events)) if edit else engine.events
    result = evaluate_scenario(spec, engine.run_id, events)
    return {s.name: s for s in result.stages}, result.verdict


def retouch(events, kind, **detail):
    """Rewrite the first event of a kind, as a tampered bundle would."""
    for i, e in enumerate(events):
        if e.kind == kind:
            events[i] = e.model_copy(update={"detail": {**e.detail, **detail}})
            return events
    raise AssertionError(f"no {kind} event")


def test_dutch_auction_holds_a_delayed_accept_to_its_price():
    stages, verdict = judged("dutch_auction")
    assert verdict == "passed"
    assert all(stages[n].status == "passed" for n in DUTCH_STAGES)
    assert "sold at 1800" in stages["acceptance"].note
    assert "4 lower prices" in stages["delayed_accept"].note


def test_naive_control_fails_for_the_predicted_reason():
    stages, verdict = judged("dutch_auction_naive")
    assert verdict == "failed"
    assert stages["clock"].status == "passed"
    assert stages["delayed_accept"].status == "passed"
    assert stages["acceptance"].status == "failed"
    assert "sale price 1600 differs" in stages["acceptance"].note
    assert stages["payment"].status == "failed"
    assert "accepted total is 3600" in stages["payment"].note
    # Money is still conserved: only the new stages see the repricing.
    assert stages["ledger_conserved"].status == "passed"


def test_without_the_fault_the_delay_stage_claims_nothing():
    def no_faults(spec):
        return spec.model_copy(update={"faults": []})
    for name in ("dutch_auction", "dutch_auction_naive"):
        stages, _ = judged(name, spec_edit=no_faults)
        assert stages["acceptance"].status == "passed"
        assert stages["delayed_accept"].status == "not_enough_evidence"


def test_a_sale_at_another_price_fails_acceptance():
    stages, _ = judged("dutch_auction",
                       lambda ev: retouch(ev, "offer_accepted", cents=1600))
    assert stages["acceptance"].status == "failed"


def test_an_accept_naming_an_unposted_price_fails_acceptance():
    def unposted(events):
        for i, e in enumerate(events):
            if e.kind == "message_sent" and e.detail.get("kind") == \
                    "dutch_accept":
                body = {**e.detail["body"], "cents": 1825}
                events[i] = e.model_copy(
                    update={"detail": {**e.detail, "body": body}})
        return events
    stages, _ = judged("dutch_auction", unposted)
    assert stages["acceptance"].status == "failed"
    assert "never posted" in stages["acceptance"].note


def test_a_second_payment_fails_the_payment_stage():
    def pay_twice(events):
        pay = next(e for e in events if e.kind == "payment_settled")
        return events + [pay.model_copy(update={"event_id": "ev-999"})]
    stages, _ = judged("dutch_auction", pay_twice)
    assert stages["payment"].status == "failed"


def test_an_off_schedule_price_fails_the_clock():
    stages, _ = judged("dutch_auction",
                       lambda ev: retouch(ev, "price_posted", cents=2100))
    assert stages["clock"].status == "failed"


def test_both_dutch_bundles_verify(tmp_path):
    for name in ("dutch_auction", "dutch_auction_naive"):
        bundle, _ = run_lab(name, str(tmp_path))
        assert verify_bundle(bundle) == []


# -- rising.v1: an accept is an immediate-or-cancel limit order --------


def rising(plugin="rising.v1"):
    engine = RecordingEngine()
    neg = resolve("negotiation", plugin)(engine)
    nid = neg.open("seller", "widget", 1500, 50, 1700)
    return engine, neg, nid


def test_rising_clock_climbs_one_step_per_tick_and_stops_at_the_cap():
    engine, neg, nid = rising()
    posted = []
    while (p := neg.post(nid, "seller")) is not None:
        posted.append(p)
    assert posted == [(1, 1500), (2, 1550), (3, 1600), (4, 1650), (5, 1700)]
    assert len(engine.kinds("clock_ended")) == 1
    [(_, _, _, opened)] = engine.kinds("negotiation_started")
    assert opened["cap_cents"] == 1700 and "floor_cents" not in opened


def test_an_accept_fills_at_the_current_price_when_within_its_limit():
    _, neg, nid = rising()
    neg.post(nid, "seller")
    assert neg.accept(nid, "buyer", 1, 1500) == 1500


def test_a_stale_limit_below_the_current_price_is_refused():
    engine, neg, nid = rising()
    for _ in range(4):
        neg.post(nid, "seller")          # the price is now 1650
    assert neg.accept(nid, "buyer", 1, 1500) is None
    [(_, _, _, detail)] = engine.kinds("accept_rejected")
    assert detail["reason"] == "price moved above the limit"
    assert engine.kinds("offer_accepted") == []


def test_stale_twin_sells_the_old_bargain():
    _, neg, nid = rising("rising.stale.v1")
    for _ in range(4):
        neg.post(nid, "seller")
    assert neg.accept(nid, "buyer", 1, 1500) == 1500


def test_a_rising_accept_must_still_name_a_posted_price():
    engine, neg, nid = rising()
    neg.post(nid, "seller")
    assert neg.accept(nid, "buyer", 1, 1400) is None
    [(_, _, _, detail)] = engine.kinds("accept_rejected")
    assert detail["reason"] == "not a posted price"


@pytest.mark.parametrize("start, step, cap", [
    (1500, 0, 2000), (2000, 50, 1500), (-1, 50, 2000),
])
def test_open_refuses_a_malformed_rising_clock(start, step, cap):
    neg = resolve("negotiation", "rising.v1")(RecordingEngine())
    with pytest.raises(NegotiationError):
        neg.open("seller", "widget", start, step, cap)


RISING_STAGES = ["clock", "limit_respected", "paid_as_filled",
                 "delayed_accept"]


def test_rising_clock_refuses_the_delayed_stale_limit():
    stages, verdict = judged("rising_clock")
    assert verdict == "passed"
    assert all(stages[n].status == "passed" for n in RISING_STAGES)
    assert "moved to 1700, above the buyer's limit 1500" in \
        stages["limit_respected"].note
    assert stages["paid_as_filled"].note == \
        "the accept was refused, so nothing was paid"


def test_stale_control_fails_for_the_predicted_reason():
    stages, verdict = judged("rising_clock_stale")
    assert verdict == "failed"
    assert "sold at 1500 while the seller's current price was 1700" in \
        stages["limit_respected"].note
    assert stages["paid_as_filled"].status == "failed"
    assert stages["ledger_conserved"].status == "passed"


def test_without_the_delay_the_limit_fills_at_the_current_price():
    def no_faults(spec):
        return spec.model_copy(update={"faults": []})
    stages, _ = judged("rising_clock", spec_edit=no_faults)
    assert "filled at the current price 1500" in \
        stages["limit_respected"].note
    assert "one payment of 3000" in stages["paid_as_filled"].note
    assert stages["delayed_accept"].status == "not_enough_evidence"


def test_a_refusal_while_the_price_was_within_the_limit_fails():
    """A seller cannot refuse a valid limit by claiming the price moved."""
    def early_refusal(events):
        # move the refusal to straight after the accept was sent,
        # before any higher price was posted
        i = next(k for k, e in enumerate(events) if e.kind == "accept_rejected")
        refusal = events.pop(i)
        j = next(k for k, e in enumerate(events) if e.kind == "message_sent"
                 and e.detail.get("kind") == "dutch_accept")
        events.insert(j + 1, refusal)
        return events
    stages, _ = judged("rising_clock", early_refusal)
    assert stages["limit_respected"].status == "failed"
    assert "within the limit" in stages["limit_respected"].note


def test_both_rising_bundles_verify(tmp_path):
    for name in ("rising_clock", "rising_clock_stale"):
        bundle, _ = run_lab(name, str(tmp_path))
        assert verify_bundle(bundle) == []
