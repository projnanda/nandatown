"""hmac.bound.v1: a signed message is good for one recipient and one kind."""

import pytest

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.sim.api import TownAPI
from nandatown.sim.engine import Engine
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.scenario import ScenarioSpec, load_bundled
from nandatown.sim.validators import VALIDATORS, Trace


class Inbox:
    """An agent that keeps what the town delivers to it."""

    role = "inbox"

    def __init__(self, name):
        self.name = name
        self.received = []

    def on_start(self):
        pass

    def on_message(self, envelope):
        self.received.append(envelope)


def town(auth, names=("alice", "bob", "carol")):
    spec = ScenarioSpec(name="auth-unit", agents=[], layers={"auth": auth})
    engine = Engine(spec)
    for name in names:
        engine.add_agent(Inbox(name))
    return engine


def drain(engine):
    engine.run()
    return {name: agent.received for name, agent in engine.agents.items()}


def kinds(engine):
    return [e.kind for e in engine.events]


def relabelled(engine, original, to, kind):
    """What a recipient can build from a message it was sent: the same
    body and signature in a new envelope under the sender's name."""
    replay = engine.layers["communication"].envelope(
        original["sender"], to, kind, original["body"],
        original["conversation"])
    replay["signature"] = original["signature"]
    return replay


def test_default_auth_accepts_a_signed_body_relabelled_for_someone_else():
    """The gap: hmac.v1 signs the body alone."""
    engine = town("hmac.v1")
    sent = TownAPI(engine, "alice").send("bob", "notice", {"cents": 900})
    replay = relabelled(engine, sent, "carol", "invoice")
    engine.layers["transport"].send("bob", "carol", replay)

    carol = drain(engine)["carol"]
    assert [(m["sender"], m["kind"], m["body"]) for m in carol] == [
        ("alice", "invoice", {"cents": 900})]
    assert "signature_invalid" not in kinds(engine)


def test_bound_auth_refuses_the_relabelled_message():
    engine = town("hmac.bound.v1")
    sent = TownAPI(engine, "alice").send("bob", "notice", {"cents": 900})
    replay = relabelled(engine, sent, "carol", "invoice")
    engine.layers["transport"].send("bob", "carol", replay)

    inboxes = drain(engine)
    assert inboxes["carol"] == []
    assert [m["message_id"] for m in inboxes["bob"]] == [sent["message_id"]]
    invalid = [e for e in engine.events if e.kind == "signature_invalid"]
    assert [(e.subject, e.detail) for e in invalid] == [(
        replay["message_id"],
        {"claimed": "alice", "to": "carol", "kind": "invoice",
         "bound": "envelope"})]
    failed = [e for e in engine.events if e.kind == "delivery_failed"]
    assert [(e.subject, e.detail["reason"]) for e in failed] == [
        (replay["message_id"], "bad signature")]


def test_bound_auth_delivers_a_genuine_message():
    engine = town("hmac.bound.v1")
    sent = TownAPI(engine, "alice").send("bob", "notice", {"cents": 900})

    assert drain(engine)["bob"] == [sent]
    assert "signature_invalid" not in kinds(engine)
    assert "delivery_failed" not in kinds(engine)


@pytest.mark.parametrize("field, value", [
    ("to", "carol"),
    ("kind", "invoice"),
    ("conversation", "c-999"),
    ("message_id", "m-999"),
    ("sender", "carol"),
    ("body", {"cents": 1}),
])
def test_changing_any_signed_field_is_refused(field, value):
    engine = town("hmac.bound.v1")
    engine.layers["identity"].create("carol")
    sent = TownAPI(engine, "alice").send("bob", "notice", {"cents": 900})
    engine._queue.clear()
    changed = dict(sent, **{field: value})

    assert not engine.layers["auth"].verify_envelope(
        changed, changed["to"])
    assert engine.layers["auth"].verify_envelope(sent, "bob")


def test_an_intact_envelope_is_refused_at_the_wrong_recipient():
    """Handing the untouched envelope to someone it does not name."""
    engine = town("hmac.bound.v1")
    sent = TownAPI(engine, "alice").send("bob", "notice", {"cents": 900})
    engine.layers["transport"].send("bob", "carol", sent)

    inboxes = drain(engine)
    assert inboxes["carol"] == []
    assert inboxes["bob"] == [sent]


@pytest.mark.parametrize("damage", [
    lambda e: e.pop("to"),
    lambda e: e.pop("kind"),
    lambda e: e.pop("conversation"),
    lambda e: e.pop("signature"),
    lambda e: e.update(signature=None),
    lambda e: e.update(signature=""),
    lambda e: e.update(sender="nobody"),
    lambda e: e.update(sender=None),
])
def test_an_incomplete_or_unsigned_envelope_fails_closed(damage):
    engine = town("hmac.bound.v1")
    sent = TownAPI(engine, "alice").send("bob", "notice", {"cents": 900})
    broken = dict(sent)
    damage(broken)

    assert not engine.layers["auth"].verify_envelope(broken, "bob")
    assert kinds(engine)[-1] == "signature_invalid"


def test_signing_an_incomplete_envelope_raises():
    engine = town("hmac.bound.v1")
    engine.layers["identity"].create("alice")
    with pytest.raises(ValueError):
        engine.layers["auth"].sign_envelope(
            {"sender": "alice", "body": {}})


def test_a_duplicated_delivery_still_verifies():
    """The transport's duplicate fault redelivers the same envelope to
    the same recipient; recognizing it stays the agent's job."""
    spec = ScenarioSpec(
        name="auth-unit", agents=[], layers={"auth": "hmac.bound.v1"},
        faults=[{"action": "duplicate", "kind": "notice"}])
    engine = Engine(spec)
    for name in ("alice", "bob"):
        engine.add_agent(Inbox(name))
    sent = TownAPI(engine, "alice").send("bob", "notice", {"cents": 900})

    assert drain(engine)["bob"] == [sent, sent]
    assert "signature_invalid" not in kinds(engine)


def test_cards_are_still_signed_and_checked_as_before():
    engine = town("hmac.bound.v1")
    assert TownAPI(engine, "alice").register(["sell.widget"])
    assert not TownAPI(engine, "carol").register(
        ["sell.widget"], forge_key_of="shadow")

    registered = [e for e in engine.events if e.kind == "card_registered"]
    assert [(e.subject, e.detail["verified"]) for e in registered] == [
        ("alice", True)]
    assert "card_unverified" in kinds(engine)


def test_default_auth_still_signs_the_body_alone():
    engine = town("hmac.v1")
    sent = TownAPI(engine, "alice").send("bob", "notice", {"cents": 900})
    assert sent["signature"] == engine.layers["auth"].sign_as(
        "alice", {"cents": 900})


def test_replay_is_refused_and_only_the_winner_pays(tmp_path):
    bundle_dir, result = run_lab("auction_replay", str(tmp_path))
    stages = {s.name: s.status for s in result.stages}
    assert result.verdict == "passed", stages
    assert stages["replay_attempted"] == "passed"
    assert stages["replay_refused"] == "passed"
    assert stages["settlement"] == "passed"
    assert verify_bundle(bundle_dir) == []

    events = load_bundle(bundle_dir)["events"]
    paid = [(e.detail["from"], e.detail["cents"]) for e in events
            if e.kind == "payment_settled"]
    assert paid == [("bidder-y", 900)]
    refused = [e for e in events if e.kind == "delivery_failed"]
    assert [(e.detail["to"], e.detail["reason"]) for e in refused] == [
        ("bidder-late", "bad signature")]


def test_unbound_control_fails_because_a_loser_was_charged(tmp_path):
    """Negative control: the same run on hmac.v1 delivers the replay,
    and a bidder whose only bid was rejected pays the winning price."""
    bundle_dir, result = run_lab("auction_replay_unbound", str(tmp_path))
    stages = {s.name: s.status for s in result.stages}
    assert result.verdict == "failed", stages
    assert stages["replay_attempted"] == "passed"
    assert stages["replay_refused"] == "failed"
    assert stages["settlement"] == "failed"
    assert stages["ledger_conserved"] == "passed"
    assert verify_bundle(bundle_dir) == []

    events = load_bundle(bundle_dir)["events"]
    paid = [(e.detail["from"], e.detail["cents"]) for e in events
            if e.kind == "payment_settled"]
    assert paid == [("bidder-y", 900), ("bidder-late", 900)]
    assert [e.detail["to"] for e in events
            if e.kind == "message_delivered"
            and e.detail["kind"] == "item_delivery"] == ["bidder-y"]


def test_the_two_scenarios_differ_only_in_the_auth_plugin():
    bound = load_bundled("auction_replay")
    control = load_bundled("auction_replay_unbound")
    assert bound.layers["auth"] == "hmac.bound.v1"
    assert control.layers["auth"] == "hmac.v1"
    assert control.validator == "auction_replay"
    assert bound.agents == control.agents
    assert {k: v for k, v in bound.layers.items() if k != "auth"} == {
        k: v for k, v in control.layers.items() if k != "auth"}


def test_layer_override_reproduces_the_control(tmp_path):
    _, result = run_lab("auction_replay", str(tmp_path),
                        layer_overrides={"auth": "hmac.v1"})
    stages = {s.name: s.status for s in result.stages}
    assert result.verdict == "failed", stages
    assert stages["replay_refused"] == "failed"


def test_the_validator_does_not_pass_a_run_with_no_replay():
    """An ordinary auction has nothing to refuse, and says so."""
    spec = load_bundled("auction")
    engine = build_engine(spec)
    engine.run()
    stages = {s.name: s.status for s in VALIDATORS["auction_replay"](
        spec, Trace(engine.events, engine.run_id))}
    assert stages["replay_attempted"] == "not_enough_evidence"
    assert stages["replay_refused"] != "passed"


def test_a_refusal_does_not_excuse_a_payment_by_a_loser():
    """Refusing the replay is not enough if a loser was charged anyway."""
    spec = load_bundled("auction_replay")
    engine = build_engine(spec)
    engine.run()
    engine.layers["payments"].transfer("bidder-late", "auctioneer", 1,
                                       "auction-print-001")
    stages = {s.name: s.status for s in VALIDATORS["auction_replay"](
        spec, Trace(engine.events, engine.run_id))}
    assert stages["replay_attempted"] == "passed"
    assert stages["replay_refused"] == "failed"


@pytest.mark.parametrize("name", [
    "marketplace", "auction", "voting", "consensus", "supply_chain",
    "capability_spoofing",
])
def test_the_stock_scenarios_pass_unchanged_on_bound_auth(name, tmp_path):
    _, result = run_lab(name, str(tmp_path),
                        layer_overrides={"auth": "hmac.bound.v1"})
    assert result.verdict == "passed", {
        s.name: s.status for s in result.stages}


def test_the_replay_run_is_deterministic():
    def trace():
        engine = build_engine(load_bundled("auction_replay"))
        engine.run()
        return [e.model_dump(exclude={"run_id"}) for e in engine.events
                if e.subject != engine.run_id]

    assert trace() == trace()
