"""A stand-in outlet agent for offline runs. It is a fixture, not the project.

It speaks the same three calls as the real outlet agent (agent card,
A2A SendMessage, package download) and signs with a fixed test key, so
the Town roles and the validator run without a network, a model or a
credential. Its story and outlet are invented.

Like the real agent, it builds a fresh package for every message it
receives: the package carries its issue time, so two answers to the same
request have two different ids. `--idempotent` returns the first package
again for a repeated messageId, which is the behaviour the duplicate
scenario asks for.

    python examples/signed_answers/stub_outlet.py --port 8970
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

STUB_TOKEN = "stub-subscriber-credential"  # a fixture value, grants nothing real
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_SEED = hashlib.sha256(b"signed-answers stub outlet, test key only").digest()

SENTENCES = [
    "The Harbor Gazette reported on March 3 that the transit board cut 12 night routes.",
    "The board's own minutes put the saving at $4.1 million a year.",
    "A later correction said 12 routes were cut, not 21 as first published.",
]


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _b58(data: bytes) -> str:
    number, out = int.from_bytes(data, "big"), ""
    while number:
        number, rem = divmod(number, 58)
        out = _B58[rem] + out
    return out


class StubOutlet:
    def __init__(self, idempotent: bool = False):
        self.private = Ed25519PrivateKey.from_private_bytes(_SEED)
        raw = self.private.public_key().public_bytes(Encoding.Raw,
                                                     PublicFormat.Raw)
        self.did = "did:key:z" + _b58(b"\xed\x01" + raw)
        self.idempotent = idempotent
        self.packages: dict[str, dict[str, Any]] = {}
        self.by_message: dict[str, str] = {}
        self.calls = 0
        self.lock = threading.Lock()

    def envelope(self, base: str = "http://127.0.0.1:0") -> dict[str, Any]:
        self.calls += 1
        package = {
            "schema": "source-package/stub",
            "result": "match",
            "publisher": {"name": "Harbor Gazette (invented)", "url": base,
                          "agent_key": self.did},
            # Issue time is part of the signed content, as in the real agent.
            "issued_at": f"2026-01-01T00:00:{self.calls:02d}Z",
            "story": {"title": "Transit board cuts night routes",
                      "url": f"{base}/stories/night-routes",
                      "published": "2025-03-03T09:00:00Z"},
            "correction": {"kind": "correction",
                           "note": "12 routes were cut, not 21.",
                           "applies_to_sentence": 1},
            "sentences": [{"n": i + 1, "text": text, "sources": []}
                          for i, text in enumerate(SENTENCES)],
        }
        signature = self.private.sign(canonical(package))
        return {
            "package_id": "df://sha256-"
                          + hashlib.sha256(canonical(package)).hexdigest(),
            "package": package,
            "signature": {
                "alg": "Ed25519", "key": self.did,
                "value": base64.urlsafe_b64encode(signature).decode().rstrip("="),
            },
        }

    def answer(self, message_id: str, subscribed: bool, base: str) -> str:
        if not subscribed:
            return ("Signed source packages are for subscribers, and this"
                    " request carried no subscriber credential.\n"
                    "- Transit board cuts night routes (March 3, 2025)")
        with self.lock:
            key = self.by_message.get(message_id) if self.idempotent else None
            if key is None:
                envelope = self.envelope(base)
                key = envelope["package_id"].removeprefix("df://")
                self.packages[key] = envelope
                self.by_message[message_id] = key
        return (f"Here is a signed source package.\n"
                f"Package: {base}/packages/{key}\n"
                f"The package is signed by {self.did}.")


def make_server(port: int = 0, idempotent: bool = False) -> ThreadingHTTPServer:
    outlet = StubOutlet(idempotent)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep test output quiet
            pass

        def _base(self) -> str:
            return f"http://127.0.0.1:{self.server.server_address[1]}"

        def _json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _subscribed(self) -> bool:
            return self.headers.get("Authorization", "") == f"Bearer {STUB_TOKEN}"

        def do_GET(self):  # noqa: N802
            if self.path == "/.well-known/agent-card.json":
                self._json(200, {"name": "Stub outlet agent",
                                 "url": f"{self._base()}/a2a",
                                 "signing": {"alg": "Ed25519",
                                             "key": outlet.did}})
            elif self.path == "/stories/night-routes":
                # The published story, with markup the reader must see through.
                body = ("<html><body><article><p>"
                        + "</p>\n<p>".join(
                            s.replace("transit board",
                                      "<a href='/board'>transit board</a>")
                            .replace("'", "&rsquo;")
                            for s in SENTENCES)
                        + "</p></article></body></html>").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/packages/"):
                envelope = outlet.packages.get(
                    self.path.removeprefix("/packages/"))
                if not self._subscribed():
                    self._json(401, {"error": "subscriber credential required"})
                elif envelope is None:
                    self._json(404, {"error": "unknown package"})
                else:
                    self._json(200, envelope)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            try:
                rpc = json.loads(self.rfile.read(length))
                message = rpc["params"]["message"]
                message_id = str(message["messageId"])
            except (ValueError, KeyError, TypeError):
                self._json(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700,
                                           "message": "parse error"}})
                return
            text = outlet.answer(message_id, self._subscribed(), self._base())
            self._json(200, {"jsonrpc": "2.0", "id": rpc.get("id"), "result": {
                "task": {"id": message_id,
                         "contextId": message.get("contextId"),
                         "status": {"state": "TASK_STATE_COMPLETED",
                                    "message": {"role": "ROLE_AGENT",
                                                "parts": [{"text": text}]}}}}})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.outlet = outlet
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8970)
    parser.add_argument("--idempotent", action="store_true")
    args = parser.parse_args()
    server = make_server(args.port, args.idempotent)
    print(f"stub outlet on http://127.0.0.1:{server.server_address[1]}"
          f" (key {server.outlet.did})")
    server.serve_forever()


if __name__ == "__main__":
    main()
