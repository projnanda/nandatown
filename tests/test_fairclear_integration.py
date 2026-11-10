"""FairClear integration: sealed second-price procurement through Town layers."""

import os
import sys
from pathlib import Path

import pytest

from nandatown.sim.runner import run_lab

ROOT = Path(__file__).resolve().parent.parent
SCENARIO = ROOT / "integrations" / "fairclear" / "fairclear_procurement.yaml"


def stages(result):
    return {s.name: s.status for s in result.stages}


def run(tmp_path, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _, result = run_lab(str(SCENARIO), str(tmp_path))
    return result


def test_reference_backend_passes_every_stage(tmp_path, monkeypatch):
    monkeypatch.delenv("FAIRCLEAR_BACKEND", raising=False)
    result = run(tmp_path, monkeypatch)
    s = stages(result)
    for name in ("discovery", "sealed_bids", "rejections", "efficient_award",
                 "second_price", "receipt_verified", "settlement",
                 "ledger_conserved"):
        assert s[name] == "passed", (name, s)


def test_clearing_rule_boundaries():
    sys.path.insert(0, str(SCENARIO.parent))
    from fairclear_town import clear_reference

    rfq = {"reserve_cents": 2000}
    bids = [{"bidder": "a", "cents": 1800}, {"bidder": "b", "cents": 1700}]
    assert clear_reference(rfq, bids) == {"winner": "b", "price_cents": 1800}
    # One eligible bid: the winner is paid the buyer's reserve.
    assert clear_reference(rfq, bids[:1]) == {"winner": "a", "price_cents": 2000}
    # Bids above the reserve can never win.
    assert clear_reference(rfq, [{"bidder": "c", "cents": 2500}]) == {
        "winner": None, "price_cents": None}


def test_wrong_clearing_service_is_caught(tmp_path, monkeypatch):
    """A service that pays the winner its own bid (first price) must fail
    the second_price check and the buyer's receipt check, and is not paid."""
    stub = tmp_path / "first_price.py"
    stub.write_text(
        "import json,sys\n"
        "r=json.load(sys.stdin)\n"
        "b=min(r['bids'],key=lambda x:(x['cents'],x['bidder']))\n"
        "print(json.dumps({'winner':b['bidder'],'price_cents':b['cents'],"
        "'result_hash':'x','verified':True}))\n")
    result = run(tmp_path / "out", monkeypatch, FAIRCLEAR_BACKEND="external",
                 FAIRCLEAR_CLEARING_CMD=f"{sys.executable} {stub}")
    s = stages(result)
    assert s["second_price"] == "failed"
    assert s["receipt_verified"] == "failed"
    # The buyer refused to pay, so no settlement exists to pass.
    assert s["settlement"] != "passed"
    assert s["ledger_conserved"] == "passed"


@pytest.mark.skipif(not os.environ.get("FAIRCLEAR_CLEARING_CMD"),
                    reason="set FAIRCLEAR_CLEARING_CMD to test a live deployment")
def test_external_backend_passes(tmp_path, monkeypatch):
    result = run(tmp_path, monkeypatch, FAIRCLEAR_BACKEND="external")
    assert result.verdict == "passed", stages(result)
