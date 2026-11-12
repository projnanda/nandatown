"""The example seller's dependency handling, without a town or a network.

These cover the three decisions that are easy to get wrong and expensive to
diagnose from a bundle: reading an abstention as an abstention rather than as
silence, reading a ranked answer, and surviving an acknowledgement the town
has already refused.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import threading
import urllib.error
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "graveyard_seller.py"

ABSTAIN = "graveyard: no known graves for this task (622 searched). Proceed normally."

RANKED = """graveyard pre-mortem for "tear down 3 T4 VMs": 10 past agent failures match (gpu-compute, provision-cloud).
Check these before acting, most relevant first:
1. [P0 runaway-cost, $10-100] GPU benchmark VMs: 200 GB boot disks kept billing after stop
   guard (verify-teardown): delete, don't just stop.
   applies when: Creating cloud VMs from CUDA images  (g_af0416cfb438)
"""


def _sse(payload: dict) -> bytes:
    return b"event: message\ndata: " + json.dumps(payload).encode() + b"\n\n"


class _Handler(http.server.BaseHTTPRequestHandler):
    text = ABSTAIN

    def do_POST(self):  # noqa: N802 - http.server's spelling
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("method") == "initialize":
            payload = {"result": {"protocolVersion": "2025-06-18",
                                  "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "stub",
                                                 "version": "0"}}}
        else:
            payload = {"result": {"content": [{"type": "text",
                                               "text": type(self).text}]}}
        raw = _sse(payload)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


@pytest.fixture
def stub():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/mcp", _Handler
    server.shutdown()
    server.server_close()


def load(monkeypatch, graveyard_url, **extra):
    """Import the example with a town environment it will not use."""
    monkeypatch.setenv("TOWN_URL", "http://127.0.0.1:1/unused")
    monkeypatch.setenv("RUN_ID", "run-test")
    monkeypatch.setenv("NAME", "seller")
    monkeypatch.setenv("TOKEN", "t")
    monkeypatch.setenv("GRAVEYARD_URL", graveyard_url)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("graveyard_seller", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_abstention_is_recorded_as_abstention(monkeypatch, stub):
    url, handler = stub
    handler.text = ABSTAIN
    seller = load(monkeypatch, url)
    check = seller.premortem("respond to a quote request")
    assert check["status"] == "consulted"
    assert check["graves"] == 0
    assert check["abstained"] is True
    assert check["grave_id"] == ""
    assert isinstance(check["latency_ms"], int)


def test_ranked_answer_keeps_the_count_tags_and_first_guard(monkeypatch, stub):
    url, handler = stub
    handler.text = RANKED
    seller = load(monkeypatch, url)
    check = seller.premortem("tear down 3 T4 VMs")
    assert check["status"] == "consulted"
    assert check["graves"] == 10
    assert check["abstained"] is False
    assert check["matched_tags"] == "gpu-compute, provision-cloud"
    assert check["grave_id"] == "g_af0416cfb438"
    assert "verify-teardown" in check["guard"]


def test_unreachable_service_never_raises(monkeypatch):
    # Port 9 discards; nothing listens, so the connection is refused.
    seller = load(monkeypatch, "http://127.0.0.1:9/mcp",
                  GRAVEYARD_TIMEOUT="0.5")
    check = seller.premortem("anything")
    assert check["status"] == "unreachable"
    assert check["abstained"] is None
    assert check["graves"] is None
    assert seller.handshake()["reachable"] is False


def test_a_refused_acknowledgement_is_a_lost_lease_not_a_crash(monkeypatch,
                                                              stub):
    url, _ = stub
    seller = load(monkeypatch, url)

    def refuse(*_args, **_kwargs):
        raise urllib.error.HTTPError(url, 409, "stale fence", {}, None)

    monkeypatch.setattr(seller, "call", refuse)
    assert seller.lost_lease("POST", "/inbox/ack", {"status": "retryable"}) is False


def test_an_accepted_acknowledgement_reports_success(monkeypatch, stub):
    url, _ = stub
    seller = load(monkeypatch, url)
    monkeypatch.setattr(seller, "call", lambda *a, **k: {})
    assert seller.lost_lease("POST", "/inbox/ack", {"status": "processed"}) is True
