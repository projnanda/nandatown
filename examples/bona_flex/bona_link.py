"""A minimal MCP client over stdio (JSON-RPC 2.0, newline-delimited), standard library only.

Used by the `flex_desk` role to talk to the real Bona MCP server (`$BONA_PYTHON -m bona.server`). It speaks just
what the desk needs: `initialize`, `notifications/initialized`, `tools/list` and `tools/call`. Requests are sent
one at a time and answered in order (the desk is the only client of its server process).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Mapping, Sequence

PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    pass


class BonaLink:
    def __init__(self, cmd: Sequence[str], env: Mapping[str, str] | None = None, cwd: str | None = None,
                 timeout_s: float = 30.0):
        self.timeout_s = timeout_s
        self._id = 0
        self._stderr: list[bytes] = []
        self.proc = subprocess.Popen(list(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=dict(env) if env is not None else None, cwd=cwd)
        threading.Thread(target=self._drain, daemon=True).start()
        try:
            self.server_info = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "nandatown-bona-flex-desk", "version": "0.1"}})
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:      # never leave the server running when the handshake fails (QA 95e5d91 L5)
            self.proc.kill()
            self.proc.wait(timeout=5)
            raise

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self._stderr.append(line)

    def stderr_tail(self, n: int = 20) -> str:
        return b"".join(self._stderr[-n:]).decode("utf-8", "replace")

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg, separators=(",", ":")).encode() + b"\n")
        self.proc.stdin.flush()

    def _read(self) -> dict:
        result: list[bytes] = []
        t = threading.Thread(target=lambda: result.append(self.proc.stdout.readline()), daemon=True)
        t.start()
        t.join(self.timeout_s)
        if not result:
            raise McpError(f"no reply within {self.timeout_s} s; server stderr:\n{self.stderr_tail()}")
        if not result[0]:
            raise McpError(f"server closed its output; stderr:\n{self.stderr_tail()}")
        return json.loads(result[0])

    def _request(self, method: str, params: dict) -> dict:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            msg = self._read()
            if msg.get("id") != rid:      # server notifications / log messages: ignore
                continue
            if "error" in msg:
                raise McpError(f"{method}: {msg['error']}")
            return msg["result"]

    def list_tools(self) -> list[str]:
        return [t["name"] for t in self._request("tools/list", {})["tools"]]

    def call(self, name: str, arguments: dict) -> dict:
        r = self._request("tools/call", {"name": name, "arguments": arguments})
        if r.get("isError"):
            text = " ".join(c.get("text", "") for c in r.get("content", []))
            raise McpError(f"{name}: tool error: {text}")
        if "structuredContent" not in r:
            raise McpError(f"{name}: no structuredContent in the reply")
        return r["structuredContent"]

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait(timeout=5)

    def __enter__(self) -> BonaLink:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def bona_cmd() -> list[str]:
    py = os.environ.get("BONA_PYTHON")
    if not py:
        raise McpError("BONA_PYTHON is not set (the Python that has the Bona runtime)")
    return [py, "-m", "bona.server"]
