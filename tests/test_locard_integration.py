"""Locard integration (integrations/locard): a coverage-gated vendor in Town's Track quote task.

Runs the real Track runner and coordinator with this integration's buyer and vendor
as command participants, against a stand-in Locard service (fake_locard.py) so the
tests need no outside service. The compatibility evidence against the real Locard
service is produced by integrations/locard/run_checks.py.
"""

import os
import sys

import pytest

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.runner import run_town

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCARD_DIR = os.path.join(REPO_ROOT, "integrations", "locard")
sys.path.insert(0, LOCARD_DIR)

import fake_locard  # noqa: E402
from vendor import decide  # noqa: E402

DEAD_LOCARD = "http://127.0.0.1:9"


@pytest.fixture(scope="module")
def locard():
    server, state = fake_locard.serve()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()


def _run(tmp_path, profile, locard_url, mode="insured", agent_id="bot", vendor_url=None):
    vendor = [sys.executable, os.path.join(LOCARD_DIR, "vendor.py"),
              "--locard-url", vendor_url or locard_url]
    buyer = [sys.executable, os.path.join(LOCARD_DIR, "buyer.py"), "--mode", mode,
             "--locard-url", locard_url, "--agent-id", agent_id]
    bundle_dir, result = run_town(profile, str(tmp_path),
                                  external={"seller": vendor, "buyer": buyer})
    assert verify_bundle(bundle_dir) == []
    events = load_bundle(bundle_dir)["events"]
    vendor_acks = [e.detail for e in events
                   if e.kind == "ack_recorded" and e.observer == "seller"]
    return result, vendor_acks


def _stages(result):
    return {s.name: s.status for s in result.stages}


def test_insured_buyer_gets_a_quote(tmp_path, locard):
    url, state = locard
    calls = state.verify_calls
    result, acks = _run(tmp_path, "quote-clean", url, agent_id="bot-insured")
    assert result.verdict == "passed", _stages(result)
    assert acks[0]["status"] == "processed"
    assert acks[0]["note"]["coverage_verified"] is True
    assert state.verify_calls == calls + 1


def test_suspended_policy_is_refused(tmp_path, locard):
    url, _ = locard
    result, acks = _run(tmp_path, "quote-clean", url, mode="violate", agent_id="bot-violate")
    assert result.verdict != "passed"
    assert _stages(result)["processed"] != "passed"
    assert acks[0]["status"] == "rejected"
    assert acks[0]["note"]["applied"] is False
    assert "policy_suspended:over_spend_cap" in acks[0]["note"]["reasons"]


def test_tampered_token_is_refused(tmp_path, locard):
    url, _ = locard
    result, acks = _run(tmp_path, "quote-clean", url, mode="tamper", agent_id="bot-tamper")
    assert result.verdict != "passed"
    assert acks[0]["note"]["reasons"] == ["invalid_signature"]


def test_unreachable_locard_fails_closed(tmp_path, locard):
    url, _ = locard
    result, acks = _run(tmp_path, "quote-clean", url, agent_id="bot-down", vendor_url=DEAD_LOCARD)
    assert result.verdict != "passed"
    assert acks[0]["status"] == "rejected"
    assert acks[0]["note"]["reasons"] == ["locard_unreachable"]


def test_duplicate_delivery_checks_coverage_once_and_applies_once(tmp_path, locard):
    """Regression: the vendor used to repeat applied=true on the redelivered copy, which
    Town scored as "applied 2 times, expected once". The duplicate ack must say only
    duplicate, and the coverage check must not run twice."""
    url, state = locard
    calls = state.verify_calls
    result, acks = _run(tmp_path, "quote-duplicate-delivery", url, agent_id="bot-dup")
    assert result.verdict == "passed", _stages(result)
    assert [a["note"] for a in acks if a["note"].get("duplicate")] == [{"duplicate": True}]
    assert state.verify_calls == calls + 1


def test_request_without_a_policy_is_refused_without_calling_locard():
    """What Town's own scripted buyer sends: no coverage field at all."""
    decision = decide(DEAD_LOCARD, {"sku": "widget", "quantity": 2, "unit_price_cents": 1995})
    assert decision == {"covered": False, "total_cents": 3990,
                        "reasons": ["coverage_required"], "locard": None}
