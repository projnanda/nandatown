"""An offer that lapses may not be accepted, and no trade settles at it.

The enforced run uses negotiation=haggle.expiring.v1; the negative control
swaps in the default haggle.v1, which has no validity window and therefore
accepts the delayed offer and settles at the stale price.
"""

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.sim.runner import run_lab


def _stages(result):
    return {s.name: s.status for s in result.stages}


def test_enforced_run_holds_the_invariant(tmp_path):
    directory, result = run_lab("expiring_offer", str(tmp_path))
    stages = _stages(result)
    assert result.verdict == "passed"
    assert stages["offer_validity"] == "passed"
    assert stages["no_stale_settlement"] == "passed"

    bundle = load_bundle(directory)
    kinds = {e.kind for e in bundle["events"]}
    # the rule actually fired, and nothing settled against the lapsed offer
    assert "offer_expired" in kinds
    assert "offer_accepted" not in kinds
    assert "escrow_released" not in kinds


def test_enforced_bundle_verifies(tmp_path):
    directory, _ = run_lab("expiring_offer", str(tmp_path))
    assert verify_bundle(directory) == []


def test_negative_control_fails_as_predicted(tmp_path):
    directory, result = run_lab(
        "expiring_offer", str(tmp_path),
        layer_overrides={"negotiation": "haggle.v1"})
    stages = _stages(result)
    assert result.verdict == "failed"
    # the predicted failure: a stale offer was accepted and money moved
    assert stages["offer_validity"] == "failed"
    kinds = {e.kind for e in load_bundle(directory)["events"]}
    assert "offer_accepted" in kinds
    assert "escrow_released" in kinds
