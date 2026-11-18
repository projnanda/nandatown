"""LOOM portal over HTTP (standard library only).

    loom-portal --port 8787 --seed-credit cambridge-clinic=85

Endpoints (JSON in, JSON out):

    GET  /health
    POST /v1/<namespace>/requests   new access request
    POST /v1/<namespace>/fetch      fresh data under a grant
    GET  /v1/<namespace>/credit     publisher -> credit
    GET  /v1/<namespace>/log        every decision, in order (owner token; no data)
    GET  /v1/policy                 the built-in tasks (0.2.0)
    GET  /v1/<namespace>/proposals/<id>   status of a proposal (0.2.0)
    GET  /v1/<namespace>/owner/proposals  the person's queue   (owner token)
    POST /v1/<namespace>/owner/decide     approve / decline   (owner token)

A namespace is one isolated portal state (one person, one test run);
it is created on first use, with the configured seed credits.
"""

from __future__ import annotations

import argparse
import hmac
import os
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .policy import POLICY_VERSION, Portal, public_policy
from .record import synthetic_record

_NS = re.compile(r"^/v1/([A-Za-z0-9_.-]{1,80})/(requests|fetch|credit|log"
                 r"|owner/proposals|owner/decide|proposals/[A-Za-z0-9_-]{1,40})$")


class PortalStore:
    def __init__(self, seed_credit: dict[str, int] | None = None,
                 access_log: str | None = None,
                 owner_token: str | None = None):
        self.owner_token = owner_token
        self.seed_credit = dict(seed_credit or {})
        self.portals: dict[str, Portal] = {}
        self.lock = threading.Lock()
        self.access_log = access_log
        self.log_lock = threading.Lock()
        self.seq = 0

    def record_exchange(self, method: str, path: str, body, code: int,
                        out) -> None:
        """Append one HTTP exchange (request and response) as a JSON line.

        Evidence only: it lets a test show exactly what an agent sent and
        got back, independent of what the agent later reports."""
        if not self.access_log:
            return
        with self.log_lock:
            self.seq += 1
            line = {"seq": self.seq, "ts": time.time(), "method": method,
                    "path": path, "request": body, "status": code,
                    "response": out}
            with open(self.access_log, "a") as fh:
                fh.write(json.dumps(line) + "\n")

    def get(self, ns: str) -> Portal:
        if ns not in self.portals:
            self.portals[ns] = Portal(record=synthetic_record(),
                                      credit=dict(self.seed_credit))
        return self.portals[ns]


def make_handler(store: PortalStore):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, obj, req=None) -> None:
            store.record_exchange(self.command, self.path, req, code, obj)
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _is_owner(self) -> bool:
            got = self.headers.get("X-Loom-Owner-Token", "")
            return bool(store.owner_token) and hmac.compare_digest(
                got.encode(), store.owner_token.encode())

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True, "version": __version__,
                                        "policy": POLICY_VERSION})
            if self.path == "/v1/policy":
                return self._send(200, {"policy": POLICY_VERSION,
                                        "tasks": public_policy()})
            m = _NS.match(self.path)
            if not m or m.group(2) in ("requests", "fetch", "owner/decide"):
                return self._send(404, {"error": "not found"})
            what = m.group(2)
            if what in ("owner/proposals", "log") and not self._is_owner():
                return self._send(403, {"error": "owner token required"})
            with store.lock:
                p = store.get(m.group(1))
                if what == "credit":
                    out = p.credit
                elif what == "log":
                    out = p.log
                elif what == "owner/proposals":
                    out = list(p.proposals.values())
                else:
                    out = p.proposal(what.split("/", 1)[1])
                    if out is None:
                        return self._send(404, {"error": "unknown proposal"})
                return self._send(200, out)

        def do_POST(self):
            m = _NS.match(self.path)
            raw = None
            if not m or m.group(2) not in ("requests", "fetch", "owner/decide"):
                return self._send(404, {"error": "not found"})
            owner = m.group(2) == "owner/decide"
            if owner and not self._is_owner():
                return self._send(403, {"error": "owner token required"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(n).decode("utf-8", "replace")
                req = json.loads(raw or "{}")
                if not isinstance(req, dict):
                    raise ValueError("body must be a JSON object")
                if not owner and "request_id" not in req:
                    raise ValueError("missing required field(s): request_id")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"error": str(exc)}, raw)
            with store.lock:
                p = store.get(m.group(1))
                try:
                    if owner:
                        return self._send(200, p.owner_decide(req), req)
                    out = (p.request(req) if m.group(2) == "requests"
                           else p.fetch(req))
                except (KeyError, TypeError, ValueError) as exc:
                    msg = exc.args[0] if exc.args else str(exc)
                    return self._send(400, {"error": f"bad request: {msg}"},
                                      req)
            return self._send(409 if out["decision"] == "conflict" else 200,
                              out, req)
    return Handler


def serve(host: str = "127.0.0.1", port: int = 8787,
          seed_credit: dict[str, int] | None = None,
          access_log: str | None = None,
          owner_token: str | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(PortalStore(seed_credit, access_log, owner_token)))
    return server


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="loom-portal")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--seed-credit", action="append", default=[],
                    metavar="PUBLISHER=SCORE")
    ap.add_argument("--access-log", metavar="FILE",
                    help="append every HTTP request and response to FILE"
                         " (JSON lines)")
    ap.add_argument("--owner-token", default=os.environ.get("LOOM_OWNER_TOKEN"),
                    help="secret the person's app sends as X-Loom-Owner-Token"
                         " to review proposals (default: $LOOM_OWNER_TOKEN;"
                         " unset = owner endpoints disabled)")
    args = ap.parse_args(argv)
    seeds = {}
    for item in args.seed_credit:
        pub, _, score = item.partition("=")
        seeds[pub] = int(score)
    server = serve(args.host, args.port, seeds, args.access_log,
                   args.owner_token)
    print(f"loom-portal {__version__} ({POLICY_VERSION}) on"
          f" http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
