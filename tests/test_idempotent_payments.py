"""A duplicated payment request settles at most once.

ledger.idempotent.v1 treats a transfer's (payer, memo) as the identity of
one logical payment. These tests pin the defect it answers (a duplicated
auction award charges the winner twice under ledger.v1), the negative
control that keeps that defect visible, and the stages that judge the
duplicated-award scenario from its recorded events.
"""

import importlib.resources
from pathlib import Path

import pytest
import yaml

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.layers import DEFAULT_PLUGINS, resolve
from nandatown.layers.payments import Ledger, PaymentError
from nandatown.sim.engine import Engine
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.scenario import (
    FaultRule,
    ScenarioSpec,
    load_bundled,
    load_scenario_file,
)
from nandatown.sim.validators import evaluate_scenario

POSITIVE = "auction_duplicate_award"
CONTROL = "auction_duplicate_award_no_idempotency"
PLUGIN = "ledger.idempotent.v1"

AWARD_NOTE = "the award to bidder-y was delivered twice under one message identity"
REPLAY_NOTE = ("the second delivery replayed the original settlement;"
               " no second payment settled")
POSITIVE_STAGES = [
    ("announced", "passed", ""),
    ("bidding", "passed", ""),
    ("award", "passed", ""),
    ("settlement", "passed",
     "one payment to the auctioneer for this task's recorded award"),
    ("delivery", "passed", ""),
    ("duplicate_award_delivered", "passed", AWARD_NOTE),
    ("duplicate_payment_replayed", "passed", REPLAY_NOTE),
    ("ledger_conserved", "passed", "6000 cents conserved across every movement"),
    ("privacy", "not_tested", "no redaction declared by this scenario"),
]
CONTROL_STAGES = [
    ("announced", "passed", ""),
    ("bidding", "passed", ""),
    ("award", "passed", ""),
    ("settlement", "failed", "expected exactly one payment_settled record"),
    ("delivery", "passed", ""),
    ("duplicate_award_delivered", "passed", AWARD_NOTE),
    ("duplicate_payment_replayed", "failed",
     "the duplicated award settled 2 payments under one payment identity"),
    ("ledger_conserved", "passed", "6000 cents conserved across every movement"),
    ("privacy", "not_tested", "no redaction declared by this scenario"),
]


def stage_table(result):
    return [(s.name, s.status, s.note) for s in result.stages]


def of_kind(events, kind):
    return [e for e in events if e.kind == kind]


def ledger_engine(payments=PLUGIN):
    """A real engine whose payments layer is the named plugin, built the
    way every Lab run builds its layers."""
    spec = ScenarioSpec(name="payments-unit", agents=[],
                        layers={"payments": payments})
    engine = Engine(spec)
    ledger = engine.layers["payments"]
    for name in ("a", "b", "c"):
        ledger.open_account(name, 1000 if name == "a" else 0)
    return engine, ledger


def run_in_memory(spec):
    engine = build_engine(spec)
    engine.run()
    return engine


def scenario_yaml(name):
    path = importlib.resources.files("nandatown.sim") / "scenarios" / f"{name}.yaml"
    return yaml.safe_load(path.read_text())


# -- the plugin surface -------------------------------------------------


def test_plugin_is_an_opt_in_ledger():
    assert issubclass(resolve("payments", PLUGIN), Ledger)
    assert DEFAULT_PLUGINS["payments"] == "ledger.v1"
    assert resolve("payments", "ledger.v1") is Ledger


def test_ledger_v1_settles_an_exact_repeat_twice():
    """Characterisation: the default ledger has no payment identity."""
    engine, ledger = ledger_engine("ledger.v1")
    ledger.transfer("a", "b", 100, "X")
    ledger.transfer("a", "b", 100, "X")
    assert len(of_kind(engine.events, "payment_settled")) == 2
    assert ledger.balances == {"a": 800, "b": 200, "c": 0}


def test_exact_repeat_is_replayed():
    engine, ledger = ledger_engine()
    ledger.transfer("a", "b", 100, "X")
    ledger.transfer("a", "b", 100, "X")
    settled = of_kind(engine.events, "payment_settled")
    assert len(settled) == 1
    replayed = of_kind(engine.events, "payment_replay_ignored")
    assert [(e.observer, e.subject, e.detail) for e in replayed] == [
        ("town", "X", {"from": "a", "to": "b", "cents": 100,
                       "settlement": settled[0].event_id})]
    assert ledger.balances == {"a": 900, "b": 100, "c": 0}


def test_conflicting_repeat_is_rejected():
    engine, ledger = ledger_engine()
    ledger.transfer("a", "b", 100, "X")
    ledger.transfer("a", "c", 100, "X")
    settled = of_kind(engine.events, "payment_settled")
    assert len(settled) == 1
    rejected = of_kind(engine.events, "payment_reuse_rejected")
    assert [(e.observer, e.subject, e.detail) for e in rejected] == [
        ("town", "X", {"from": "a", "to": "c", "cents": 100,
                       "settlement": settled[0].event_id})]
    assert of_kind(engine.events, "payment_replay_ignored") == []
    assert ledger.balances == {"a": 900, "b": 100, "c": 0}


def payment_events(engine):
    return [(e.kind, e.observer, e.subject, e.detail) for e in engine.events
            if e.kind.startswith("payment_")]


def test_state_is_per_engine():
    """Two runs in one process never share payment identities."""
    for _ in range(2):
        engine, ledger = ledger_engine()
        ledger.transfer("a", "b", 100, "X")
        assert [kind for kind, *_ in payment_events(engine)] == [
            "payment_settled"]


@pytest.mark.parametrize("cents", [0, -1, True, 1.0, "x"])
def test_invalid_cents_raise_and_name_no_payment(cents):
    engine, ledger = ledger_engine()
    before = list(engine.events)
    with pytest.raises(PaymentError):
        ledger.transfer("a", "b", cents, "X")
    assert engine.events == before
    ledger.transfer("a", "b", 100, "X")
    with pytest.raises(PaymentError):
        ledger.transfer("a", "b", cents, "X")
    assert [kind for kind, *_ in payment_events(engine)] == ["payment_settled"]


def test_failed_first_payment_leaves_the_payment_unsettled():
    engine, ledger = ledger_engine()
    ledger.open_account("p", 50)
    with pytest.raises(PaymentError):
        ledger.transfer("p", "b", 100, "X")
    ledger.transfer("a", "p", 50, "top-up")
    ledger.transfer("p", "b", 100, "X")
    assert [(kind, subject) for kind, _, subject, _ in payment_events(engine)] \
        == [("payment_rejected", "p"), ("payment_settled", "top-up"),
            ("payment_settled", "X")]
    assert ledger.balances == {"a": 950, "b": 100, "c": 0, "p": 0}


@pytest.mark.parametrize("repeats", [1, 2, 3, 5, 10])
def test_repeats_settle_once(repeats):
    engine, ledger = ledger_engine()
    for _ in range(repeats):
        ledger.transfer("a", "b", 100, "X")
    kinds = [kind for kind, *_ in payment_events(engine)]
    assert kinds == ["payment_settled"] + ["payment_replay_ignored"] * (repeats - 1)
    assert ledger.balances == {"a": 900, "b": 100, "c": 0}


@pytest.mark.parametrize("to,cents", [("b", 200), ("c", 100)])
def test_conflicting_terms_are_refused_and_the_original_stands(to, cents):
    engine, ledger = ledger_engine()
    ledger.transfer("a", "b", 100, "X")
    ledger.transfer("a", to, cents, "X")
    ledger.transfer("a", "b", 100, "X")
    settlement = engine.events[-3].event_id
    assert payment_events(engine)[1:] == [
        ("payment_reuse_rejected", "town", "X",
         {"from": "a", "to": to, "cents": cents, "settlement": settlement}),
        ("payment_replay_ignored", "town", "X",
         {"from": "a", "to": "b", "cents": 100, "settlement": settlement})]
    assert ledger.balances == {"a": 900, "b": 100, "c": 0}


def test_other_payers_may_use_the_same_memo():
    engine, ledger = ledger_engine()
    ledger.transfer("a", "b", 100, "X")
    ledger.transfer("b", "c", 100, "X")
    assert [kind for kind, *_ in payment_events(engine)] == [
        "payment_settled", "payment_settled"]


def test_empty_memo_names_no_payment():
    engine, ledger = ledger_engine()
    ledger.transfer("a", "b", 100, "")
    ledger.transfer("a", "b", 100, "")
    assert [kind for kind, *_ in payment_events(engine)] == [
        "payment_settled", "payment_settled"]
    assert ledger.balances == {"a": 800, "b": 200, "c": 0}


def test_non_string_memo_keeps_ledger_v1_behaviour():
    """Characterisation only: this ledger.v1 behaviour is out of scope."""
    outcomes = []
    for payments in ("ledger.v1", PLUGIN):
        engine, ledger = ledger_engine(payments)
        with pytest.raises(Exception) as raised:
            ledger.transfer("a", "b", 10, 7)
        outcomes.append((type(raised.value), dict(ledger.balances),
                         payment_events(engine)))
    assert outcomes[0] == outcomes[1]


def test_escrow_is_inherited_unchanged():
    for method in ("open_account", "balance", "total", "hold", "release",
                   "refund"):
        assert method not in vars(resolve("payments", PLUGIN)), method


def differential(payments):
    """The same operations on a fresh ledger; no successful payment repeats."""
    engine, ledger = ledger_engine(payments)
    outcomes = []
    for operation, *args in [
            ("transfer", "a", "b", 100, "order-1"),
            ("transfer", "a", "a", 5, "self"),
            ("transfer", "a", "stranger", 5, "ü-1"),
            ("transfer", "a", "b", 1, ""),
            ("transfer", "a", "b", 1, ""),
            ("transfer", "c", "b", 1, "broke"),
            ("transfer", "b", "c", 1, "broke"),
            ("transfer", "a", "b", 0, "zero"),
            ("transfer", "a", "b", True, "bool"),
            ("hold", "a", 300, "escrow-1"),
            ("hold", "a", 300, "escrow-1"),
            ("release", "escrow-1", "b"),
            ("release", "escrow-1", "b"),
            ("hold", "a", 50, "escrow-2"),
            ("refund", "escrow-2"),
            ("transfer", "a", "c", 7, "x" * 200)]:
        try:
            getattr(ledger, operation)(*args)
            outcomes.append(None)
        except PaymentError as exc:
            outcomes.append(str(exc))
    events = [e.model_dump(exclude={"run_id"}) for e in engine.events]
    return events, outcomes, ledger.balances, ledger.escrow, ledger.total()


def test_first_payments_match_ledger_v1():
    assert differential(PLUGIN) == differential("ledger.v1")


def normalised(engine):
    events = []
    for event in engine.events:
        data = event.model_dump(exclude={"run_id"})
        if data["subject"] == engine.run_id:
            data["subject"] = "RUN"
        events.append(data)
    return events


EXISTING_SCENARIOS = ["marketplace", "auction", "voting", "consensus",
                      "supply_chain", "capability_spoofing",
                      "capability_spoofing_weak_auth"]
UPSTREAM = sorted(
    (Path(__file__).parent / "fixtures" / "upstream").glob("*.yaml"))


@pytest.mark.parametrize("load", [
    *[lambda name=name: load_bundled(name) for name in EXISTING_SCENARIOS],
    *[lambda path=path: load_scenario_file(str(path)) for path in UPSTREAM],
], ids=[*EXISTING_SCENARIOS, *(path.stem for path in UPSTREAM)])
def test_swapping_in_the_plugin_changes_no_existing_scenario(load):
    runs = []
    for payments in ("ledger.v1", PLUGIN):
        spec = load()
        spec.layers["payments"] = payments
        engine = run_in_memory(spec)
        result = evaluate_scenario(spec, engine.run_id, engine.events)
        runs.append((normalised(engine), engine.layers["payments"].balances,
                     engine.layers["payments"].escrow, stage_table(result),
                     result.verdict))
    assert runs[0] == runs[1]


# -- the duplicated award, end to end -------------------------------------


def test_duplicate_award_settles_once():
    engine = run_in_memory(load_bundled(POSITIVE))
    events = engine.events
    duplicated = [e for e in of_kind(events, "message_duplicated")
                  if e.detail.get("kind") == "auction_won"]
    assert len(duplicated) == 1
    deliveries = [e for e in of_kind(events, "message_delivered")
                  if e.subject == duplicated[0].subject]
    assert [d.detail["to"] for d in deliveries] == ["bidder-y", "bidder-y"]
    attempts = [(i["actor"], i["payload"]) for i in engine.intents
                if i["action"] == "pay"]
    assert attempts == [("bidder-y", {"to": "auctioneer", "cents": 900,
                                      "memo": "auction-print-001"})] * 2
    assert len(of_kind(events, "payment_settled")) == 1
    assert engine.layers["payments"].balances == {
        "auctioneer": 900, "bidder-x": 2000, "bidder-y": 1100,
        "bidder-late": 2000}


def test_positive_scenario_stages_literal():
    spec = load_bundled(POSITIVE)
    engine = run_in_memory(spec)
    result = evaluate_scenario(spec, engine.run_id, engine.events)
    assert stage_table(result) == POSITIVE_STAGES
    assert result.verdict == "passed"


def test_control_fails_exactly_as_predicted(tmp_path):
    bundle_dir, result = run_lab(CONTROL, str(tmp_path))
    assert stage_table(result) == CONTROL_STAGES
    assert result.verdict == "failed"
    assert verify_bundle(bundle_dir) == []
    events = load_bundle(bundle_dir)["events"]
    assert len(of_kind(events, "payment_settled")) == 2
    assert of_kind(events, "payment_replay_ignored") == []


def test_control_differs_only_in_its_payments_plugin():
    positive, control = scenario_yaml(POSITIVE), scenario_yaml(CONTROL)
    assert set(positive) == set(control)
    for key in set(positive) - {"name", "description", "layers"}:
        assert positive[key] == control[key], key
    assert positive["validator"] == control["validator"] == POSITIVE
    assert positive["layers"] == {"payments": PLUGIN}
    assert control["layers"] == {"payments": "ledger.v1"}
    # Every bidder can be charged twice, so the control fails on the
    # duplicated settlement rather than on insufficient funds.
    for agent in control["agents"]:
        if agent["role"] == "bidder":
            config = agent["config"]
            assert config["balance_cents"] >= 2 * config["valuation_cents"]


def test_control_trace_characterisation():
    """The event chain ledger.v1 records for a duplicated award."""
    engine = run_in_memory(load_bundled(CONTROL))
    events = engine.events
    position = {e.event_id: i for i, e in enumerate(events)}
    award, = of_kind(events, "task_awarded")
    sent, = [e for e in of_kind(events, "message_sent")
             if e.detail.get("kind") == "auction_won"]
    duplicated, = of_kind(events, "message_duplicated")
    first, second = [e for e in of_kind(events, "message_delivered")
                     if e.subject == sent.subject]
    settle_1, settle_2 = of_kind(events, "payment_settled")
    item, = [e for e in of_kind(events, "message_delivered")
             if e.detail.get("kind") == "item_delivery"]
    assert (award.observer, award.subject, award.detail["winner"],
            award.detail["cents"]) == ("auctioneer", "auction-print-001",
                                       "bidder-y", 900)
    assert (sent.observer, sent.detail["to"], sent.detail["body"]) == (
        "auctioneer", "bidder-y", {"task_id": "auction-print-001",
                                   "cents": 900})
    assert (duplicated.observer, duplicated.subject) == ("town", sent.subject)
    for settled in (settle_1, settle_2):
        assert (settled.observer, settled.subject, settled.detail) == (
            "town", "auction-print-001",
            {"from": "bidder-y", "to": "auctioneer", "cents": 900})
    assert [position[e.event_id] for e in (
        award, sent, duplicated, first, settle_1, second, settle_2, item)] \
        == sorted(position[e.event_id] for e in (
            award, sent, duplicated, first, settle_1, second, settle_2, item))
    assert (settle_1.at, settle_2.at) == (first.at, second.at)
    assert of_kind(events, "payment_replay_ignored") == []


# -- the stages, judged from recorded events ------------------------------


def control_trace(faults=None):
    spec = load_bundled(CONTROL)
    if faults is not None:
        spec.faults = faults
    engine = run_in_memory(spec)
    return [e.model_copy(deep=True) for e in engine.events]


def as_specified(events):
    """A ledger.v1 trace as the idempotent ledger is specified to record
    it: the second settlement of the award becomes a replay of the first."""
    events = list(events)
    indexes = [i for i, e in enumerate(events) if e.kind == "payment_settled"]
    first = events[indexes[0]]
    events[indexes[1]] = events[indexes[1]].model_copy(update={
        "kind": "payment_replay_ignored",
        "detail": {**first.detail, "settlement": first.event_id}})
    return events


def judge(events):
    spec = load_bundled(POSITIVE)
    result = evaluate_scenario(spec, events[0].run_id, events)
    stages = {s.name: (s.status, s.note) for s in result.stages}
    return result.verdict, stages


def index_of(events, event_kind, nth=0, **detail):
    matches = [i for i, e in enumerate(events) if e.kind == event_kind
               and all(e.detail.get(k) == v for k, v in detail.items())]
    return matches[nth]


def award_delivery(events, nth):
    sent = events[index_of(events, "message_sent", kind="auction_won")]
    matches = [i for i, e in enumerate(events)
               if e.kind == "message_delivered" and e.subject == sent.subject]
    return matches[nth]


def move(events, source, before):
    events = list(events)
    event = events.pop(source)
    events.insert(before if before < source else before - 1, event)
    return events


def test_v1_specified_chain_passes():
    verdict, stages = judge(as_specified(control_trace()))
    assert stages["duplicate_award_delivered"] == ("passed", AWARD_NOTE)
    assert stages["duplicate_payment_replayed"] == ("passed", REPLAY_NOTE)
    assert verdict == "passed"


def test_v2_no_duplicate_evidence_is_incomplete():
    events = [e for e in as_specified(control_trace())
              if e.kind != "message_duplicated"]
    verdict, stages = judge(events)
    assert stages["duplicate_award_delivered"] == (
        "not_enough_evidence",
        "no duplicated auction_won delivery to the recorded winner")
    assert stages["duplicate_payment_replayed"] == (
        "not_enough_evidence", "the duplicated award delivery is not in evidence")
    assert verdict == "incomplete"


def test_v3_forged_replay_without_the_fault_is_incomplete():
    engine = run_in_memory(load_bundled("auction"))
    events = [e.model_copy(deep=True) for e in engine.events]
    settled = index_of(events, "payment_settled")
    events.insert(settled + 1, events[settled].model_copy(update={
        "event_id": "forged-replay", "kind": "payment_replay_ignored",
        "detail": {**events[settled].detail,
                   "settlement": events[settled].event_id}}))
    verdict, stages = judge(events)
    assert stages["duplicate_award_delivered"][0] == "not_enough_evidence"
    assert stages["duplicate_payment_replayed"][0] == "not_enough_evidence"
    assert verdict == "incomplete"


def test_v4_two_settlements_fail():
    verdict, stages = judge(control_trace())
    assert stages["duplicate_payment_replayed"] == (
        "failed",
        "the duplicated award settled 2 payments under one payment identity")
    assert verdict == "failed"


MISMATCH = ("the replayed payment does not match the original settlement"
            " and the second delivery")
SETTLEMENT_MISMATCH = ("the settlement does not match the award and its"
                       " first delivery")


def test_v5_replay_before_the_second_delivery_fails():
    events = as_specified(control_trace())
    events = move(events, index_of(events, "payment_replay_ignored"),
                  award_delivery(events, 1))
    assert judge(events)[1]["duplicate_payment_replayed"] == ("failed", MISMATCH)


def test_v6_wrong_settlement_pointer_fails():
    events = as_specified(control_trace())
    replay = events[index_of(events, "payment_replay_ignored")]
    replay.detail["settlement"] = events[index_of(events, "task_awarded")].event_id
    assert judge(events)[1]["duplicate_payment_replayed"] == ("failed", MISMATCH)


def test_v7_replay_observed_by_a_participant_does_not_count():
    events = as_specified(control_trace())
    events[index_of(events, "payment_replay_ignored")].observer = "bidder-y"
    verdict, stages = judge(events)
    assert stages["duplicate_payment_replayed"] == (
        "not_enough_evidence",
        "no replayed payment is recorded for the second delivery")
    assert verdict == "incomplete"


def test_v7_duplicate_observed_by_a_participant_does_not_count():
    events = as_specified(control_trace())
    events[index_of(events, "message_duplicated")].observer = "auctioneer"
    verdict, stages = judge(events)
    assert stages["duplicate_award_delivered"][0] == "not_enough_evidence"
    assert verdict == "incomplete"


def test_v8_settlement_after_the_second_delivery_fails():
    events = as_specified(control_trace())
    settled = index_of(events, "payment_settled")
    events = move(events, settled, award_delivery(events, 1) + 1)
    assert judge(events)[1]["duplicate_payment_replayed"] == (
        "failed", SETTLEMENT_MISMATCH)


def test_v9_wrong_payee_fails():
    events = as_specified(control_trace())
    events[index_of(events, "payment_settled")].detail["to"] = "bidder-x"
    assert judge(events)[1]["duplicate_payment_replayed"] == (
        "failed", SETTLEMENT_MISMATCH)


@pytest.mark.parametrize("kind,want", [
    ("payment_replay_ignored", MISMATCH),
    ("payment_settled", SETTLEMENT_MISMATCH),
])
def test_v10_wrong_amount_fails(kind, want):
    events = as_specified(control_trace())
    events[index_of(events, kind)].detail["cents"] = 901
    assert judge(events)[1]["duplicate_payment_replayed"] == ("failed", want)


def test_v11_wrong_fault_kind_is_incomplete():
    events = control_trace([FaultRule(action="duplicate", kind="auction_result",
                                      nth=1)])
    verdict, stages = judge(events)
    assert stages["duplicate_award_delivered"] == (
        "not_enough_evidence",
        "no duplicated auction_won delivery to the recorded winner")
    assert verdict == "incomplete"


def test_v12_equivalent_unkinded_fault_is_judged_by_its_effect():
    """An empty kind matches every message; its sixth is the award."""
    events = control_trace([FaultRule(action="duplicate", kind="", nth=6)])
    assert judge(events)[1]["duplicate_award_delivered"] == ("passed", AWARD_NOTE)
    verdict, stages = judge(as_specified(events))
    assert stages["duplicate_payment_replayed"] == ("passed", REPLAY_NOTE)
    assert verdict == "passed"


def test_second_replay_fails():
    events = as_specified(control_trace())
    replay = index_of(events, "payment_replay_ignored")
    events.insert(replay + 1, events[replay].model_copy(
        update={"event_id": "second-replay"}))
    assert judge(events)[1]["duplicate_payment_replayed"] == (
        "failed", "exactly one replayed payment is required")


def test_reuse_rejection_under_the_award_identity_fails():
    events = as_specified(control_trace())
    replay = index_of(events, "payment_replay_ignored")
    events.insert(replay + 1, events[replay].model_copy(update={
        "event_id": "reuse", "kind": "payment_reuse_rejected"}))
    assert judge(events)[1]["duplicate_payment_replayed"] == (
        "failed", "a payment under the award's identity was rejected for"
                  " changed terms")


def test_second_settlement_under_another_subject_still_counts():
    events = control_trace()
    events[index_of(events, "payment_settled", nth=1)].subject = "AUCTION-print-001"
    assert judge(events)[1]["duplicate_payment_replayed"][0] == "failed"


def test_replay_at_another_logical_time_fails():
    events = as_specified(control_trace())
    events[index_of(events, "payment_replay_ignored")].at += 0.05
    assert judge(events)[1]["duplicate_payment_replayed"] == ("failed", MISMATCH)


def test_missing_second_delivery_is_incomplete():
    events = as_specified(control_trace())
    del events[award_delivery(events, 1)]
    verdict, stages = judge(events)
    assert stages["duplicate_award_delivered"][0] == "not_enough_evidence"
    assert verdict == "incomplete"


def test_missing_award_is_incomplete():
    events = [e for e in as_specified(control_trace())
              if e.kind != "task_awarded"]
    verdict, stages = judge(events)
    assert stages["duplicate_award_delivered"] == (
        "not_enough_evidence", "no single recorded award to duplicate")
    assert verdict != "passed"


def test_duplicated_award_for_another_task_fails():
    events = as_specified(control_trace())
    sent = events[index_of(events, "message_sent", kind="auction_won")]
    sent.detail["body"]["task_id"] = "auction-other"
    assert judge(events)[1]["duplicate_award_delivered"] == (
        "failed", "the duplicated auction_won does not match the recorded award")


def test_a_third_award_delivery_fails():
    events = as_specified(control_trace())
    second = award_delivery(events, 1)
    events.insert(second + 1, events[second].model_copy(
        update={"event_id": "third-delivery"}))
    verdict, stages = judge(events)
    assert stages["duplicate_award_delivered"][0] == "failed"
    assert verdict == "failed"


def test_missing_settlement_is_incomplete():
    events = as_specified(control_trace())
    del events[index_of(events, "payment_settled")]
    verdict, stages = judge(events)
    assert stages["duplicate_payment_replayed"] == (
        "not_enough_evidence", "the award payment never settled")
    assert verdict != "passed"


def test_settlement_at_another_logical_time_fails():
    """The settlement must happen at the first delivery, not merely after it."""
    events = as_specified(control_trace())
    settlement = events[index_of(events, "payment_settled")]
    settlement.at += 0.05
    verdict, stages = judge(events)
    assert stages["duplicate_payment_replayed"] == ("failed", SETTLEMENT_MISMATCH)
    assert verdict != "passed"


# -- the funding boundary: winning amount 900 ---------------------------------


def with_winner_balance(name, balance):
    spec = load_bundled(name)
    spec.agents = [agent.model_copy(update={
        "config": {**agent.config, "balance_cents": balance}})
        if agent.name == "bidder-y" else agent for agent in spec.agents]
    return spec


@pytest.mark.parametrize("balance,remaining", [
    (2000, 1100), (1800, 900), (1799, 899), (900, 0)])
def test_positive_settles_once_whenever_one_charge_is_affordable(
        balance, remaining):
    spec = with_winner_balance(POSITIVE, balance)
    engine = run_in_memory(spec)
    result = evaluate_scenario(spec, engine.run_id, engine.events)
    assert result.verdict == "passed"
    assert len(of_kind(engine.events, "payment_settled")) == 1
    assert len(of_kind(engine.events, "payment_replay_ignored")) == 1
    assert engine.layers["payments"].balances["bidder-y"] == remaining


def test_positive_below_one_charge_keeps_ledger_v1_insufficient_funds():
    with pytest.raises(PaymentError, match="bidder-y lacks 900"):
        run_in_memory(with_winner_balance(POSITIVE, 899))


@pytest.mark.parametrize("balance,remaining", [(2000, 200), (1800, 0)])
def test_control_fails_on_the_invariant_while_two_charges_are_affordable(
        balance, remaining):
    spec = with_winner_balance(CONTROL, balance)
    engine = run_in_memory(spec)
    result = evaluate_scenario(spec, engine.run_id, engine.events)
    opened = 4000 + balance  # bidder-x and bidder-late hold 2000 each
    assert stage_table(result) == [
        (name, status, f"{opened} cents conserved across every movement")
        if name == "ledger_conserved" else (name, status, note)
        for name, status, note in CONTROL_STAGES]
    assert len(of_kind(engine.events, "payment_settled")) == 2
    assert engine.layers["payments"].balances == {
        "auctioneer": 1800, "bidder-x": 2000, "bidder-y": remaining,
        "bidder-late": 2000}


@pytest.mark.parametrize("balance", [1799, 900, 899])
def test_control_below_two_charges_raises(balance):
    """Characterisation only: ledger.v1 refuses an unaffordable second
    charge by raising, which would end the run without a bundle. This is
    why the shipped control must stay funded for two charges."""
    with pytest.raises(PaymentError, match="bidder-y lacks 900"):
        run_in_memory(with_winner_balance(CONTROL, balance))


def test_shipped_control_is_funded_for_two_charges_of_its_award():
    spec = load_bundled(CONTROL)
    engine = run_in_memory(spec)
    award, = of_kind(engine.events, "task_awarded")
    winner, cents = award.detail["winner"], award.detail["cents"]
    funded = next(a.config["balance_cents"] for a in spec.agents
                  if a.name == winner)
    assert (winner, cents) == ("bidder-y", 900)
    assert funded >= 2 * cents
    result = evaluate_scenario(spec, engine.run_id, engine.events)
    failed = {s.name for s in result.stages if s.status == "failed"}
    assert failed == {"settlement", "duplicate_payment_replayed"}


# -- the award chain the fault stage rests on ------------------------------

AWARD_MISMATCH = "the duplicated auction_won does not match the recorded award"


def award_send(events):
    return index_of(events, "message_sent", kind="auction_won")


def test_award_sent_by_someone_other_than_the_issuer_fails():
    events = as_specified(control_trace())
    events[award_send(events)].observer = "bidder-x"
    assert judge(events)[1]["duplicate_award_delivered"] == (
        "failed", AWARD_MISMATCH)


def test_award_sent_to_someone_other_than_the_winner_fails():
    events = as_specified(control_trace())
    events[award_send(events)].detail["to"] = "bidder-x"
    assert judge(events)[1]["duplicate_award_delivered"] == (
        "failed", AWARD_MISMATCH)


def test_award_delivered_to_someone_else_is_incomplete():
    events = as_specified(control_trace())
    events[award_delivery(events, 1)].detail["to"] = "bidder-x"
    verdict, stages = judge(events)
    assert stages["duplicate_award_delivered"] == (
        "not_enough_evidence", NO_DUPLICATE_NOTE)
    assert verdict != "passed"


def test_duplicate_recorded_before_the_award_was_sent_fails():
    events = as_specified(control_trace())
    events = move(events, index_of(events, "message_duplicated"),
                  award_send(events))
    assert judge(events)[1]["duplicate_award_delivered"] == (
        "failed", AWARD_MISMATCH)


def test_award_sent_after_its_duplicate_in_logical_time_fails():
    events = as_specified(control_trace())
    events[award_send(events)].at += 0.05
    assert judge(events)[1]["duplicate_award_delivered"] == (
        "failed", AWARD_MISMATCH)


def test_second_record_of_the_award_send_fails():
    events = as_specified(control_trace())
    sent = award_send(events)
    events.insert(sent + 1, events[sent].model_copy(
        update={"event_id": "second-send"}))
    assert judge(events)[1]["duplicate_award_delivered"] == (
        "failed", AWARD_MISMATCH)


def test_second_record_of_the_duplication_fails():
    events = as_specified(control_trace())
    duplicated = index_of(events, "message_duplicated")
    events.insert(duplicated + 1, events[duplicated].model_copy(
        update={"event_id": "second-duplication"}))
    assert judge(events)[1]["duplicate_award_delivered"] == (
        "failed", AWARD_MISMATCH)


def test_duplicated_award_without_its_send_record_fails():
    events = as_specified(control_trace())
    del events[award_send(events)]
    assert judge(events)[1]["duplicate_award_delivered"] == (
        "failed", AWARD_MISMATCH)


NO_DUPLICATE_NOTE = "no duplicated auction_won delivery to the recorded winner"


# -- the settlement pointer guard ------------------------------------------


def test_a_settlement_that_is_not_the_last_event_is_never_recorded(monkeypatch):
    """Ledger.transfer always ends with its payment_settled; if it ever did
    not, the plugin refuses to point a later replay at the wrong event."""
    original = Ledger.transfer

    def transfer_then_audit(self, frm, to, cents, memo):
        original(self, frm, to, cents, memo)
        self.engine.emit("town", "ledger_audited", memo, {})

    monkeypatch.setattr(Ledger, "transfer", transfer_then_audit)
    engine, ledger = ledger_engine()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="did not record its settlement"):
            ledger.transfer("a", "b", 100, "X")
    assert ("a", "X") not in ledger.settled
    assert [kind for kind, *_ in payment_events(engine)] == [
        "payment_settled", "payment_settled"]
    assert ledger.balances == {"a": 800, "b": 200, "c": 0}


# -- the report a reader sees ------------------------------------------------


def test_report_explains_the_duplicated_award_stages(tmp_path):
    from nandatown.report import render_report

    bundle_dir, _ = run_lab(POSITIVE, str(tmp_path))
    report = render_report(load_bundle(bundle_dir))
    for stage in ("duplicate_award_delivered", "duplicate_payment_replayed"):
        line = next(line for line in report.splitlines()
                    if line.strip().startswith(stage))
        assert line.split("Passed", 1)[1].split("[", 1)[0].strip(), line
