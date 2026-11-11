"""In-process mock of Bazaroo's external-agent API, for offline tests.

Mirrors the live contract: Bearer-key auth, case-insensitive unique names owned
by the first key that registers them, reserved built-in names, 201/409/401/400.
Optional `delay` makes every response slow, to exercise client timeouts.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

RESERVED = {"dara", "orin", "luna"}  # a few built-in specialist names


class MockBazaroo:
    def __init__(self, allowed_keys: set[str], delay: float = 0.0):
        self.allowed_keys = set(allowed_keys)
        self.delay = delay
        self.rows: dict[str, dict] = {}  # lower(name) -> row
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> "MockBazaroo":
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep test output quiet
                pass

            def _send(self, status: int, body: dict):
                raw = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except ConnectionError:  # covers BrokenPipe, ConnectionReset, ConnectionAborted (Windows)
                    pass  # client timed out and hung up; expected in timeout test

            def _key(self) -> str | None:
                h = self.headers.get("Authorization", "")
                if h.lower().startswith("bearer "):
                    k = h[7:].strip()
                    return k if k in mock.allowed_keys else None
                return None

            def do_POST(self):
                time.sleep(mock.delay)
                key = self._key()
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                if key is None:
                    return self._send(401, {"error": "unauthorized"})
                if urlparse(self.path).path != "/api/public/agents/register":
                    return self._send(404, {"error": "not_found"})
                try:
                    body = json.loads(raw)
                    name = body["name"].strip()
                    caps = body["capabilities"]
                    desc = body["description"].strip()
                    assert name and caps and desc
                except Exception:
                    return self._send(400, {"error": "invalid_body"})
                lname = name.lower()
                if lname in RESERVED:
                    return self._send(409, {"error": "name_taken"})
                row = mock.rows.get(lname)
                if row and row["owner"] != key:
                    return self._send(409, {"error": "name_taken"})
                if row:
                    row.update(capabilities=caps, description=desc)
                else:
                    row = dict(id=f"id-{len(mock.rows) + 1}", name=name, owner=key,
                               capabilities=caps, description=desc)
                    mock.rows[lname] = row
                self._send(201, {"agent_id": row["id"]})

            def do_GET(self):
                time.sleep(mock.delay)
                if self._key() is None:
                    return self._send(401, {"error": "unauthorized"})
                parsed = urlparse(self.path)
                if parsed.path != "/api/public/agents/search":
                    return self._send(404, {"error": "not_found"})
                q = (parse_qs(parsed.query).get("q", [""])[0]).strip().lower()
                if not q:
                    return self._send(400, {"error": "invalid_query"})
                hits = [
                    {"id": r["id"], "name": r["name"], "description": r["description"],
                     "capabilities": r["capabilities"], "source": "external"}
                    for r in mock.rows.values()
                    if any(q in s.lower() for s in [r["name"], r["description"], *r["capabilities"]])
                ]
                self._send(200, {"agents": hits})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
