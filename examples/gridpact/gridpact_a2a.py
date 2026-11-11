"""GridPact behind an A2A endpoint, so Town's Path mode can test it.

GridPact (https://github.com/nh13120/gridpact, tag hw8-v1) is a peer-to-peer energy-trading protocol:
agents trade over an MCP server with a locked stake, a ledger and an auditor. This adapter is the only
Town-side code. It makes no trading decisions of its own. For each Town quote request it drives a real
GridPact server through its MCP tools (propose, accept) and answers with what GridPact recorded.

Town's synthetic order  ->  GridPact trade
  quantity (widgets)        MWh of energy, delivered over `steps` 15-minute steps from 17:00 (default 12)
  unit_price_cents          price in cents per MWh ($/MWh = cents / 100)
  request_id                propose(request_id=...), GridPact's idempotency key
  total_cents               value of the commitment GridPact created (kWh/step x steps x price)
  commitment_id             GridPact's own id for that commitment (so a double-booking is visible)
After the trade the adapter plays the rest of a GridPact day for that commitment: an auditor agent checks the
seller's declared capacity against its nameplate and challenges a lie; GridPact settles delivery (operator tool,
needs GRIDPACT_OPERATOR_KEY on the server); the quote reports the audit verdict and delivered/shortfall kWh.

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

BUYER, AUDITOR = "COM_09", "AUD_1"
STEP_FROM = 68                        # 17:00 local; the window is STEP_FROM .. STEP_FROM + steps - 1
PENALTY_USD_MWH = 60.0
TOLERANCE = 1.10                      # GridPact's rule: declared capacity may exceed nameplate by 10%
# Public registry nameplates (kW) for GridPact's synthetic fixture. The auditor is handed these, as in the HW7 benchmark.
NAMEPLATES = {"PVB_17": 5000.0, "PVB_37": 2500.0, "PVB_32": 3000.0, "PVB_50": 6000.0}
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
    def __init__(self, mcp_url: str, seller: str = "PVB_17", declared_flex_kw: float | None = None,
                 operator_key: str | None = None, steps: int = 12):
        self.gp = GridPactClient(mcp_url)
        self.seller, self.operator_key, self.steps = seller, operator_key, steps
        self.idempotent = self.gp.supports_request_id()
        self.can_settle = bool(operator_key) and "operator_settle" in self.gp.tools
        self.tokens = {}
        for agent, role, flex in ((BUYER, "buyer", None), (seller, "seller", declared_flex_kw), (AUDITOR, "auditor", None)):
            args = {"agent_id": agent, "role": role}
            if flex is not None:
                args["declared_flex_kw"] = flex
            r = self.gp.call("register_agent", **args)
            if not r["ok"]:
                raise SystemExit(f"GridPact refused to register {agent}: {r['reason']}. "
                                 "Start a fresh GridPact server (tokens are only issued once).")
            self.tokens[agent] = r["token"]
        self.done: dict[int, dict] = {}        # commitment_id -> the quote already given for it
        self.lock = threading.Lock()

    def _commitment_for_offer(self, offer_id: int) -> dict | None:
        for e in self.gp.call("get_ledger", token=self.tokens[BUYER], limit=1000)["entries"]:
            if e["action"] == "commit" and e["payload"]["offer_id"] == offer_id:
                return e["payload"]
        return None

    def _audit_and_settle(self, commit: dict, ordered_kwh: float) -> dict:
        """The rest of a GridPact day for one commitment: audit before settlement, then settle delivery."""
        seller = commit["seller"]
        declared = next(c["declared_flex_kw"] for c in self.gp.call("find_counterparties", token=self.tokens[BUYER])["counterparties"]
                        if c["agent_id"] == seller)
        nameplate = NAMEPLATES.get(seller)
        info = {"audit": "not challenged"}
        if nameplate and declared > TOLERANCE * nameplate:
            v = self.gp.call("challenge", token=self.tokens[AUDITOR], commitment_id=commit["commitment_id"],
                             claim="declared capacity exceeds nameplate",
                             evidence=f"declared {declared:.0f} kW vs nameplate {nameplate:.0f} kW")
            info["audit"] = f"challenge {v.get('verdict')}"
            info["bounty_usd"] = v.get("bounty_usd")
        if self.can_settle:
            r = self.gp.call("operator_settle", operator_key=self.operator_key)
            line = next((x for x in r.get("settled", []) if x["commitment_id"] == commit["commitment_id"]), None)
            if line:
                info.update(ordered_kwh=round(ordered_kwh, 1), delivered_kwh=line["delivered_kwh"],
                            shortfall_kwh=line["shortfall_kwh"], penalty_usd=line["penalty_usd"])
        return info

    def quote(self, req: dict) -> tuple[str, dict]:
        request_id = req.get("request_id")
        if req.get("sku") != "widget" or not all(isinstance(req.get(k), int) and req[k] > 0
                                                 for k in ("quantity", "unit_price_cents")):
            raise ValueError("expected {sku: 'widget', quantity: int > 0, unit_price_cents: int > 0}")
        n_steps = self.steps
        kwh_per_step = req["quantity"] * 1000.0 / n_steps
        args = dict(token=self.tokens[BUYER], to=self.seller, step_from=STEP_FROM, step_to=STEP_FROM + n_steps - 1, kwh_per_step=kwh_per_step,
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
                r = self.gp.call("respond", token=self.tokens[self.seller], offer_id=offer["offer_id"], action="accept")
                if not r["ok"]:
                    return "failed", {"request_id": request_id, "error": r["reason"]}
                commit = self._commitment_for_offer(offer["offer_id"])
            cid = commit["commitment_id"]
            if cid not in self.done:                       # a repeat of an order gets the same answer
                total = commit["kwh_per_step"] * n_steps / 1000.0 * commit["price_usd_mwh"]
                self.done[cid] = {"request_id": request_id, "total_cents": round(total * 100), "commitment_id": cid,
                                  "seller": commit["seller"], "stake_usd": commit["stake_usd"],
                                  **self._audit_and_settle(commit, commit["kwh_per_step"] * n_steps)}
            return "completed", self.done[cid]


def build_app(mcp_url: str, base_url: str, **seller_opts) -> FastAPI:
    app = FastAPI(title="GridPact A2A adapter")
    seller = GridPactSeller(mcp_url, **seller_opts)
    tasks: dict[str, dict] = {}
    card = {"name": "gridpact-quote-seller", "version": "hw8-v1", "url": base_url,
            "description": "GridPact energy seller. A quote request becomes a GridPact trade: propose, accept,"
                           " stake locked, commitment on a hash-chained ledger.",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text"], "defaultOutputModes": ["text"],
            "skills": [{"id": "quote", "name": "Quote",
                        "description": "JSON {request_id, sku: 'widget', quantity, unit_price_cents} -> JSON"
                                       " {request_id, total_cents, commitment_id, audit, delivered_kwh, ...}. A widget"
                                       " is 1 MWh delivered over 12 fifteen-minute steps by default; unit_price_cents is cents per MWh.",
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
    ap.add_argument("--seller", default="PVB_17", help="GridPact seller that fills the orders (PVB_37 for the liar case)")
    ap.add_argument("--steps", type=int, default=12, help="delivery window in 15-minute steps (2 packs the order into 17:00-17:30)")
    ap.add_argument("--declared-flex-kw", type=float, default=None, help="capacity the seller declares (a lie if above nameplate)")
    a = ap.parse_args()
    import os
    uvicorn.run(build_app(a.mcp_url, f"http://127.0.0.1:{a.port}", seller=a.seller, declared_flex_kw=a.declared_flex_kw, steps=a.steps,
                          operator_key=os.environ.get("GRIDPACT_OPERATOR_KEY")), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
