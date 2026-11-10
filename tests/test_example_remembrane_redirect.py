"""examples/integrations/remembrane-redirect: the recorded evidence verifies offline, and the personal agent's
redirect works through Town's real mailbox against a stand-in that speaks Remembrane's HTTP contract (it is not
Remembrane: it returns fixed categories, so it checks the Town side and the protocol, not the health memory)."""

import importlib.util
import json
from pathlib import Path
import secrets
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx

HERE = Path(__file__).resolve().parents[1] / "examples" / "integrations" / "remembrane-redirect"
spec = importlib.util.spec_from_file_location("remembrane_redirect_check", HERE / "run_check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_recorded_evidence_verifies_offline():
    evidence = check.verify_file(HERE / "evidence" / "local.json")
    assert [c["observed"]["status"] for c in evidence["cases"]] == [
        "granted", "granted", "declined", "granted", "declined", "unavailable"]


def test_scope_table_follows_the_policy():
    assert check.in_scope("fitness", "symptom", "cond_digestive_liver")          # a recent symptom
    assert not check.in_scope("fitness", "condition", "cond_digestive_liver")    # but not the condition
    assert not check.in_scope("fitness", "symptom", "cond_mental_health")        # never a sensitive group
    assert check.in_scope("intake", "medication", "med_gi")
    assert not check.in_scope("intake", "condition", "cond_mental_health")       # this patient opted out
    assert not check.in_scope("none", "allergy", "allergy")


class _StandIn(BaseHTTPRequestHandler):
    """/health, /delegate (owner only) and /context (delegated tokens) with Remembrane's request and reply shapes."""
    owner, tokens = "owner-" + secrets.token_hex(8), {}
    CATS = {"fitness": {"a": ("allergy", "allergy"), "b": ("symptom", "cond_musculoskeletal")},
            "intake": {"a": ("allergy", "allergy"), "c": ("medication", "med_gi")}}

    def log_message(self, *args):
        pass

    def _reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._reply(200, {"status": "ok"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path == "/delegate":
            if bearer in self.tokens:
                return self._reply(403, {"error": "delegate is owner-only"})
            if bearer != self.owner:
                return self._reply(401, {"error": "invalid token"})
            kind = body["card"].get("kind", "")
            purpose = "intake" if "clinic" in kind else "fitness" if "coach" in kind else "none"
            if body.get("requested_purpose") not in (None, purpose):
                purpose = "none"
            out = {"purpose": purpose, "assigned": purpose, "reason": "stand-in", "patient": body["patient"]}
            if purpose == "none":
                return self._reply(200, {**out, "status": "declined", "token": None})
            tok = secrets.token_urlsafe(16)
            self.tokens[tok] = purpose
            return self._reply(200, {**out, "status": "granted", "token": tok, "expires_in_minutes": 10,
                                     "redeem": {"url": base + "/context"}})
        if self.path == "/context":
            purpose = self.tokens.get(bearer)
            if not purpose:
                return self._reply(401, {"error": "invalid token"})
            cats = self.CATS[purpose]
            return self._reply(200, {"text": "stand-in", "purpose": purpose, "tokens": 2, "item_ids": sorted(cats),
                                     "sources": {i: {"kind": k, "category": c} for i, (k, c) in cats.items()}})
        self._reply(404, {})


def test_redirect_through_the_town_mailbox_against_a_stand_in():
    fixtures = json.loads((HERE / "requesters.json").read_text(encoding="utf-8"))["requesters"]
    pick = [r for r in fixtures if r["name"] in ("clinic-intake", "coach", "employer-wellbeing", "coach-asks-intake")]
    stand_in = ThreadingHTTPServer(("127.0.0.1", 0), _StandIn)
    Thread(target=stand_in.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{stand_in.server_port}"
    try:
        with tempfile.TemporaryDirectory() as scratch, httpx.Client(timeout=10, trust_env=False) as memory:
            with check.coordinator(Path(scratch) / "town.db") as (http, admin, town_url):
                cases = check.run_requesters(http, admin, town_url, url, _StandIn.owner, "patient-1", pick, memory)
    finally:
        stand_in.shutdown()
    assert [(c["case"], c["observed"]["status"], c["observed"]["purpose"]) for c in cases] == [
        ("clinic-intake", "granted", "intake"), ("coach", "granted", "fitness"),
        ("employer-wellbeing", "declined", "none"), ("coach-asks-intake", "declined", "none")]
