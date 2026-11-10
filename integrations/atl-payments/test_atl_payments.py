"""Compatibility checks for the ATL payments integration (skipped unless an ATL checkout is configured).

    ATL_REPO=/path/to/atl ATL_VAR_DIR=/tmp/atl-town python integrations/atl-payments/setup_atl.py
    ATL_REPO=/path/to/atl ATL_VAR_DIR=/tmp/atl-town python -m pytest -q integrations/atl-payments
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nandatown.sim.runner import run_lab

HERE = Path(__file__).resolve().parent
pytestmark = pytest.mark.skipif(
    not (os.environ.get("ATL_REPO") and os.environ.get("ATL_VAR_DIR")),
    reason="needs ATL_REPO and an ATL_VAR_DIR prepared by setup_atl.py",
)


def run(name: str, tmp_path: Path, scenario: str | None = None, layer: str | None = None):
    target = scenario or str(HERE / "scenarios" / f"{name}.yaml")
    overrides = {"payments": layer} if layer else None
    plugins = [str(HERE / "atl_payments.py")] if layer else None
    bundle, result = run_lab(target, str(tmp_path), plugins=plugins, layer_overrides=overrides)
    events = [json.loads(x) for x in Path(bundle, "events.jsonl").read_text().splitlines() if x.strip()]
    stages = {s.name: s.status for s in result.stages}
    return result, stages, events


def paid(events, to=None):
    return sum(e["detail"]["cents"] for e in events if e["kind"] == "payment_settled"
               and e["detail"].get("from") != e["detail"].get("to") and (to is None or e["detail"].get("to") == to))


def test_marketplace_pays_only_with_atl_receipts(tmp_path):
    result, stages, events = run("atl_marketplace", tmp_path)
    assert result.verdict == "passed"
    assert stages["atl_every_payment_receipted"] == "passed"
    receipts = [e for e in events if e["kind"] == "atl_receipt"]
    assert len(receipts) == 2 and all(e["detail"]["valid"] is True for e in receipts)


def test_rival_is_never_paid_but_discovery_still_fails(tmp_path):
    result, stages, events = run("atl_registry_eviction", tmp_path)
    assert stages["atl_rival_unpaid"] == "passed"
    assert paid(events, "seller-rival") == 0
    refused = [e for e in events if e["kind"] == "atl_decision" and e["detail"]["verdict"] == "refuse"]
    assert refused and refused[0]["detail"]["rule_hits"] == ["R2_UNKNOWN_PAYEE"]
    # Boundary: ATL protects the money, not the index; Town's discovery stages still fail on index.v1.
    assert stages["listing_intact"] == "failed" and result.verdict == "failed"


def test_baseline_ledger_pays_the_rival(tmp_path):
    _, _, events = run("", tmp_path, scenario="registry_eviction_unowned")
    assert paid(events, "seller-rival") > 0


def test_owned_index_and_atl_together_pass(tmp_path):
    result, stages, _ = run("atl_registry_eviction_owned", tmp_path)
    assert result.verdict == "passed" and stages["honest_trade_completed"] == "passed"


def test_over_cap_is_escalated_refused_and_refunded(tmp_path):
    result, stages, events = run("atl_over_cap", tmp_path)
    assert result.verdict == "passed" and stages["atl_over_cap_held"] == "passed"
    assert paid(events) == 0
    assert any(e["kind"] == "escrow_refunded" for e in events)


def test_swap_layer_on_upstream_marketplace(tmp_path):
    # The plugin also works as a plain --layer swap on Town's own scenario.
    result, stages, events = run("", tmp_path, scenario="marketplace", layer="atl.v1")
    assert result.verdict == "passed"
    assert sum(1 for e in events if e["kind"] == "atl_receipt" and e["detail"]["valid"]) == 2
