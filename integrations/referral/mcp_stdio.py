"""A minimal synchronous MCP client over stdio, for Town's synchronous Lab.

MCP's stdio transport is newline-delimited JSON-RPC 2.0. This client starts
the server as a child process, performs the ``initialize`` handshake, and
then makes blocking ``tools/call`` requests with a deadline. It needs only
the standard library, so Town does not gain a dependency on the MCP SDK.

It is deliberately small: one request at a time, no server-initiated
requests, no resources or prompts. Notifications from the server are
ignored. A deadline, a dead process or a JSON-RPC error raises
``McpError``; a tool that reports ``isError`` raises ``McpToolError``.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "nandatown-referral-integration", "version": "0.1.0"}


class McpError(Exception):
    """The session could not start, a call timed out, or the server died."""


class McpToolError(McpError):
    """The server answered, but the tool reported an error."""


class StdioMcpClient:
    def __init__(self, command: list[str], env: dict[str, str] | None = None,
                 startup_timeout: float = 20.0, call_timeout: float = 30.0):
        if not command:
            raise McpError("no server command given")
        self.command = list(command)
        self.env = env
        self.startup_timeout = startup_timeout
        self.call_timeout = call_timeout
        self.server_info: dict[str, Any] = {}
        self.protocol_version = ""
        self.tools: list[str] = []
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> list[str]:
        """Start the server, initialize, and return its tool names."""
        env = dict(os.environ)
        env.update(self.env or {})
        try:
            self._proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
        except OSError as exc:
            raise McpError(f"cannot start server: {type(exc).__name__}") from None
        threading.Thread(target=self._read, daemon=True,
                         name="referral-mcp-reader").start()
        init = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, self.startup_timeout)
        self.server_info = dict(init.get("serverInfo") or {})
        self.protocol_version = str(init.get("protocolVersion", ""))
        self._notify("notifications/initialized", {})
        listed = self._request("tools/list", {}, self.startup_timeout)
        self.tools = sorted(t.get("name", "") for t in listed.get("tools", []))
        return self.tools

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=5)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- calls ----------------------------------------------------------

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """Call one tool and return its structured result as a dict."""
        result = self._request("tools/call",
                               {"name": tool, "arguments": arguments},
                               self.call_timeout)
        payload = result.get("structuredContent")
        if payload is None:
            text = "".join(part.get("text", "")
                           for part in result.get("content", [])
                           if part.get("type") == "text")
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError:
                payload = {"text": text}
        if isinstance(payload, dict) and set(payload) == {"result"} \
                and isinstance(payload["result"], dict):
            payload = payload["result"]
        if result.get("isError"):
            raise McpToolError(f"{tool}: {payload}")
        if not isinstance(payload, dict):
            payload = {"result": payload}
        return payload

    # -- JSON-RPC plumbing ---------------------------------------------

    def _read(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._lines.put(json.loads(line))
            except json.JSONDecodeError:
                continue  # not a protocol message; MCP forbids it, ignore it
        self._lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise McpError("server is not running")
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except OSError:
            raise McpError("server closed its input") from None

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any],
                 timeout: float) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                        "params": params})
            while True:
                try:
                    message = self._lines.get(timeout=timeout)
                except queue.Empty:
                    raise McpError(f"{method}: no answer within {timeout:g}s") from None
                if message is None:
                    raise McpError(f"{method}: server exited")
                if message.get("id") != rid or "method" in message:
                    continue  # a notification or a stale reply
                if "error" in message:
                    error = message["error"] or {}
                    raise McpError(f"{method}: {error.get('message', error)}")
                return message.get("result") or {}
