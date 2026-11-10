"""AgentTab integration: a Track seller that buys on credit before it quotes.

examples/agenttab_seller.py plays Town's seller role and, for each quote,
buys one 40-cent price lookup on its AgentTab credit line through
AgentTab's HTTP API (check_credit, signed IOU, charge, capture).
examples/agenttab_check.py then compares Town's evidence with AgentTab's
ledger. The interface under test is the meeting of two delivery
contracts: Town delivers work at least once, AgentTab must charge once.

Offline tests run against tests/fixtures/agenttab_contract_stub.py, a
Town-side stand-in with AgentTab's request and reply shapes. They show the
adapter follows the contract; they are not evidence about AgentTab.

The live test runs when AGENTTAB_URL names a running AgentTab service:

    AGENTTAB_URL=http://127.0.0.1:8000 python -m pytest -q \\
        tests/test_agenttab_integration.py -k live
"""

import json
import os
import subprocess
import sys

import pytest

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.runner import run_town

from fixtures import agenttab_contract_stub

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELLER = os.path.join(REPO_ROOT, "examples", "agenttab_seller.py")
CHECK = os.path.join(REPO_ROOT, "examples", "agenttab_check.py")


def run(profile, tmp_path, monkeypatch, url, wait_timeout=20.0, **env):
    """One Track run with the AgentTab seller. Returns (bundle dir, result,
    the seller's acknowledgements). wait_timeout is Town's run limit in
    seconds; the buyer gets 15 s less, so a hosted AgentTab, which answers
    over the network, needs Town's default of 60."""
    monkeypatch.setenv("AGENTTAB_URL", url)
    for name in ("AGENTTAB_BOUNDARY", "AGENTTAB_NONCE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    bundle_dir, result = run_town(
        profile, str(tmp_path), external={"seller": [sys.executable, SELLER]},
        wait_timeout=wait_timeout)
    acks = [e.detail for e in load_bundle(bundle_dir)["events"]
            if e.kind == "ack_recorded" and e.observer == "seller"]
    return bundle_dir, result, acks


def check(bundle_dir, url):
    """Run the check script on a bundle. Returns (exit code, its JSON)."""
    proc = subprocess.run([sys.executable, CHECK, bundle_dir],
                          env={**os.environ, "AGENTTAB_URL": url},
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, json.loads(proc.stdout) if proc.stdout else {}


@pytest.fixture
def stub():
    url, state, stop = agenttab_contract_stub.serve("stored_reply")
    yield url, state
    stop()


@pytest.fixture
def old_stub():
    url, state, stop = agenttab_contract_stub.serve("iou_replay")
    yield url, state
    stop()


def test_clean_quote_is_paid_for_once_and_the_evidence_names_the_charge(
        tmp_path, monkeypatch, stub):
    url, state = stub
    bundle_dir, result, acks = run("quote-clean", tmp_path, monkeypatch, url)
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    assert verify_bundle(bundle_dir) == []
    note = acks[-1]["note"]
    assert note["runtime"] == "agenttab-seller" and note["total_cents"] == 3990
    paid = note["agenttab"]
    assert paid["cost_cents"] == 40 and paid["check_credit"] == "accept"
    assert paid["authorization_id"] and paid["charge_id"]
    assert state.calls.count("charge") == 1 and state.calls.count("capture") == 1
    code, report = check(bundle_dir, url)
    assert code == 0 and report["match"] is True
    assert report["expected_lookups_charged"] == 1
    assert report["observed_lookups_charged"] == 1


def test_seller_that_forgets_and_restarts_recovers_the_charge_by_nonce(
        tmp_path, monkeypatch, stub):
    """Town's amnesia fault: the seller buys, answers, loses its journal and
    dies before acknowledging. Restarted, it presents the same nonce;
    AgentTab's retry rule hands back the first authorization."""
    url, state = stub
    bundle_dir, result, acks = run("quote-amnesia-restart", tmp_path,
                                   monkeypatch, url)
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    assert state.calls.count("charge") == 2          # asked twice ...
    assert len(state.auths) == 1                     # ... one authorization
    code, report = check(bundle_dir, url)
    assert code == 0, report
    assert report["observed_lookups_charged"] == 1


def test_older_agenttab_retry_rule_strands_the_restarted_seller(
        tmp_path, monkeypatch, old_stub):
    """The same fault against the retry rule AgentTab had before commit
    14d5403: the repeated nonce is refused as iou_replay and cannot be
    looked up, so the seller declines work it has already been charged
    for. Town reports the run as not passed and the check finds the
    stranded charge."""
    url, state = old_stub
    bundle_dir, result, acks = run("quote-amnesia-restart", tmp_path,
                                   monkeypatch, url)
    assert result.verdict != "passed"
    declined = [a for a in acks if a["status"] == "rejected"]
    assert declined, acks
    refusal = declined[-1]["note"]["agenttab"]
    assert refusal["reason"] == "retry_refused"
    assert refusal["charge_error"] == "iou_replay"
    assert refusal["lookup_error"] == "unknown_tool"
    code, report = check(bundle_dir, url)
    assert code == 1
    assert report["expected_lookups_charged"] == 0
    assert report["observed_lookups_charged"] == 1


def test_owner_policy_refusal_is_declined_work_not_an_unpaid_quote(
        tmp_path, monkeypatch, stub):
    """Boundary: the owner caps purchases at 10 cents, the lookup costs 40.
    AgentTab refuses, the seller sends no quote, and nothing is charged."""
    url, state = stub
    bundle_dir, result, acks = run("quote-clean", tmp_path, monkeypatch, url,
                                   AGENTTAB_BOUNDARY="policy_cap")
    assert result.verdict != "passed"
    assert [a["status"] for a in acks] == ["rejected"]
    refusal = acks[0]["note"]["agenttab"]
    assert refusal["reason"] == "credit_refused"
    assert refusal["binding_rule"] == "owner_policy"
    assert refusal["check_credit"] == "decline"
    responses = [e for e in load_bundle(bundle_dir)["events"]
                 if e.kind == "message_accepted"
                 and e.detail.get("kind") == "quote_response"]
    assert responses == [] and state.calls.count("capture") == 0
    code, report = check(bundle_dir, url)
    assert code == 0 and report["observed_lookups_charged"] == 0


def test_fresh_nonce_per_attempt_pays_twice_and_only_the_check_sees_it(
        tmp_path, monkeypatch, stub):
    """Negative control. With a new nonce on each attempt the restarted
    seller pays again. Every Town stage still passes, because Town sees
    one quote; the double payment shows only in AgentTab's ledger."""
    url, state = stub
    bundle_dir, result, acks = run("quote-amnesia-restart", tmp_path,
                                   monkeypatch, url, AGENTTAB_NONCE="fresh")
    assert result.verdict == "passed"
    code, report = check(bundle_dir, url)
    assert code == 1
    assert report["expected_lookups_charged"] == 1
    assert report["observed_lookups_charged"] == 2


def test_unreachable_agenttab_is_retryable_and_never_an_unpaid_quote(
        tmp_path, monkeypatch):
    bundle_dir, result, acks = run("quote-clean", tmp_path, monkeypatch,
                                   "http://127.0.0.1:9")
    assert result.verdict != "passed"
    assert acks and {a["status"] for a in acks} == {"retryable"}
    assert {a["note"]["reason"] for a in acks} == {"agenttab_unavailable"}
    responses = [e for e in load_bundle(bundle_dir)["events"]
                 if e.kind == "message_accepted"
                 and e.detail.get("kind") == "quote_response"]
    assert responses == []


@pytest.mark.skipif(not os.environ.get("AGENTTAB_URL"),
                    reason="AGENTTAB_URL is not set: no live AgentTab service")
@pytest.mark.parametrize("profile", ["quote-clean", "quote-crash-restart",
                                     "quote-duplicate-delivery",
                                     "quote-lost-ack"])
def test_live_agenttab_charges_once_under_town_faults(tmp_path, monkeypatch,
                                                      profile):
    """Against a real AgentTab service. These four profiles pass on every
    AgentTab version; quote-amnesia-restart depends on the version and is
    reported in docs/integrations/agenttab.md."""
    url = os.environ["AGENTTAB_URL"]
    bundle_dir, result, acks = run(profile, tmp_path, monkeypatch, url,
                                   wait_timeout=60.0)
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    code, report = check(bundle_dir, url)
    assert code == 0, report
    assert report["observed_lookups_charged"] == 1
