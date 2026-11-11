#!/usr/bin/env python3
"""A local stand-in for Cerata's HTTP surface. A Town FIXTURE, not Cerata.

It exists so CI and a rehearsal can exercise the adapter, the buyer and the
Track wiring without network access or an API key. It answers
POST /v1/preflight by looking the request up in cases.json and returning that
case's *expected* answer, so a pass against it says nothing about Cerata. Only a run against the real service (check.sh with CERATA_URL)
is compatibility evidence.

What it does model faithfully, because the adapter depends on it:
- Idempotency-Key: a repeat replays the stored response with
  `Idempotent-Replayed: true`; another body under the same key is 422; no
  key means a fresh check with a new check id and receipt.
- POST /v1/verify: receipts verify; a clearance token is consumed once
  (`?consume=true`), then refused with `already_consumed`.
- POST /mcp: initialize, tools/list and tools/call for preflight and verify,
  as stateless JSON-RPC like Cerata's Streamable HTTP endpoint.

    python fake_cerata.py --port 8971    # prints "listening on ..."
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET = b"fake-cerata-fixture-secret"
API_KEY = "ak_test_fixture_local"
TOOLS = ["preflight", "approve_campaign", "ask_coverage", "explain_rule", "get_hold_status", "verify"]


def canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sign(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(canon(claims).encode()).decode().rstrip("=")
    mac = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"fixture.{payload}.{mac}"


def unsign(token: str) -> dict[str, Any] | None:
    try:
        tag, payload, mac = token.split(".")
    except ValueError:
        return None
    good = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    if tag != "fixture" or not hmac.compare_digest(mac, good):
        return None
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class State:
    def __init__(self, cases_path: str):
        with open(cases_path) as f:
            self.cases: dict[str, dict[str, Any]] = {}
            for c in json.load(f)["cases"]:
                # A repeat case shares its facts; the first case owns the expectation.
                self.cases.setdefault(canon(c["request"]), c)
        self.idem: dict[str, tuple[str, dict[str, Any]]] = {}
        self.consumed: set[str] = set()
        self.lock = threading.Lock()

    def decide(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        case = self.cases.get(canon(request))
        if case is None:
            return 422, {"error": {"code": "fixture_unknown_request"}}
        e = case["expect"]
        decision = "block" if e.get("never_clear") else e["decision"]
        check_id = "chk_" + uuid.uuid4().hex[:16]
        digest = "sha256:" + hashlib.sha256(canon(request).encode()).hexdigest()
        out: dict[str, Any] = {
            "check_id": check_id, "env": "test", "decision": decision,
            "decision_digest": digest,
        }
        if decision == "hold":
            out["hold"] = {"hold_id": "hold_" + check_id[4:], "status_url": f"/v1/holds/hold_{check_id[4:]}", "state": "pending"}
        else:
            out["receipt_id"] = "rcpt_" + check_id[4:]
            out["receipt"] = sign({"kind": "receipt", "rid": out["receipt_id"], "decision": decision, "digest": digest})
        if e.get("token"):
            aud = (request.get("token") or {}).get("aud", "town-agent")
            out["token"] = sign({"kind": "token", "jti": uuid.uuid4().hex, "aud": aud})
        return 200, out

    def preflight(self, body: dict[str, Any], key: str | None) -> tuple[int, dict[str, str], dict[str, Any]]:
        with self.lock:
            if key is not None and key in self.idem:
                stored_body, stored = self.idem[key]
                if stored_body != canon(body):
                    return 422, {}, {"error": {"code": "idempotency_key_reused"}}
                return (202 if "hold" in stored else 200), {"Idempotent-Replayed": "true"}, stored
            status, out = self.decide(body)
            if status == 200 and "hold" in out:
                status = 202  # as Cerata answers a hold
            if key is not None and status in (200, 202):
                self.idem[key] = (canon(body), out)
            return status, {}, out

    def verify(self, body: dict[str, Any], consume: bool) -> dict[str, Any]:
        if "receipt" in body:
            claims = unsign(body["receipt"])
            return {"valid": claims is not None and claims.get("kind") == "receipt", "kind": "receipt", "kid": "fixture"}
        claims = unsign(body.get("token", ""))
        if claims is None or claims.get("kind") != "token":
            return {"valid": False, "kind": "token", "reason": "bad_signature"}
        if not consume:
            return {"valid": True, "kind": "token", "claims": {"aud": claims["aud"]}}
        if body.get("aud") != claims["aud"]:
            return {"valid": False, "kind": "token", "reason": "aud_mismatch"}
        with self.lock:
            if claims["jti"] in self.consumed:
                return {"valid": False, "kind": "token", "reason": "already_consumed"}
            self.consumed.add(claims["jti"])
        return {"valid": True, "kind": "token", "consumed": True}


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, status: int, payload: Any, headers: dict[str, str] | None = None):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("content-length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _authed(self) -> bool:
            return self.headers.get("authorization") == f"Bearer {API_KEY}"

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"ok": True, "fixture": True})
            return self._send(404, {"error": {"code": "not_found"}})

        def do_POST(self):
            path, _, query = self.path.partition("?")
            body = self._body()
            if path == "/v1/verify":
                return self._send(200, state.verify(body, "consume=true" in query))
            if not self._authed():
                return self._send(401, {"error": {"code": "unauthorized"}})
            if path == "/v1/preflight":
                status, headers, out = state.preflight(body, self.headers.get("idempotency-key"))
                return self._send(status, out, headers)
            if path == "/mcp":
                return self._send(200, self._mcp(body))
            return self._send(404, {"error": {"code": "not_found"}})

        def _mcp(self, msg: dict[str, Any]) -> dict[str, Any]:
            mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "fake-cerata-fixture", "version": "0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": t, "inputSchema": {"type": "object"}} for t in TOOLS]}
            elif method == "tools/call":
                args = dict(params.get("arguments") or {})
                if params.get("name") == "preflight":
                    key = args.pop("idempotency_key", None)
                    _, _, out = state.preflight(args, key)
                elif params.get("name") == "verify":
                    consume = bool(args.pop("consume", False))
                    out = state.verify(args, consume)
                else:
                    out = {"error": {"code": "fixture_tool_not_modelled"}}
                result = {"content": [{"type": "text", "text": json.dumps(out)}]}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}

    return Handler


def serve(port: int, cases_path: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(State(cases_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8971)
    ap.add_argument("--cases", default=os.path.join(HERE, "cases.json"))
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(State(args.cases)))
    print(f"fake Cerata (fixture) listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
