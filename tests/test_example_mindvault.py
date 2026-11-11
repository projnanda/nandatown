"""The MindVault seller example against a mock MindVault (no key, no service).

The mock speaks the same MCP Streamable HTTP JSON-RPC as MindVault: an
initialize handshake, then tools/call for memory_read and memory_write. It
stores nothing real; it counts calls and answers like MindVault does
(b-701 readable by finance, deal notes labelled L2, intern denied with
LEVEL_TOO_LOW). Live results against the real service are in the example's
README.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nandatown.runner import run_town

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "integrations", "mindvault")
SELLER = os.path.join(EXAMPLE, "mv_seller.py")
PRICE_TEXT = ("Item: widget. Unit price: 1995 cents (USD). "
              "A quote for 2 widgets totals 3990 cents.")


class MockMindVault:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []  # (agent, tool, args)
        self.writes = 0
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                agent = self.headers.get("Authorization", "").removeprefix("Bearer ")
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in msg:  # notification
                    self.send_response(202)
                    self.end_headers()
                    return
                if msg["method"] == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {},
                              "serverInfo": {"name": "mock-mindvault", "version": "0"}}
                else:
                    tool = msg["params"]["name"]
                    args = msg["params"]["arguments"]
                    mock.calls.append((agent, tool, args))
                    out = mock.tool(agent, tool, args)
                    result = {"content": [{"type": "text", "text": json.dumps(out)}],
                              "structuredContent": out, "isError": False}
                body = json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                   "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "mock-session")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tool(self, agent, tool, args):
        if tool == "memory_read":
            if agent == "intern-key":
                return {"denied": True, "reason_code": "LEVEL_TOO_LOW",
                        "required_level": 2}
            return {"results": [{"block_id": "b-701", "level": 2,
                                 "status": "verified", "content": PRICE_TEXT}]}
        self.writes += 1
        return {"block_id": f"w-{self.writes:04d}", "level": 2,
                "status": "unverified", "level_basis": "session_max_read"}


@pytest.fixture
def mindvault(tmp_path, monkeypatch):
    mock = MockMindVault()
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"agents": [
        {"agent_id": "finance", "key": "finance-key"},
        {"agent_id": "intern", "key": "intern-key"}]}))
    # Command harnesses inherit the operator environment.
    monkeypatch.setenv("MINDVAULT_URL", mock.url)
    monkeypatch.setenv("MINDVAULT_AGENTS", str(agents))
    monkeypatch.setenv("MV_LOG", str(tmp_path / "mv-calls.jsonl"))
    yield mock
    mock.server.shutdown()


def run(profile, tmp_path):
    _, result = run_town(profile, str(tmp_path / "runs"),
                         external={"seller": [sys.executable, SELLER]})
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    return result


def test_off_mode_passes_and_never_calls_mindvault(tmp_path, monkeypatch, mindvault):
    monkeypatch.setenv("MEMORY", "off")
    run("quote-clean", tmp_path)
    assert mindvault.calls == []


def test_mindvault_mode_prices_from_the_l2_block_then_writes_one_note(
        tmp_path, monkeypatch, mindvault):
    monkeypatch.setenv("MEMORY", "mindvault")
    monkeypatch.setenv("PRICE_CENTS", "1")  # would make the total wrong if used
    run("quote-clean", tmp_path)
    assert [(a, t) for a, t, _ in mindvault.calls] == [
        ("finance-key", "memory_read"), ("finance-key", "memory_write")]
    assert mindvault.calls[0][2] == {"block_id": "b-701"}
    assert "3990 cents" in mindvault.calls[1][2]["content"]


def test_crash_restart_passes_but_writes_the_deal_note_twice(
        tmp_path, monkeypatch, mindvault):
    # The seller crashes after its MindVault write and before its journal
    # commit. Town absorbs the second reply; memory_write has no
    # idempotency key, so the restart stores a second note.
    monkeypatch.setenv("MEMORY", "mindvault")
    result = run("quote-crash-restart", tmp_path)
    assert {s.name: s.status for s in result.stages}["stale_fence_rejected"] == "passed"
    assert mindvault.writes == 2


def test_intern_probe_counts_level_denials(tmp_path, monkeypatch, mindvault):
    monkeypatch.setenv("MEMORY", "mindvault")
    run("quote-clean", tmp_path)
    out = tmp_path / "probe.json"
    subprocess.run([sys.executable, os.path.join(EXAMPLE, "intern_probe.py"),
                    "--log", os.environ["MV_LOG"], "--out", str(out)],
                   check=True, capture_output=True)
    probe = json.loads(out.read_text())
    assert (probe["notes"], probe["protected"], probe["leaked"]) == (1, 1, 0)
