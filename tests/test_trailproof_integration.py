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
