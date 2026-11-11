"""GridPact behind an A2A endpoint, so Town's Path mode can test it.

GridPact (https://github.com/nh13120/gridpact, tag hw8-v1) is a peer-to-peer energy-trading protocol:
agents trade over an MCP server with a locked stake, a ledger and an auditor. This adapter is the only
Town-side code. It makes no trading decisions of its own. For each Town quote request it drives a real
GridPact server through its MCP tools (propose, accept) and answers with what GridPact recorded.

Town's synthetic order  ->  GridPact trade
  quantity (widgets)        MWh of energy, delivered over steps 68-79 (17:00-19:45, 12 steps)
  unit_price_cents          price in cents per MWh ($/MWh = cents / 100)
  request_id                propose(request_id=...), GridPact's idempotency key
  total_cents               value of the commitment GridPact created (kWh/step x steps x price)
  commitment_id             GridPact's own id for that commitment (so a double-booking is visible)

Run (needs a fresh GridPact server; it hands out each agent's token only once):
    GRIDPACT_DATA_DIR=fixtures/synthetic python -m gridpact.server        # in the gridpact checkout
    python examples/gridpact/gridpact_a2a.py --port 8941                   # in this checkout
Then:  nandatown test-agent --url http://127.0.0.1:8941 --path-profile a2a-capability-fulfillment@0.3

Only httpx, fastapi and uvicorn, which Town already depends on. This is a scripted adapter: no LLM agent
takes part, and the Track and Lab modes are not used.
"""
from __future__ import annotations

import argparse
import json
import threading
import uuid

import httpx
from fastapi import FastAPI, Request

BUYER, SELLER = "COM_09", "PVB_17"
STEP_FROM, STEP_TO = 68, 79
PENALTY_USD_MWH = 60.0
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


class GridPactClient:
    """Just enough MCP over streamable HTTP (JSON-RPC, replies as server-sent events) to call tools."""

    def __init__(self, url: str, timeout: float = 20.0):
        self.url, self.http, self.n, self.session = url, httpx.Client(timeout=timeout), 0, None
        self.lock = threading.Lock()
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "nandatown-gridpact-a2a", "version": "0"}})
        self.http.post(self.url, headers=self._headers(), json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = {t["name"]: t for t in self._rpc("tools/list", {})["tools"]}

    def _headers(self):
        return {**HEADERS, **({"Mcp-Session-Id": self.session, "MCP-Protocol-Version": "2025-06-18"} if self.session else {})}

    def _rpc(self, method, params):
        self.n += 1
        r = self.http.post(self.url, headers=self._headers(), json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        r.raise_for_status()
        self.session = r.headers.get("mcp-session-id", self.session)
        body = next((ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")), r.text)
        msg = json.loads(body)
        if "error" in msg:
            raise RuntimeError(f"MCP error: {msg['error']}")
        return msg["result"]

    def call(self, name: str, **args) -> dict:
        with self.lock:
            res = self._rpc("tools/call", {"name": name, "arguments": args})
        return json.loads(res["content"][0]["text"])

    def supports_request_id(self) -> bool:
        return "request_id" in self.tools["propose"]["inputSchema"].get("properties", {})


class GridPactSeller:
    def __init__(self, mcp_url: str):
        self.gp = GridPactClient(mcp_url)
        self.idempotent = self.gp.supports_request_id()
        self.tokens = {}
        for agent, role in ((BUYER, "buyer"), (SELLER, "seller")):
            r = self.gp.call("register_agent", agent_id=agent, role=role)
            if not r["ok"]:
                raise SystemExit(f"GridPact refused to register {agent}: {r['reason']}. "
                                 "Start a fresh GridPact server (tokens are only issued once).")
            self.tokens[agent] = r["token"]
        self.lock = threading.Lock()

    def _commitment_for_offer(self, offer_id: int) -> dict | None:
        for e in self.gp.call("get_ledger", token=self.tokens[BUYER], limit=1000)["entries"]:
            if e["action"] == "commit" and e["payload"]["offer_id"] == offer_id:
                return e["payload"]
        return None

    def quote(self, req: dict) -> tuple[str, dict]:
        request_id = req.get("request_id")
        if req.get("sku") != "widget" or not all(isinstance(req.get(k), int) and req[k] > 0
                                                 for k in ("quantity", "unit_price_cents")):
            raise ValueError("expected {sku: 'widget', quantity: int > 0, unit_price_cents: int > 0}")
        n_steps = STEP_TO - STEP_FROM + 1
        kwh_per_step = req["quantity"] * 1000.0 / n_steps
        args = dict(token=self.tokens[BUYER], to=SELLER, step_from=STEP_FROM, step_to=STEP_TO, kwh_per_step=kwh_per_step,
                    price_usd_mwh=req["unit_price_cents"] / 100.0, penalty_usd_mwh=PENALTY_USD_MWH)
        if self.idempotent and isinstance(request_id, str):
            args["request_id"] = request_id
        with self.lock:
            offer = self.gp.call("propose", **args)
            if not offer["ok"]:
                state = "rejected" if "different terms" in offer["reason"] else "failed"
                return state, {"request_id": request_id, "error": offer["reason"]}
            if offer.get("duplicate") and offer.get("status") == "accepted":
                commit = self._commitment_for_offer(offer["offer_id"])
            else:
                r = self.gp.call("respond", token=self.tokens[SELLER], offer_id=offer["offer_id"], action="accept")
                if not r["ok"]:
                    return "failed", {"request_id": request_id, "error": r["reason"]}
                commit = self._commitment_for_offer(offer["offer_id"])
        total = commit["kwh_per_step"] * n_steps / 1000.0 * commit["price_usd_mwh"]
        return "completed", {"request_id": request_id, "total_cents": round(total * 100),
                             "commitment_id": commit["commitment_id"], "seller": commit["seller"],
                             "stake_usd": commit["stake_usd"]}


def build_app(mcp_url: str, base_url: str) -> FastAPI:
    app = FastAPI(title="GridPact A2A adapter")
    seller = GridPactSeller(mcp_url)
    tasks: dict[str, dict] = {}
    card = {"name": "gridpact-quote-seller", "version": "hw8-v1", "url": base_url,
            "description": "GridPact energy seller. A quote request becomes a GridPact trade: propose, accept,"
                           " stake locked, commitment on a hash-chained ledger.",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text"], "defaultOutputModes": ["text"],
            "skills": [{"id": "quote", "name": "Quote",
                        "description": "JSON {request_id, sku: 'widget', quantity, unit_price_cents} -> JSON"
                                       " {request_id, total_cents, commitment_id}. A widget is 1 MWh delivered"
                                       " over 12 fifteen-minute steps; unit_price_cents is cents per MWh.",
                        "tags": ["quote", "energy", "gridpact"]}]}

    @app.get("/.well-known/agent-card.json")
    @app.get("/.well-known/agent.json")
    def agent_card():
        return card

    @app.post("/")
    async def rpc(request: Request):
        msg = await request.json()
        mid, method = msg.get("id"), msg.get("method")
        if method == "message/send":
            parts = msg.get("params", {}).get("message", {}).get("parts", [])
            text = next((p.get("text", "") for p in parts if p.get("kind") == "text"), "{}")
            try:
                state, body = seller.quote(json.loads(text))
                name = "quote" if state == "completed" else "error"
            except (ValueError, KeyError, TypeError, RuntimeError, httpx.HTTPError) as exc:
                state, body, name = "failed", f"bad request or GridPact unavailable: {exc}", "error"
            tid = "task-" + uuid.uuid4().hex[:12]
            task = {"id": tid, "contextId": "ctx-" + tid, "status": {"state": state}, "kind": "task",
                    "artifacts": [{"artifactId": "a-1", "name": name,
                                   "parts": [{"kind": "text", "text": body if isinstance(body, str) else json.dumps(body)}]}]}
            tasks[tid] = task
            return {"jsonrpc": "2.0", "id": mid, "result": task}
        if method == "tasks/get":
            task = tasks.get(msg.get("params", {}).get("id", ""))
            if task:
                return {"jsonrpc": "2.0", "id": mid, "result": task}
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32001, "message": "task not found"}}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method {method!r} not found"}}

    return app


def main():
    import uvicorn
    ap = argparse.ArgumentParser(prog="gridpact_a2a")
    ap.add_argument("--port", type=int, default=8941)
    ap.add_argument("--mcp-url", default="http://127.0.0.1:8765/mcp")
    a = ap.parse_args()
    uvicorn.run(build_app(a.mcp_url, f"http://127.0.0.1:{a.port}"), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
