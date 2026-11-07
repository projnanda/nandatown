"""A small client for SimOps AI over HTTP, using only httpx (a Town dependency).

SimOps runs elsewhere. This file speaks two of its interfaces:

- MCP (streamable HTTP) at <SIMOPS_URL>/mcp, the seat tools any agent uses. The bearer token decides which
  trade a call plays; SimOps never takes the trade from an argument.
- The playground routes at <SIMOPS_URL>/api/playground, which open a fresh Week 9 run with one seat token per
  trade, close its week, and return its log and SimOps's own audit.

Site is the construction manager's side of a run: it opens the run before the Lab starts, hands each trade
its own token out of band (the way SimOps invites outside agents), and keeps the run key that reads the
audit. One Site per Lab run, kept in SITES by the Lab run id so run_check.py can read it afterwards.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8765"
PROTOCOL = "2025-06-18"
SITES: dict[str, "Site"] = {}


class Unreachable(Exception):
    """SimOps did not answer: refused connection, timeout, or a server error."""


def base_url(url: str | None = None) -> str:
    url = (url or os.environ.get("SIMOPS_URL") or DEFAULT_URL).rstrip("/")
    return url[:-4] if url.endswith("/mcp") else url


class SeatSession:
    """One MCP session for one seat token. Tool results come back as SimOps's own dicts."""

    def __init__(self, base: str, token: str, timeout: float = 30.0):
        self.url = base + "/mcp"
        self.token = token
        self.http = httpx.Client(timeout=timeout)
        self.session_id: str | None = None
        self.next_id = 0

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            return self.http.post(self.url, headers=headers, content=json.dumps(payload))
        except httpx.HTTPError as e:
            raise Unreachable(f"{type(e).__name__}: {e}") from e

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        r = self._post({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        if r.status_code == 401:
            raise PermissionError(r.json().get("explain", "unauthorized") if r.content else "unauthorized")
        if r.status_code >= 500:
            raise Unreachable(f"HTTP {r.status_code}")
        r.raise_for_status()
        if "mcp-session-id" in r.headers:
            self.session_id = r.headers["mcp-session-id"]
        message = _rpc_message(r, self.next_id)
        if "error" in message:
            raise RuntimeError(f"MCP error: {message['error']}")
        return message["result"]

    def open(self) -> None:
        if self.session_id is None:
            self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                     "clientInfo": {"name": "nandatown-simops-desk", "version": "0.1.0"}})
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tools(self) -> list[dict[str, Any]]:
        """SimOps's own tool list, as MCP serves it: [{name, description, inputSchema}]."""
        self.open()
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.open()
        result = self._rpc("tools/call", {"name": tool, "arguments": args})
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        return json.loads(result["content"][0]["text"])


def _rpc_message(r: httpx.Response, want: int) -> dict[str, Any]:
    """The JSON-RPC response with id `want`, from a JSON body or an event stream."""
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[5:].strip())
                if msg.get("id") == want:
                    return msg
        raise RuntimeError("no response in the event stream")
    return r.json()


class Seats:
    """Calls a SimOps tool with a given seat token, one MCP session per token."""

    def __init__(self, base: str):
        self.base = base
        self.sessions: dict[str, SeatSession] = {}

    def call(self, token: str | None, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if not token:
            return refusal("unauthorized", "No seat token came with the request.")
        session = self.sessions.setdefault(token, SeatSession(self.base, token))
        try:
            return session.call(tool, args)
        except PermissionError as e:
            self.sessions.pop(token, None)
            return refusal("unauthorized", str(e))


    def tools(self, token: str) -> list[dict[str, Any]]:
        return self.sessions.setdefault(token, SeatSession(self.base, token)).tools()


class Model:
    """One OpenAI Responses API conversation with SimOps's tools as function tools. The only place a model is
    called; used by the optional live-seat case and only when OPENAI_API_KEY is set."""

    URL = "https://api.openai.com/v1/responses"
    PRICES = {"gpt-6-luna": (0.10, 0.50), "gpt-6-sol": (2.00, 10.00)}  # USD per million tokens, in / out

    def __init__(self, model: str, instructions: str, tools: list[dict[str, Any]], timeout: float = 120.0):
        self.model, self.instructions = model, instructions
        self.tools = [{"type": "function", "name": t["name"], "description": t.get("description", ""),
                       "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}} for t in tools]
        self.http = httpx.Client(timeout=timeout)
        self.previous: str | None = None
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def step(self, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        """Send input items; return (function calls [{call_id, name, arguments}], any text)."""
        body = {"model": self.model, "instructions": self.instructions, "input": items, "tools": self.tools,
                "parallel_tool_calls": False}
        if self.previous:
            body["previous_response_id"] = self.previous
        r = self.http.post(self.URL, headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                           json=body)
        r.raise_for_status()
        out = r.json()
        self.previous = out["id"]
        u = out.get("usage") or {}
        self.usage["calls"] += 1
        self.usage["input_tokens"] += u.get("input_tokens", 0)
        self.usage["output_tokens"] += u.get("output_tokens", 0)
        calls = [{"call_id": o["call_id"], "name": o["name"], "arguments": json.loads(o.get("arguments") or "{}")}
                 for o in out.get("output", []) if o.get("type") == "function_call"]
        text = " ".join(c.get("text", "") for o in out.get("output", []) if o.get("type") == "message"
                        for c in o.get("content", []) if c.get("type") == "output_text")
        return calls, text

    def cost_usd(self) -> float:
        pin, pout = self.PRICES.get(self.model, (10.0, 50.0))
        return round((self.usage["input_tokens"] * pin + self.usage["output_tokens"] * pout) / 1e6, 6)


def refusal(reason: str, explain: str) -> dict[str, Any]:
    return {"ok": False, "status": "rejected", "reason": reason, "explain": explain}


class Playground:
    def __init__(self, base: str, timeout: float = 30.0):
        self.base = base
        self.http = httpx.Client(timeout=timeout)

    def _do(self, method: str, path: str, key: str | None = None, body: dict | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            r = self.http.request(method, self.base + path, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise Unreachable(f"{type(e).__name__}: {e}") from e
        if r.status_code >= 500:
            raise Unreachable(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    def open(self, case: str) -> dict[str, Any]:
        return self._do("POST", "/api/playground", body={"case": case})

    def read(self, run: str, key: str) -> dict[str, Any]:
        return self._do("GET", f"/api/playground/{run}", key=key)

    def close(self, run: str, key: str) -> dict[str, Any]:
        return self._do("POST", f"/api/playground/{run}/close", key=key)


class Site:
    """The construction manager's side of one Lab run: the SimOps run, its seat tokens and its key."""

    def __init__(self, lab_run: str, case: str, url: str | None = None):
        self.lab_run, self.case = lab_run, case
        self.base = base_url(url)
        self.error: str | None = None
        self.opened: dict[str, Any] = {}
        self.audit: dict[str, Any] | None = None
        self.closed: dict[str, Any] | None = None
        self.playground = Playground(self.base)
        self.seats = Seats(self.base)
        try:
            self.opened = self.playground.open(case)
        except (Unreachable, RuntimeError) as e:
            self.error = str(e)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def run(self) -> str | None:
        return self.opened.get("run")

    def token(self, trade: str) -> str | None:
        """The out-of-band invite: a trade's own seat token, handed only to that trade."""
        return self.opened.get("seats", {}).get(trade)

    def public(self) -> dict[str, Any]:
        """What may go in the Town record: no tokens, no key."""
        o = self.opened
        return {"simops_run": o.get("run"), "case": o.get("case", self.case), "title": o.get("title"),
                "week": o.get("week"), "trades": o.get("trades", []), "simops": o.get("simops"),
                "endpoint": "<SIMOPS_URL>/mcp"}

    def close_week(self) -> dict[str, Any]:
        self.closed = self.playground.close(self.run, self.opened["key"])
        return self.closed

    def read(self) -> dict[str, Any]:
        self.audit = self.playground.read(self.run, self.opened["key"])
        return self.audit


def site_for(lab_run: str, case: str) -> Site:
    """The Site of a Lab run, opened on first use."""
    if lab_run not in SITES:
        SITES[lab_run] = Site(lab_run, case)
    return SITES[lab_run]
