"""index.owned.v1: a forged card cannot erase or withdraw a listing."""

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.sim.engine import Engine
from nandatown.sim.runner import run_lab
from nandatown.sim.scenario import ScenarioSpec


def town(registry: str) -> Engine:
    spec = ScenarioSpec(name="registry-unit", agents=[],
                        layers={"registry": registry})
    return Engine(spec)


def signed_card(engine, name, signer, capabilities=("sell.widget",),
                facts=None):
    card = engine.layers["identity"].card(name, list(capabilities),
                                          facts or {})
    engine.layers["identity"].create(signer)
    return card, engine.layers["auth"].sign_as(signer, card)


def kinds(engine):
    return [e.kind for e in engine.events]


def test_default_index_lets_a_forged_card_erase_a_verified_listing():
    """The gap: index.v1 stores the forged card over the verified one."""
    engine = town("index.v1")
    registry = engine.layers["registry"]
    registry.publish("honest", *signed_card(engine, "honest", "honest"))
    assert registry.names_with("sell.widget") == ["honest"]

    registry.publish("rival", *signed_card(engine, "honest", "rival"))
    assert registry.names_with("sell.widget") == []


def test_forged_card_is_refused_and_the_listing_survives():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    card, signature = signed_card(engine, "honest", "honest")
    assert registry.publish("honest", card, signature)

    forged, forged_sig = signed_card(engine, "honest", "rival",
                                     facts={"note": "closed"})
    assert not registry.publish("rival", forged, forged_sig)
    assert registry.names_with("sell.widget") == ["honest"]
    assert registry.cards["honest"]["card"] == card
    refused = [e for e in engine.events if e.kind == "card_publish_refused"]
    assert [e.detail["publisher"] for e in refused] == ["rival"]


def test_owner_can_update_its_listing():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    registry.publish("honest", *signed_card(engine, "honest", "honest"))
    updated, signature = signed_card(engine, "honest", "honest",
                                     capabilities=("sell.gadget",))
    assert registry.publish("honest", updated, signature)
    assert registry.names_with("sell.widget") == []
    assert registry.names_with("sell.gadget") == ["honest"]
    assert engine.events[-1].detail["update"] is True


def test_unverified_squat_does_not_claim_the_name():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    assert not registry.publish("rival",
                                *signed_card(engine, "honest", "rival"))
    assert registry.names_with("sell.widget") == []
    assert registry.publish("honest",
                            *signed_card(engine, "honest", "honest"))
    assert registry.names_with("sell.widget") == ["honest"]


def test_owner_withdraws_and_a_forged_withdrawal_is_refused():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    auth = engine.layers["auth"]
    card, signature = signed_card(engine, "honest", "honest")
    registry.publish("honest", card, signature)
    request = registry.withdrawal("honest", card)
    engine.layers["identity"].create("rival")

    assert not registry.withdraw("rival", "honest",
                                 auth.sign_as("rival", request))
    assert registry.names_with("sell.widget") == ["honest"]

    assert registry.withdraw("honest", "honest",
                             auth.sign_as("honest", request))
    assert registry.names_with("sell.widget") == []
    assert kinds(engine)[-1] == "card_withdrawn"
    assert not registry.withdraw("honest", "honest",
                                 auth.sign_as("honest", request))


def test_a_replayed_withdrawal_cannot_remove_a_newer_listing():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    auth = engine.layers["auth"]
    first, signature = signed_card(engine, "honest", "honest")
    registry.publish("honest", first, signature)
    engine.layers["identity"].create("rival")
    old = auth.sign_as("honest", registry.withdrawal("honest", first))
    assert registry.withdraw("honest", "honest", old)

    second, signature = signed_card(engine, "honest", "honest",
                                    facts={"reopened": True})
    registry.publish("honest", second, signature)
    assert not registry.withdraw("rival", "honest", old)
    assert registry.names_with("sell.widget") == ["honest"]


def test_eviction_scenario_passes_and_verifies(tmp_path):
    bundle_dir, result = run_lab("registry_eviction", str(tmp_path))
    stages = {s.name: s.status for s in result.stages}
    assert result.verdict == "passed", stages
    assert verify_bundle(bundle_dir) == []
    events = load_bundle(bundle_dir)["events"]
    assert [e.subject for e in events
            if e.kind == "card_publish_refused"] == ["seller-honest"]
    paid = [e.detail["to"] for e in events if e.kind == "escrow_released"]
    assert paid == ["seller-honest"]


def test_unowned_control_fails_because_the_listing_was_erased(tmp_path):
    """Negative control: the same run on index.v1 fails at listing_intact,
    and the rival, not the honest seller, is paid."""
    bundle_dir, result = run_lab("registry_eviction_unowned", str(tmp_path))
    stages = {s.name: s.status for s in result.stages}
    assert result.verdict == "failed", stages
    assert stages["forgery_detected"] == "passed"
    assert stages["listing_intact"] == "failed"
    assert stages["honest_trade_completed"] == "failed"
    assert stages["ledger_conserved"] == "passed"
    assert verify_bundle(bundle_dir) == []
    events = load_bundle(bundle_dir)["events"]
    paid = [e.detail["to"] for e in events if e.kind == "escrow_released"]
    assert paid == ["seller-rival"]
