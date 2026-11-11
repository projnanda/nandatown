"""A small synchronous MCP client over stdio, standard library only.

Town's Lab engine is synchronous, so each tool call blocks the simulation
until Rebbi answers; logical time does not move while it waits. One
session is one Rebbi MCP server process, started for one fleet agent:
Rebbi takes the agent's identity from REBBI_AGENT_ID, never from a tool
argument, so the adapter opens a session per agent it speaks for.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
MAX_LINE_BYTES = 1024 * 1024


class McpError(Exception):
    """The session could not start, a call failed, or the server is gone."""


class McpSession:
    def __init__(self, command: list[str], env: dict[str, str], cwd: str,
                 timeout: float = 30.0, stderr=subprocess.DEVNULL):
        self._timeout = timeout
        self._next_id = 0
        self._lines: queue.Queue = queue.Queue()
        self._process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=stderr, env=env, cwd=cwd)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.server_info: dict[str, Any] = {}
        self.protocol_version = ""
        self.tools: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        init = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "nandatown-rebbi-adapter",
                           "version": "1"},
        })
        self.server_info = init.get("serverInfo", {})
        self.protocol_version = init.get("protocolVersion", "")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = self._request("tools/list", {})
        self.tools = {t["name"]: t for t in listed.get("tools", [])}

    def accepts(self, tool: str, argument: str) -> bool:
        schema = self.tools.get(tool, {}).get("inputSchema", {})
        return argument in schema.get("properties", {})

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """One tools/call. A tool error raises McpError with its message."""
        result = self._request("tools/call",
                               {"name": tool, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content", [])
                       if c.get("type") == "text")
        if result.get("isError"):
            raise McpError(f"{tool}: {text[:300]}")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            inner = structured.get("result")
            if set(structured) == {"result"} and isinstance(inner, dict):
                return inner
            return structured
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpError(f"{tool}: reply is not JSON") from exc
        if not isinstance(decoded, dict):
            raise McpError(f"{tool}: reply is not a JSON object")
        return decoded

    def close(self) -> None:
        process = self._process
        if process.poll() is not None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5)

    # -- plumbing -------------------------------------------------------

    def _read(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None
        try:
            while True:
                line = stdout.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None or self._process.poll() is not None:
            raise McpError("server is not running")
        try:
            stdin.write((json.dumps(message) + "\n").encode())
            stdin.flush()
        except OSError as exc:
            raise McpError("server closed its input") from exc

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": params})
        while True:
            try:
                line = self._lines.get(timeout=self._timeout)
            except queue.Empty:
                raise McpError(f"{method}: no reply in {self._timeout}s") \
                    from None
            if line is None:
                raise McpError(f"{method}: server closed its output")
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise McpError(f"{method}: malformed JSON reply") from None
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue  # a notification, or a reply to something else
            if "error" in message:
                error = message["error"] or {}
                raise McpError(f"{method}: {error.get('message', error)}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise McpError(f"{method}: reply has no result object")
            return result
