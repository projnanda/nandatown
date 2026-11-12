"""TrailProof under Town's quote task: Track runs with the TrailProof buyer and seller.

These tests need a TrailProof checkout (https://github.com/Andreyivvi/TrailProof)
and are skipped without ``TRAILPROOF_HOME``; see examples/trailproof/README.md.
They exercise the project as a running MCP server, not a fixture of Town's.
"""

from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "trailproof")
sys.path.insert(0, EXAMPLE)

from trailproof_link import (  # noqa: E402
    TrailProofClient,
    start_server,
    stop_server,
    trailproof_python,
)
import run_check  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TRAILPROOF_HOME") and trailproof_python()),
    reason="TRAILPROOF_HOME (a TrailProof checkout with a .venv) is not set",
)

TRAILPROOF_TOOLS = {"register_party", "record_event", "check_order", "get_trail", "open_dispute", "reveal",
                    "get_case_bundle", "export_evidence_pack", "attest_history"}


@pytest.fixture(scope="module")
def ledger():
    process, url = start_server()
    try:
        yield url
    finally:
        stop_server(process)


def test_town_mcp_probe_accepts_the_trailproof_server():
    """Town's own MCP handshake probe (nandatown mcp test) against TrailProof over stdio."""
    from nandatown.mcp_adapter import probe

    report = probe([trailproof_python(), "-m", "trailproof.server"])
    assert report["ok"], report
    assert set(report["tools"]) == TRAILPROOF_TOOLS


def test_streamable_http_client_lists_the_same_tools(ledger):
    client = TrailProofClient(ledger)
    client.initialize()
    assert set(client.list_tools()) == TRAILPROOF_TOOLS


@pytest.mark.parametrize("case", list(run_check.CASES))
def test_quote_task_with_trailproof_participants(ledger, tmp_path, case):
    r = run_check.run_case(case, ledger, str(tmp_path))
    failed = [name for name, ok in r["checks"].items() if not ok]
    assert not failed, (failed, r["observed"]["stages"], r["observed"]["buyer_note"])


def test_duplicate_delivery_commits_one_offer(ledger, tmp_path):
    r = run_check.run_case("duplicate", ledger, str(tmp_path))
    assert r["ok"], r["checks"]
    assert [e["type"] for e in r["observed"]["trail"]].count("OFFER") == 1


def test_over_budget_records_no_order_and_names_the_rule(ledger, tmp_path):
    r = run_check.run_case("over_budget", ledger, str(tmp_path))
    assert r["ok"], r["checks"]
    reasons = r["observed"]["buyer_note"]["reasons"]
    assert reasons[0]["code"] == "OVER_BUDGET" and "39.90" in reasons[0]["detail"]
    assert "ORDER" not in [e["type"] for e in r["observed"]["trail"]]


def test_wrong_product_is_disputed_and_refunded_on_the_ledger(ledger, tmp_path):
    """The whole lifecycle: three roles commit, the buyer disputes, the ledger decides, the PSP refunds."""
    r = run_check.run_case("wrong_product", ledger, str(tmp_path))
    assert r["ok"], r["checks"]
    types = [e["type"] for e in r["observed"]["trail"]]
    assert types == ["MANDATE", "OFFER", "ORDER", "CHARGE", "FULFILMENT", "DELIVERY", "DISPUTE", "REFUND"]
    case = r["observed"]["case"]
    assert case["status"] == "closed" and case["verdict"] == "merchant_at_fault"
    assert case["remedy"] == {"type": "refund", "amount": "39.90", "currency": "EUR"}
    assert "DELIVERED_NE_OFFER" in case["findings"] and "MANDATE_IN_SCOPE" in case["findings"]
    # what Town recorded is the buyer's own account of the same case
    settlement = r["observed"]["buyer_note"]["settlement"]
    assert settlement["case_id"] == case["case_id"] and settlement["case_status"] == "closed"


def test_tampered_reveal_is_named_and_the_pack_still_verifies(ledger, tmp_path):
    """A seller that doctors its evidence: the reveal fails its hash, no refund, and the pack flags the event."""
    r = run_check.run_case("tampered_reveal", ledger, str(tmp_path))
    assert r["ok"], r["checks"]
    case = r["observed"]["case"]
    assert case["status"] == "open" and "TAMPERED" in case["findings"]
    assert "REFUND" not in [e["type"] for e in r["observed"]["trail"]]
    assert case["pack_unrevealed"] == ["FULFILMENT"]
    problems, counts = run_check.verify_pack(r["observed"]["evidence_pack"])
    assert problems == [] and counts["receipts"] == 7


def test_delivered_order_needs_no_dispute(ledger, tmp_path):
    r = run_check.run_case("delivered", ledger, str(tmp_path))
    assert r["ok"], r["checks"]
    assert "case" not in r["observed"] and r["observed"]["buyer_note"]["settlement"]["as_offered"] is True
