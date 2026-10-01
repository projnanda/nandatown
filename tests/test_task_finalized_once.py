"""task_finalized_once: a duplicated consign reaches the auctioneer's handler
twice, yet the intended auction task is announced once and awarded at most
once.

Transport emits message_delivered before authentication and dispatch, so
deliveries alone cannot prove a handler ran; only the auctioneer's
consign_received can. The stage reads scenario configuration and recorded
events only, never the Coordination plugin.
"""

import copy

import pytest

from nandatown.records import TownEvent
from nandatown.sim import validators
from nandatown.sim.agents import Consignor
from nandatown.sim.runner import build_engine
from nandatown.sim.scenario import ScenarioSpec, load_bundled
from nandatown.sim.validators import Trace, auction

RUN = "run-a"
ITEM = "print-001"
TASK = f"auction-{ITEM}"
MSG = "msg-consign"
VALIDATOR = "auction_duplicate_consign"
ANNOUNCED = {"spec": {"item": ITEM}, "rule": "highest"}
AWARDED = {"winner": "bidder-y", "cents": 900, "rule": "highest"}


DUPLICATE_CONSIGN = {"action": "duplicate", "kind": "consign"}


def scenario(coordination="contractnet.once.v1", open_on="consign",
             faults=(DUPLICATE_CONSIGN,)):
    auctioneer = {"item": ITEM, "close_after": 3.0, "balance_cents": 0}
    if open_on is not None:
        auctioneer["open_on"] = open_on
    return ScenarioSpec.model_validate({
        "name": VALIDATOR,
        "layers": {"coordination": coordination},
        "agents": [
            {"name": "auctioneer", "role": "auctioneer",
             "config": auctioneer},
            {"name": "consignor", "role": "consignor",
             "config": {"item": ITEM}},
            {"name": "bidder-x", "role": "bidder",
             "config": {"valuation_cents": 700, "bid_delay": 1.0,
                        "balance_cents": 2000}},
            {"name": "bidder-y", "role": "bidder",
             "config": {"valuation_cents": 900, "bid_delay": 1.2,
                        "balance_cents": 2000}},
            {"name": "bidder-late", "role": "bidder",
             "config": {"valuation_cents": 950, "bid_delay": 6.0,
                        "balance_cents": 2000}},
        ],
        "faults": list(faults),
        "max_time": 60,
    })


def record(i, who, kind, subject, detail):
    return TownEvent(event_id=f"event-{i}", run_id=RUN, at=float(i),
                     observer=who, kind=kind, subject=subject,
                     detail=copy.deepcopy(detail))


def controlled_duplicate(*extra):
    """Hand-written records, independent of the simulator and evaluator."""
    arrival = {"to": "auctioneer", "kind": "consign"}
    receipt = {"from": "consignor", "item": ITEM}
    rows = [
        ("consignor", "message_sent", MSG,
         {"to": "auctioneer", "kind": "consign", "conversation": None,
          "body": {"item": ITEM}}),
        ("town", "message_duplicated", MSG,
         {"to": "auctioneer", "kind": "consign", "fault": "duplicate"}),
        ("town", "message_delivered", MSG, arrival),
        ("auctioneer", "consign_received", MSG, receipt),
        ("auctioneer", "task_announced", TASK, ANNOUNCED),
        ("town", "message_delivered", MSG, arrival),
        ("auctioneer", "consign_received", MSG, receipt),
        ("auctioneer", "task_awarded", TASK, AWARDED),
        *extra,
    ]
    return [record(i, *row) for i, row in enumerate(rows)]


def stage(events, spec=None):
    return validators.task_finalized_once(spec or scenario(),
                                          Trace(events, RUN))


def test_v1_transport_delivery_alone_cannot_pass():
    events = [e for e in controlled_duplicate()
              if e.kind != "consign_received"]
    assert [e.kind for e in events].count("message_delivered") == 2

    assert stage(events).status == "not_enough_evidence"


def test_v2_controlled_duplicate_reaching_the_handler_twice_passes():
    result = stage(controlled_duplicate())

    assert result.status == "passed", result.note
    assert set(result.evidence) == {f"event-{i}" for i in range(8)}


def test_v3_second_target_announcement_fails_with_counts():
    result = stage(controlled_duplicate(
        ("auctioneer", "task_announced", TASK, ANNOUNCED)))

    assert result.status == "failed"
    assert "announced=2" in result.note and "awarded=1" in result.note
    assert "event-8" in result.evidence


def test_v4_second_target_award_fails_with_counts():
    result = stage(controlled_duplicate(
        ("auctioneer", "task_awarded", TASK, AWARDED)))

    assert result.status == "failed"
    assert "announced=1" in result.note and "awarded=2" in result.note
    assert "event-8" in result.evidence


@pytest.mark.parametrize("index, field, value", [
    (0, "observer", "intruder"),             # wrong sender
    (0, "to", "other-auctioneer"),           # wrong recipient
    (0, "body", {"item": "other-item"}),     # wrong item
    (2, "to", "other-auctioneer"),           # delivered to someone else
    (6, "from", "intruder"),                 # receipt names another sender
    (6, "observer", "other-auctioneer"),     # received by someone else
    (6, "item", "other-item"),               # receipt for another item
])
def test_v5_wrong_sender_recipient_or_item_fails(index, field, value):
    events = controlled_duplicate()
    if field == "observer":
        events[index].observer = value
    else:
        events[index].detail[field] = value

    assert stage(events).status == "failed"


@pytest.mark.parametrize("kind", ["message_delivered", "consign_received"])
@pytest.mark.parametrize("field, value", [
    ("subject", "msg-other"),
    ("run_id", "run-b"),
])
def test_v6_records_of_another_message_or_run_do_not_count(kind, field,
                                                           value):
    events = controlled_duplicate()
    for event in events:
        if event.kind == kind:
            setattr(event, field, value)

    assert stage(events).status == "failed"


@pytest.mark.parametrize("kind, detail", [
    ("task_announced", ANNOUNCED),
    ("task_awarded", AWARDED),
])
def test_v7_off_target_task_cannot_be_hidden(kind, detail):
    result = stage(controlled_duplicate(
        ("auctioneer", kind, "auction-other", detail)))

    assert result.status == "failed"
    assert "auction-other" in result.note
    assert "event-8" in result.evidence


@pytest.mark.parametrize("coordination",
                         ["contractnet.v1", "contractnet.once.v1"])
def test_v8_plugin_vocabulary_is_optional(coordination):
    plain = controlled_duplicate()
    assert not ({"announce_replayed", "award_rejected"}
                & {e.kind for e in plain})
    with_vocabulary = controlled_duplicate(
        ("town", "announce_replayed", TASK,
         {"issuer": "auctioneer", "state": "open", **ANNOUNCED}),
        ("town", "award_rejected", TASK, {"reason": "task closed"}))

    for events in (plain, with_vocabulary):
        result = stage(events, scenario(coordination))
        assert result.status == "passed", result.note


@pytest.mark.parametrize("first, second", [
    (2, 3),  # receipt before its delivery
    (1, 2),  # delivery before the duplication
])
def test_consign_records_out_of_causal_order_fail(first, second):
    events = controlled_duplicate()
    events[first], events[second] = events[second], events[first]

    assert stage(events).status == "failed"


def test_target_announcement_must_follow_the_first_consign_receipt():
    """A startup auction plus a later consign is not a consign-opened task."""
    events = controlled_duplicate()
    announced = events.pop(4)
    announced.at = 0.0
    events.insert(0, announced)

    result = stage(events)

    assert result.status == "failed"
    assert announced.event_id in result.evidence


def test_composed_validator_appends_the_stage_to_auction_stages():
    spec, trace = scenario(), Trace(controlled_duplicate(), RUN)

    composed = validators.VALIDATORS[VALIDATOR](spec, trace)

    assert ([s.name for s in composed]
            == [s.name for s in auction(spec, trace)]
            + ["task_finalized_once"])
    assert composed[-1] == validators.task_finalized_once(spec, trace)


@pytest.mark.parametrize("coordination",
                         ["contractnet.v1", "contractnet.once.v1"])
def test_consign_handler_observes_each_invocation(coordination):
    """The handler evidence is the same whichever plugin is installed."""
    engine = build_engine(scenario(coordination))
    engine.run()
    events = engine.events

    sent, = [e for e in events if e.kind == "message_sent"
             and e.detail["kind"] == "consign"]
    assert (sent.observer, sent.detail["to"], sent.detail["body"]) == (
        "consignor", "auctioneer", {"item": ITEM})
    receipts = [e for e in events if e.kind == "consign_received"]
    assert [(e.observer, e.subject, e.detail) for e in receipts] == [
        ("auctioneer", sent.subject, {"from": "consignor", "item": ITEM})] * 2
    duplicated, = [e for e in events if e.kind == "message_duplicated"]
    arrivals = [e for e in events if e.kind == "message_delivered"
                and e.subject == sent.subject]
    chain = [sent, duplicated, arrivals[0], receipts[0], arrivals[1],
             receipts[1]]
    positions = [events.index(e) for e in chain]
    assert positions == sorted(positions)
    # The consign handler is the sole trigger: no startup auction.
    first_announcement = next(i for i, e in enumerate(events)
                              if e.kind == "task_announced")
    assert (events.index(receipts[0]) < first_announcement
            < events.index(arrivals[1]))


# -- audit regressions ------------------------------------------------------


@pytest.mark.parametrize("sent_at, before_startup", [
    (0.0, [True, True]),    # both deliveries precede the 0.5 startup auction
    (0.35, [True, False]),  # the startup auction falls between them
])
def test_r1_default_auctioneer_leaves_a_duplicated_consign_unhandled(
        monkeypatch, sent_at, before_startup):
    """Without open_on: consign the startup timer opens the auction, so the
    consign stays unhandled as before and the stage cannot certify the run."""
    monkeypatch.setattr(Consignor, "on_start",
                        lambda self: self.api.later(sent_at, self.consign))
    spec = scenario(open_on=None)
    engine = build_engine(spec)
    engine.run()
    events = engine.events

    sent, = [e for e in events if e.kind == "message_sent"
             and e.detail["kind"] == "consign"]
    arrivals = [e for e in events if e.kind == "message_delivered"
                and e.subject == sent.subject]
    announced, = [e for e in events if e.kind == "task_announced"]
    assert announced.at == 0.5  # the startup timer
    assert [e.at < announced.at for e in arrivals] == before_startup
    assert not [e for e in events if e.kind == "consign_received"]
    assert [(e.observer, e.subject, e.detail) for e in events
            if e.kind == "message_unhandled"] == [
        ("auctioneer", sent.subject, {"kind": "consign"})] * 2
    result = validators.task_finalized_once(spec,
                                            Trace(events, engine.run_id))
    assert result.status == "not_enough_evidence"


def test_r2_default_auctioneer_leaves_a_malformed_consign_unhandled():
    """Outside consign mode the body is never read: a consign without an
    item is unhandled, as before the consign handler existed."""
    engine = build_engine(load_bundled("auction"))
    engine.schedule(0.0, lambda: engine.agents["bidder-x"].api.send(
        "auctioneer", "consign", {}))
    engine.run()
    events = engine.events

    sent, = [e for e in events if e.kind == "message_sent"
             and e.detail["kind"] == "consign"]
    assert [(e.observer, e.subject, e.detail) for e in events
            if e.kind == "message_unhandled"] == [
        ("auctioneer", sent.subject, {"kind": "consign"})]
    assert not [e for e in events if e.kind == "consign_received"]
    assert len([e for e in events if e.kind == "task_announced"]) == 1


@pytest.mark.parametrize("open_on", [None, "startup"])
def test_r3_stage_requires_consign_mode(open_on):
    """Only open_on: consign removes the startup trigger."""
    result = stage(controlled_duplicate(), scenario(open_on=open_on))

    assert result.status == "not_enough_evidence"


@pytest.mark.parametrize("faults", [
    [],                                                      # none declared
    [{"action": "duplicate", "kind": "auction_bid"}],        # another kind
    [{"action": "duplicate"}],                               # any kind
    [{"action": "delay", "kind": "consign", "delay": 0.1}],  # another action
])
def test_r4_stage_requires_the_declared_duplicate_consign_fault(faults):
    result = stage(controlled_duplicate(), scenario(faults=faults))

    assert result.status == "not_enough_evidence"


@pytest.mark.parametrize("fault", ["delay", None])
def test_r5_duplication_record_must_name_the_duplicate_fault(fault):
    events = controlled_duplicate()
    if fault is None:
        del events[1].detail["fault"]
    else:
        events[1].detail["fault"] = fault

    result = stage(events)

    assert result.status == "failed"
    assert "event-1" in result.evidence


@pytest.mark.parametrize("row", [
    ("town", "message_delivered", MSG,
     {"to": "auctioneer", "kind": "auction_bid"}),
    ("town", "message_duplicated", MSG,
     {"to": "auctioneer", "kind": "auction_bid", "fault": "duplicate"}),
    ("auctioneer", "message_unhandled", MSG, {"kind": "consign"}),
])
def test_r6_contradictory_record_of_the_controlled_message_fails(row):
    """Every record naming the controlled message is judged, whatever kind
    it claims."""
    result = stage(controlled_duplicate(row))

    assert result.status == "failed"
    assert "event-8" in result.evidence


@pytest.mark.parametrize("duplicated", [False, True])
def test_r7_unrelated_consign_traffic_leaves_the_result_unchanged(duplicated):
    """Only the configured consignor's consign to the auctioneer is under
    control; other consign traffic in the run is not its evidence."""
    other = "msg-unrelated"
    rows = [("bidder-x", "message_sent", other,
             {"to": "bidder-y", "kind": "consign", "conversation": None,
              "body": {"item": "other-item"}})]
    if duplicated:
        rows.append(("town", "message_duplicated", other,
                     {"to": "bidder-y", "kind": "consign",
                      "fault": "duplicate"}))
    rows += [("town", "message_delivered", other,
              {"to": "bidder-y", "kind": "consign"}),
             ("bidder-y", "message_unhandled", other, {"kind": "consign"})]
    events = controlled_duplicate()
    unrelated = [record(10 + i, *row) for i, row in enumerate(rows)]
    for event in unrelated:
        event.at = 0.5
    events[1:1] = unrelated

    result = stage(events)

    assert result == stage(controlled_duplicate())
    assert result.status == "passed", result.note


@pytest.mark.parametrize("position", [
    3,  # after the first delivery, before its receipt
    5,  # after the second delivery, before its receipt
    6,  # after both receipts
])
def test_r8_target_announcement_must_fall_between_the_receipts(position):
    """receipt #1 < target announcement < delivery #2 < receipt #2."""
    events = controlled_duplicate()
    announced = events.pop(4)
    events.insert(position, announced)
    announced.at = events[position - 1].at

    result = stage(events)

    assert result.status == "failed"
    assert announced.event_id in result.evidence


# -- mutation-gap regressions -----------------------------------------------


def same_but_id(event, i, at):
    """The same record as event, but as event-i at time at."""
    again = record(i, event.observer, event.kind, event.subject, event.detail)
    again.at = at
    assert (again.model_dump(exclude={"event_id", "at"})
            == event.model_dump(exclude={"event_id", "at"}))
    return again


def test_t22_a_second_matching_controlled_send_fails():
    """Exactly one controlled send: a second consign of the same message
    from the consignor to the auctioneer fails on multiplicity alone."""
    valid = controlled_duplicate()
    assert stage(valid).status == "passed"
    events = controlled_duplicate()
    again = same_but_id(events[0], 8, 0.5)
    events.insert(1, again)  # after the original send, before any copy
    assert [e for e in events if e is not again] == valid

    result = stage(events)

    assert result.status == "failed"
    assert "expected one controlled consign, found 2" in result.note
    assert result.evidence[0] == again.event_id


def test_t23_three_duplication_records_of_the_controlled_message_fail():
    """One duplicate fault, one copy: two more well-formed duplication
    records of the controlled consign fail on the excess alone."""
    valid = controlled_duplicate()
    assert stage(valid).status == "passed"
    events = controlled_duplicate()
    extra = [same_but_id(events[1], 8, 1.25), same_but_id(events[1], 9, 1.5)]
    events[2:2] = extra  # after the original copy, before delivery #1
    assert [e for e in events if e not in extra] == valid

    result = stage(events)

    assert result.status == "failed"
    assert (f"expected 1 message_duplicated for consign {MSG}, found 3"
            in result.note)
    assert result.evidence[0] == extra[0].event_id
