"""ProbiAI's insured ledger (examples/probiai): the claim it opens must match what Town's ledger shows.

These tests use a local stand-in for ProbiAI that answers like the hosted service and applies its published
double-payment rule (same amount, same seller, within 30 minutes), so they need no network or credentials.
The live check against the hosted service runs when PROBIAI_LIVE_KEY is set.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nandatown.sim.runner import run_lab

HERE = os.path.join(os.path.dirname(__file__), "..", "examples", "probiai")
KEY = "pk.test-agent"


class StandIn(BaseHTTPRequestHandler):
    """POST /v1/copies: keep the copy, reply with the record id and any double-payment claim it opens."""

    copies: list = []

    def do_POST(self):
        if self.path != "/v1/copies" or self.headers.get("authorization") != f"Bearer {KEY}":
            self.send_response(401)
            self.end_headers()
            return
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        fact = (body["input"]["amount"], body["input"]["merchant"])
        claims = []
        if fact in [(c["input"]["amount"], c["input"]["merchant"]) for c in StandIn.copies]:
            claims.append({"id": f"cl_{len(StandIn.copies)}", "trigger": "double_charge",
                           "description": "Two payments of the same amount at the same place", "amount": fact[0]})
        StandIn.copies.append(body)
        out = json.dumps({"ok": True, "kept": True, "record_id": f"ac_{len(StandIn.copies)}",
                          "claims": claims}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


@pytest.fixture
def probiai(monkeypatch):
    StandIn.copies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("PROBIAI_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("PROBIAI_KEY", KEY)
    monkeypatch.delenv("PROBIAI_KEYS", raising=False)
    monkeypatch.delenv("PROBIAI_INSURED", raising=False)
    yield StandIn
    server.shutdown()


def stages(scenario, tmp_path):
    _, result = run_lab(os.path.join(HERE, f"insured-{scenario}.yaml"), str(tmp_path))
    return {s.name: s for s in result.stages}


def test_a_double_payment_is_flagged_and_matches_the_ledger(probiai, tmp_path):
    s = stages("careless", tmp_path)
    assert s["insurer_received"].status == "passed"
    assert s["double_payment_verdict"].status == "passed"
    assert "paid twice" in s["double_payment_verdict"].note
    assert s["ledger_conserved"].status == "passed"          # money conserved, yet the buyer paid twice
    assert len(probiai.copies) == 2


def test_one_payment_raises_no_claim(probiai, tmp_path):
    s = stages("clean", tmp_path)
    assert s["double_payment_verdict"].status == "passed"
    assert "no claim" in s["double_payment_verdict"].note
    assert len(probiai.copies) == 1


def test_two_separate_orders_at_one_price_are_a_false_alarm_for_the_published_rule(probiai, tmp_path):
    """The boundary this integration found: the rule sees amount and seller, not the order."""
    s = stages("repeat", tmp_path)
    assert s["double_payment_verdict"].status == "failed"
    assert "false alarm" in s["double_payment_verdict"].note


def test_unreachable_probiai_leaves_town_unchanged_and_the_verdict_untested(monkeypatch, tmp_path):
    monkeypatch.setenv("PROBIAI_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PROBIAI_KEY", KEY)
    s = stages("careless", tmp_path)
    assert s["insurer_received"].status == "failed"
    assert s["double_payment_verdict"].status == "not_enough_evidence"
    assert s["ledger_conserved"].status == "passed"


def test_without_a_key_nothing_is_sent(monkeypatch, tmp_path):
    monkeypatch.delenv("PROBIAI_KEY", raising=False)
    monkeypatch.delenv("PROBIAI_KEYS", raising=False)
    s = stages("careless", tmp_path)
    assert s["insurer_received"].status == "not_enough_evidence"


@pytest.mark.skipif(not os.environ.get("PROBIAI_LIVE_KEY"), reason="set PROBIAI_LIVE_KEY to check the hosted ProbiAI")
def test_live_hosted_probiai_flags_the_double_payment(monkeypatch, tmp_path):
    monkeypatch.delenv("PROBIAI_URL", raising=False)
    monkeypatch.setenv("PROBIAI_KEY", os.environ["PROBIAI_LIVE_KEY"])
    s = stages("careless", tmp_path)
    assert s["insurer_received"].status == "passed"
    assert s["double_payment_verdict"].status == "passed"
