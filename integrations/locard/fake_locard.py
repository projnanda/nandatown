"""A stand-in Locard service for Town's test suite (standard library only).

It implements the slice of the Locard API this integration uses, with the same
request and response shapes (locard-mvp INTERFACE_SPEC section 3):
submissions -> quotes -> accept -> events (a spend-cap violation suspends) -> verify.

It is NOT Locard: there is no risk model, pricing or Ed25519 JWT. Tokens are
HMAC-signed and the scores are fixed. The real compatibility evidence comes from
run_checks.py against the real service; this exists so the tests run offline.

    python integrations/locard/fake_locard.py --port 8701
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SECRET = secrets.token_bytes(32)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign(claims: dict[str, Any]) -> str:
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(claims, sort_keys=True).encode())
    sig = _b64(hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def unsign(token: str) -> dict[str, Any] | None:
    try:
        head, body, sig = token.split(".")
    except ValueError:
        return None
    want = _b64(hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(want, sig):
        return None
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.submissions: dict[str, dict] = {}
        self.quotes: dict[str, dict] = {}
        self.policies: dict[str, dict] = {}
        self.suspended_agents: set[str] = set()
        self.verify_calls = 0


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep test output quiet
            pass

        def _send(self, status: int, body: dict[str, Any]):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802 (http.server naming)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            parts = self.path.strip("/").split("/")
            with state.lock:
                if self.path == "/v1/submissions":
                    sid = "sub_" + secrets.token_hex(2).upper()
                    state.submissions[sid] = body["manifest"]
                    return self._send(201, {"submission_id": sid, "risk": {"mean_score": 2.5},
                                            "trace_stats": {"events": len(body["traces"])}})
                if self.path == "/v1/quotes":
                    manifest = state.submissions[body["submission_id"]]
                    if manifest["agent_id"] in state.suspended_agents:
                        return self._send(409, {"error": "agent_has_suspended_policy", "detail": ""})
                    qid = "q_" + secrets.token_hex(2).upper()
                    state.quotes[qid] = manifest
                    return self._send(201, {
                        "quote_id": qid, "agent_id": manifest["agent_id"],
                        "premium_annual_cents": 500_000,
                        "coverage": {"spend_cap_per_tx_cents": manifest["spend_cap_per_tx_cents"]}})
                if parts[:2] == ["v1", "quotes"] and parts[-1] == "accept":
                    manifest = state.quotes[parts[2]]
                    pid = "pol_" + secrets.token_hex(2).upper()
                    state.policies[pid] = {"agent_id": manifest["agent_id"], "status": "active",
                                           "reason": None, "api_key": "mon_" + secrets.token_hex(8),
                                           "cap": manifest["spend_cap_per_tx_cents"]}
                    token = sign({"sub": manifest["agent_id"], "policy_id": pid,
                                  "coverage": {"spend_cap_per_tx_cents": manifest["spend_cap_per_tx_cents"]}})
                    return self._send(201, {
                        "policy_id": pid, "status": "active", "policy_token": token,
                        "monitoring": {"events_url": f"/v1/policies/{pid}/events",
                                       "api_key": state.policies[pid]["api_key"]}})
                if parts[:2] == ["v1", "policies"] and parts[-1] == "events":
                    pol = state.policies[parts[2]]
                    if self.headers.get("Authorization") != f"Bearer {pol['api_key']}":
                        return self._send(401, {"error": "bad_monitoring_key", "detail": ""})
                    violation = "over_spend_cap" if body.get("amount_cents", 0) > pol["cap"] else None
                    if violation:
                        pol.update(status="suspended", reason=violation)
                        state.suspended_agents.add(pol["agent_id"])
                    return self._send(200, {"accepted": True, "violation": violation,
                                            "policy_status": pol["status"]})
                if self.path == "/v1/verify":
                    state.verify_calls += 1
                    claims = unsign(body.get("policy_token", ""))
                    if claims is None:
                        return self._send(200, {"valid": False, "status": "unknown",
                                                "covers_transaction": False,
                                                "reasons": ["invalid_signature"]})
                    pol = state.policies[claims["policy_id"]]
                    reasons = []
                    if claims["sub"] != body.get("agent_id"):
                        reasons.append("agent_mismatch")
                    if pol["status"] != "active":
                        reasons.append(f"policy_{pol['status']}:{pol['reason']}")
                    covers = body.get("transaction_amount_cents", 0) <= pol["cap"]
                    return self._send(200, {"valid": not reasons, "status": pol["status"],
                                            "covers_transaction": covers and not reasons,
                                            "reasons": reasons})
            return self._send(404, {"error": "not_found", "detail": self.path})

    return Handler


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, State]:
    """Start the stand-in on 127.0.0.1 in a background thread. Port 0 picks a free one."""
    state = State()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8701)
    srv, _ = serve(ap.parse_args().port)
    print(f"stand-in Locard on http://127.0.0.1:{srv.server_address[1]}")
    threading.Event().wait()
