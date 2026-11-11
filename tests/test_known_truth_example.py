"""The known-truth qualification-gate example (examples/known_truth).

Scripted seller only: no model, no credentials, loopback network only.
This proves the gate mechanics (a buyer that admits or refuses a seller
by its qualification card). It is not evidence about any agent.
"""

import json
import os
import subprocess
import sys
import time

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.runner import run_town

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "known_truth")
BUYER = os.path.join(EXAMPLE, "qualifying_buyer.py")
SELLER = os.path.join(EXAMPLE, "candidate_seller.py")
CARDS = os.path.join(EXAMPLE, "cards")
CHECK_CARD = os.path.join(EXAMPLE, "check_card.py")


def card(name):
    return os.path.join(CARDS, name)


def gate_run(tmp_path, profile, card_name):
    started = time.time()
    bundle_dir, result = run_town(
        profile, str(tmp_path),
        external={"buyer": [sys.executable, BUYER, "--card", card(card_name)],
                  "seller": [sys.executable, SELLER, "--mode", "scripted"]})
    elapsed = time.time() - started
    events = load_bundle(bundle_dir)["events"]
    return bundle_dir, result, events, elapsed


def stage(result, name):
    return next(s for s in result.stages if s.name == name)


def buyer_exit_code(events):
    return next(e.detail["exit_code"] for e in events
                if e.kind == "participant_exited" and e.subject == "buyer")


def accepted_kinds(events):
    return [e.detail["kind"] for e in events if e.kind == "message_accepted"]


def refusal_note(events):
    acks = [e for e in events if e.kind == "ack_recorded"
            and e.observer == "seller"
            and e.detail["note"].get("qualification_refused")]
    assert len(acks) == 1
    return acks[0].detail["note"]


def test_qualifying_card_is_admitted_and_acknowledged_with_card(tmp_path):
    bundle_dir, result, events, _ = gate_run(
        tmp_path, "quote-duplicate-delivery",
        "qualifying__haiku55_medium.json")
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    assert stage(result, "duplicate_recognized").status == "passed", detail
    assert stage(result, "correct").status == "passed", detail
    assert buyer_exit_code(events) == 0
    with open(card("qualifying__haiku55_medium.json")) as handle:
        issued = json.load(handle)
    buyer_acks = [e for e in events if e.kind == "ack_recorded"
                  and e.observer == "buyer"
                  and "qualification" in e.detail["note"]]
    assert len(buyer_acks) == 1
    assert buyer_acks[0].detail["note"]["qualification"] == {
        "benchmark_id": issued["benchmark_id"],
        "candidate_id": "haiku55_medium",
        "exact_pass": True,
        "card_sha256": issued["card_sha256"]}
    assert verify_bundle(bundle_dir) == []


def test_non_qualifying_card_is_refused_with_no_quote_request(tmp_path):
    bundle_dir, result, events, elapsed = gate_run(
        tmp_path, "quote-clean", "not_qualifying__haiku45.json")
    assert accepted_kinds(events) == ["qualification_refused"]
    assert "quote_request" not in accepted_kinds(events)
    note = refusal_note(events)
    assert note["reason"] == "not_qualified"
    assert note["candidate_id"] == "haiku45"
    assert len(note["failed_checks"]) == 3
    assert buyer_exit_code(events) == 6
    assert result.verdict == "incomplete"
    assert stage(result, "accepted").status == "not_enough_evidence"
    assert elapsed < 20, f"refusal waited too long: {elapsed:.1f}s"
    assert verify_bundle(bundle_dir) == []


def test_tampered_card_is_refused_as_hash_mismatch(tmp_path):
    bundle_dir, result, events, elapsed = gate_run(
        tmp_path, "quote-clean", "tampered__haiku55_medium.json")
    assert accepted_kinds(events) == ["qualification_refused"]
    assert refusal_note(events)["reason"] == "card_hash_mismatch"
    assert buyer_exit_code(events) == 7
    assert elapsed < 20, f"refusal waited too long: {elapsed:.1f}s"
    assert verify_bundle(bundle_dir) == []


def test_check_card_helper_classifies_the_fixtures():
    codes = {name: subprocess.run(
        [sys.executable, CHECK_CARD, card(name)],
        capture_output=True).returncode
        for name in ("qualifying__haiku55_medium.json",
                     "not_qualifying__haiku45.json",
                     "tampered__haiku55_medium.json")}
    assert codes == {"qualifying__haiku55_medium.json": 0,
                     "not_qualifying__haiku45.json": 6,
                     "tampered__haiku55_medium.json": 7}


def test_live_mode_refuses_without_project_environment():
    env = {k: v for k, v in os.environ.items()
           if k not in ("KNOWNTRUTH_PROJECT_DIR", "PROJECT_PYTHON")}
    done = subprocess.run(
        [sys.executable, SELLER, "--mode", "live", "--candidate", "x",
         "--yes"], env=env, capture_output=True, text=True)
    assert done.returncode == 2
    assert "KNOWNTRUTH_PROJECT_DIR" in done.stderr
