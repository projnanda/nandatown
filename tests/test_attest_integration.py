"""Attest x Town: an Attest-gated seller and a signing buyer through the Track.

Skipped unless ATTEST_HOME points at a checkout of github.com/smghaas/attest
(commit pinned in examples/attest/README.md). Starts the real gate over HTTP.
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ATTEST_HOME = os.environ.get("ATTEST_HOME")
pytestmark = pytest.mark.skipif(not ATTEST_HOME,
                                reason="set ATTEST_HOME to an Attest checkout")

from nandatown.runner import run_town  # noqa: E402

EX = Path(__file__).resolve().parent.parent / "examples" / "attest"


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    subprocess.run([sys.executable, str(EX / "world.py"), str(tmp_path)], check=True,
                   env={**os.environ, "ATTEST_HOME": ATTEST_HOME})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "gate.server:app", "--port", str(port)],
        cwd=ATTEST_HOME, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "ATTEST_STATE_DIR": str(tmp_path / "gate"),
             "ATTEST_LOG_DIR": str(tmp_path / "logs")})
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(url + "/pubkey", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    monkeypatch.setenv("ATTEST_URL", url)
    monkeypatch.setenv("ATTEST_CAST", str(tmp_path / "cast.json"))
    monkeypatch.setenv("DEADLINE", "8")
    yield url
    proc.kill()
    proc.wait(timeout=10)


def _run(tmp_path, monkeypatch, profile, agent="proc", tamper=False,
         journal=True, stock_buyer=False):
    monkeypatch.setenv("ATTEST_AGENT", agent)
    monkeypatch.setenv("ATTEST_TAMPER", "1" if tamper else "0")
    monkeypatch.setenv("ATTEST_JOURNAL", "1" if journal else "0")
    harnesses = {"seller": f"cmd:{sys.executable} {EX / 'attest_seller.py'}"}
    if not stock_buyer:
        harnesses["buyer"] = f"cmd:{sys.executable} {EX / 'attest_buyer.py'}"
    bundle_dir, result = run_town(profile, str(tmp_path / "runs"), harnesses=harnesses)
    stages = {s.name: str(getattr(s.status, "value", s.status)) for s in result.stages}
    events = [json.loads(line) for line in
              (Path(bundle_dir) / "events.jsonl").read_text().splitlines()]
    notes = [(e["observer"], e["detail"]["note"]) for e in events
             if e["kind"] == "ack_recorded"]
    return SimpleNamespace(stages=stages, notes=notes)


def _seller(run):
    return [n for who, n in run.notes if who == "seller"]


def test_signed_buyer_is_served_and_receipt_verifies(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-clean")
    assert r.stages["correct"] == "passed" and r.stages["processed"] == "passed"
    assert _seller(r)[0]["attest_decision"] == "allow"
    assert [n for who, n in r.notes if who == "buyer"][0]["attest_receipt_verified"] is True


@pytest.mark.parametrize("agent,tamper,decision,reason", [
    ("forger", False, "unknown", "pow_required"),          # unstaked signer behind an Acme name
    ("greedy", False, "refuse", "attenuation_violation"),  # sub-mandate wider than its parent's
    ("newcomer", False, "unknown", "pow_required"),        # no credential at all
    ("proc", True, "refuse", "bad_request_signature"),     # amount changed after signing
])
def test_unauthorized_buyer_gets_no_quote(gate, tmp_path, monkeypatch,
                                          agent, tamper, decision, reason):
    r = _run(tmp_path, monkeypatch, "quote-clean", agent=agent, tamper=tamper)
    note = _seller(r)[0]
    assert (note["attest_decision"], note["attest_reason"]) == (decision, reason)
    assert note["applied"] is False
    # Town reads a refusal as missing evidence, not as a passed or failed quote.
    assert r.stages["correct"] == "not_enough_evidence"


def test_stock_town_buyer_is_refused(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-clean", stock_buyer=True)
    note = _seller(r)[0]
    assert note["attest_decision"] == "refuse" and "no KYA envelope" in note["attest_reason"]
    assert r.stages["correct"] == "not_enough_evidence"


def test_duplicate_delivery_with_journal_serves_once(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-duplicate-delivery")
    assert r.stages["processed"] == "passed" and r.stages["correct"] == "passed"
    notes = _seller(r)
    assert notes[0]["attest_decision"] == "allow" and notes[1]["duplicate"] is True


def test_duplicate_delivery_without_journal_hits_gate_replay_refusal(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-duplicate-delivery", journal=False)
    notes = _seller(r)
    assert notes[0]["attest_decision"] == "allow"
    assert notes[1]["attest_decision"] == "refuse" and notes[1]["attest_reason"] == "replay"
