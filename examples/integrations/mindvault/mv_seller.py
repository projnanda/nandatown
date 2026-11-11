#!/usr/bin/env python3
"""A Town seller whose price list lives in MindVault (MAS.665 HW8).

MindVault is a shared memory service for agents with access levels: every
note carries a level (L0 public .. L2 finance), an agent only reads notes at
or below its own level, and a note an agent writes is labelled with the
highest level that agent has read (session taint). Every write is checked by
an LLM reviewer before it is stored. MindVault runs as its own service;
this file is only the integration.

One switch:

    MEMORY=mindvault  read the price from MindVault block b-701 (L2, finance
                      key), reply to the buyer, then write a deal note to
                      MindVault. MindVault labels it L2 from session taint.
    MEMORY=off        read the same price from PRICE_CENTS (default 1995)
                      and write nothing. The baseline.

Town contract, same as examples/byoa_seller.py: join, claim under a lease,
reply, acknowledge. Standard library only, so Town gains no dependency;
MindVault is reached over plain MCP Streamable HTTP (JSON-RPC).

Order per request: read the price, write the deal note to MindVault, reply
to the buyer, record in the local journal, ack. The note is written BEFORE
the reply because MindVault reviews every write synchronously with an LLM
(1.5 to 9 s observed). Town's runner gives a seller only ~8 s after the
buyer has its answer to acknowledge; writing after the reply made every
MindVault crash-restart run INCOMPLETE. Before the reply, the review time
falls inside the buyer's 45 s deadline instead.

FAULT=crash_after_claim (profile quote-crash-restart): on the first claim the
seller reads the price and writes the deal note, then "crashes" before
replying: it stalls past its lease, proves the stale fence is refused, and
exits 3. The runner restarts it; Town redelivers. The restarted seller has
no journal entry, so it applies the work again. MindVault's memory_write
has no idempotency key, so the restart writes a second deal note. That is
the edge case this run measures, not hides.

Environment (Town sets the first six; the rest are ours):
    TOWN_URL RUN_ID NAME TOKEN STATE_DIR FAULT DEADLINE
    MEMORY               mindvault | off            (default off)
    MINDVAULT_URL        e.g. http://127.0.0.1:8000/mcp
    MINDVAULT_AGENTS     path to MindVault's config/agents.json
    MINDVAULT_AGENT      agent id to act as         (default finance)
    PRICE_BLOCK          block id of the price list (default b-701)
    PRICE_CENTS          price used when MEMORY=off (default 1995)
    MV_LOG               optional JSONL file: one line per MindVault call
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

CRASH_EXIT = 3
MCP_PROTOCOL = "2025-06-18"


# ---------------------------------------------------------------- MindVault

class MindVault:
    """Minimal MCP Streamable HTTP client: initialize, then tools/call."""

    def __init__(self, url: str, key: str, log_path: str | None = None):
        self.url, self.key, self.log_path = url, key, log_path
        self.session: str | None = None
        self._next_id = 0

    def _post(self, payload: dict) -> tuple[dict | None, dict]:
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", f"Bearer {self.key}")
        req.add_header("MCP-Protocol-Version", MCP_PROTOCOL)
        if self.session:
            req.add_header("Mcp-Session-Id", self.session)
        with urllib.request.urlopen(req, timeout=60) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            raw = resp.read().decode()
        if not raw.strip():
            return None, headers
        if headers.get("content-type", "").startswith("text/event-stream"):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if "id" in msg:
                        return msg, headers
            return None, headers
        return json.loads(raw), headers

    def _rpc(self, method: str, params: dict) -> dict:
        self._next_id += 1
        msg, _ = self._post({"jsonrpc": "2.0", "id": self._next_id,
                             "method": method, "params": params})
        if msg is None or "error" in msg:
            raise RuntimeError(f"MindVault {method} failed: {msg}")
        return msg["result"]

    def connect(self) -> "MindVault":
        self._next_id += 1
        msg, headers = self._post({
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "nandatown-mv-seller",
                                      "version": "0.1"}}})
        if msg is None or "error" in msg:
            raise RuntimeError(f"MindVault initialize failed: {msg}")
        self.session = headers.get("mcp-session-id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def call(self, tool: str, args: dict) -> dict:
        started = time.time()
        result = self._rpc("tools/call", {"name": tool, "arguments": args})
        if result.get("isError"):
            raise RuntimeError(f"MindVault {tool} error: {result.get('content')}")
        out = result.get("structuredContent")
        if isinstance(out, dict) and set(out) == {"result"}:
            out = out["result"]
        if not isinstance(out, dict):
            out = json.loads(result["content"][0]["text"])
        self._log(tool, args, out, time.time() - started)
        return out

    def _log(self, tool: str, args: dict, out: dict, seconds: float) -> None:
        if not self.log_path:
            return
        entry = {"t": round(time.time(), 3), "tool": tool,
                 "seconds": round(seconds, 3),
                 # ids, levels and decisions only; never note contents
                 "args": {k: v for k, v in args.items() if k == "block_id"},
                 "response": _summary(out)}
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")


def _summary(out: dict) -> dict:
    keep = ("denied", "reason_code", "required_level", "block_id",
            "staging_id", "level", "status", "action", "level_basis", "error")
    summary = {k: out[k] for k in keep if k in out}
    if "results" in out:
        summary["results"] = [{k: r.get(k) for k in ("block_id", "level", "status")}
                              for r in out["results"]]
    return summary


def agent_key(agents_file: str, agent_id: str) -> str:
    with open(agents_file) as f:
        data = json.load(f)
    agents = data["agents"] if isinstance(data, dict) else data
    return next(a["key"] for a in agents if a["agent_id"] == agent_id)


def price_from_block(read: dict, block_id: str) -> int:
    """Unit price in cents from the price-list block's text."""
    for r in read.get("results", []):
        if r.get("block_id") == block_id:
            m = re.search(r"Unit price:\s*(\d+)\s*cents", r.get("content", ""))
            if m:
                return int(m.group(1))
    raise RuntimeError(f"no unit price in {block_id}: {_summary(read)}")


# ---------------------------------------------------------------- Town

class Town:
    def __init__(self, url: str, run_id: str):
        self.url, self.run_id, self.session = url, run_id, None

    def call(self, method: str, path: str, body: dict | None = None,
             retry: bool = True):
        req = urllib.request.Request(
            f"{self.url}/runs/{self.run_id}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json")
        if self.session:
            req.add_header("X-Town-Session", self.session)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return None if resp.status == 204 else json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 503 and retry:
                time.sleep(0.2)
                return self.call(method, path, body, retry=False)
            raise

    def ack(self, claim: dict, status: str, note: dict) -> bool:
        """False if the fence is stale (409 stale_fence)."""
        try:
            self.call("POST", "/inbox/ack", {"message_id": claim["message_id"],
                                             "fence": claim["fence"],
                                             "status": status, "note": note})
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                return False
            raise


class Journal:
    """Durable record of applied work, so a restart can resend, not redo."""

    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS done (mid TEXT PRIMARY KEY,"
                        " reply TEXT, note TEXT)")

    def get(self, mid: str):
        row = self.db.execute("SELECT reply, note FROM done WHERE mid=?",
                              (mid,)).fetchone()
        return (json.loads(row[0]), json.loads(row[1])) if row else None

    def put(self, mid: str, reply: dict, note: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO done VALUES (?, ?, ?)",
                        (mid, json.dumps(reply), json.dumps(note)))


# ---------------------------------------------------------------- seller

def apply(claim: dict, town: Town, memory: str, mv: MindVault | None,
          price_block: str) -> tuple[dict, dict]:
    """Do the work once: price, MindVault note. Returns (reply, note) unsent."""
    body = claim["body"]
    note: dict = {"applied": True, "memory": memory}
    if memory == "mindvault":
        read = mv.call("memory_read", {"block_id": price_block})
        unit = price_from_block(read, price_block)
        note["price_source"] = price_block
    else:
        unit = int(os.environ.get("PRICE_CENTS", "1995"))
        note["price_source"] = "PRICE_CENTS"
    total = body["quantity"] * unit
    mid = claim["message_id"]
    reply = {"message_id": "r-" + mid.removeprefix("q-"), "to": claim["from"],
             "kind": "quote_response",
             "body": {"request_id": mid, "sku": body["sku"],
                      "quantity": body["quantity"], "total_cents": total}}
    if memory == "mindvault":                     # 1. the deal note (reviewed)
        wrote = mv.call("memory_write", {
            "title": f"Deal note: quote {mid} for {body['quantity']} {body['sku']}",
            "content": (f"Quoted {body['quantity']} x {body['sku']} to Town "
                        f"buyer {claim['from']} for request {mid}. Unit price "
                        f"{unit} cents from finance price list {price_block}; "
                        f"total {total} cents (USD). Run {town.run_id}.")})
        note["mindvault_write"] = _summary(wrote)
    note["total_cents"] = total
    return reply, note


def main() -> int:
    env = os.environ
    town = Town(env["TOWN_URL"], env["RUN_ID"])
    memory = env.get("MEMORY", "off")
    if memory not in ("mindvault", "off"):
        print(f"MEMORY must be mindvault or off, not {memory!r}", file=sys.stderr)
        return 2
    state = env.get("STATE_DIR", ".")
    os.makedirs(state, exist_ok=True)
    fault = env.get("FAULT", "none")
    price_block = env.get("PRICE_BLOCK", "b-701")
    journal = Journal(os.path.join(state, "mv-journal.db"))
    mv = None
    if memory == "mindvault":
        key = agent_key(env["MINDVAULT_AGENTS"], env.get("MINDVAULT_AGENT", "finance"))
        mv = MindVault(env["MINDVAULT_URL"], key, env.get("MV_LOG")).connect()

    joined = town.call("POST", "/join", {"name": env["NAME"], "token": env["TOKEN"]})
    town.session = joined["session"]
    lease = float(joined.get("run", {}).get("lease_seconds", 5.0))
    deadline = time.time() + float(env.get("DEADLINE", "45"))
    crash_marker = os.path.join(state, "crashed-once")

    while time.time() < deadline:
        town.call("GET", "/inbox/notify?wait=0.4")
        claim = town.call("POST", "/inbox/claim")
        if claim is None:
            continue
        mid = claim["message_id"]
        if claim["kind"] != "quote_request":
            town.ack(claim, "rejected", {"reason": "unknown kind"})
            continue
        done = journal.get(mid)
        if done:                                   # already applied: resend
            town.call("POST", "/messages", done[0])
            town.ack(claim, "processed", {**done[1], "duplicate": True})
            continue
        reply, note = apply(claim, town, memory, mv, price_block)
        if fault == "crash_after_claim" and not os.path.exists(crash_marker):
            with open(crash_marker, "w") as f:
                f.write(claim["fence"])
            time.sleep(lease + 0.5)                # crash after the note, before the reply
            if town.ack(claim, "processed", {**note, "after_crash_stall": True}):
                return 9                           # stale fence accepted: fail loudly
            return CRASH_EXIT
        town.call("POST", "/messages", reply)      # 2. reply to the buyer
        journal.put(mid, reply, note)              # 3. journal
        town.ack(claim, "processed", note)         # 4. ack
    return 0


if __name__ == "__main__":
    sys.exit(main())
