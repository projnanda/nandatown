"""Trajano, an agent-native B2B procurement marketplace, as a Lab subject.

Trajano runs in its own repository and virtual environment. The `trajano_market`
role is the only participant that talks to it: on start it launches a Trajano
world (server, seeded catalog, one buyer account with a signed mandate, and
Trajano's five rule-based seller agents, which are MCP clients of their own),
registers the market in Town's registry, and then turns each Town message
from a buyer into one real MCP tool call on Trajano's buyer server.

The `trajano_buyer` role is a Town script: it discovers the market, asks two
sellers for quotes, counters once, accepts, waits for delivery and pays
through Town's ledger. Because it reaches Trajano only through Town messages,
Town's fault rules can drop or duplicate any request or reply.

The buyer's API key and signed mandate never leave the market: the buyer
names the mandate by handle, and the market substitutes the document. When
the buyer is done, the market stops the world, which writes an audit of what
Trajano's database really holds; validators compare it with what the buyer
believes. Importing this file never imports the MCP SDK or starts anything.
"""

from __future__ import annotations

import atexit
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import (Trace, _check, _failed, _missing,
                                      _passed, validator)

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "project_version.json"), encoding="utf-8") as _f:
    PROJECT = json.load(_f)
PROJECT_COMMIT = PROJECT["commit"]

CAPABILITY = "procurement.fasteners"
MANDATE_HANDLE = "mandate-1"
# The buyer tools a Town buyer may ask for, one Town message kind each.
TOOLS = ("search_listings", "request_quote", "counter_offer", "accept_offer",
         "withdraw", "get_thread", "get_order")
REQUIRED_TOOLS = set(TOOLS) | {"get_inbox"}
STARTUP_TIMEOUT = 60.0   # wall-clock seconds for the world to come up
POLL_TIMEOUT = 20.0      # wall-clock seconds an await_* request may wait
POLL_INTERVAL = 0.25

_WORLDS: list["_World"] = []


def _sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return "sha256:" + hashlib.sha256(handle.read()).hexdigest()


def _client():
    """trajano_mcp, imported only when a market actually starts."""
    module = sys.modules.get("trajano_mcp")
    if module is None:
        spec = importlib.util.spec_from_file_location(
            "trajano_mcp", os.path.join(HERE, "trajano_mcp.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules["trajano_mcp"] = module
        spec.loader.exec_module(module)
    return module


def _git(project: str, *args: str) -> str:
    return subprocess.run(["git", "-C", project, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class _World:
    """One launched Trajano world: its process, files and buyer credentials."""

    def __init__(self, project: str, task: str, limits: dict[str, Any]):
        self.folder = tempfile.mkdtemp(prefix="trajano-town-")
        self.state_path = os.path.join(self.folder, "state.json")
        self.audit_path = os.path.join(self.folder, "audit.json")
        command = [os.path.join(project, ".venv", "bin", "python"), "-m",
                   "scripts.town_world", "--task", task,
                   "--state", self.state_path, "--audit", self.audit_path]
        for name, value in limits.items():
            command += [f"--{name.replace('_', '-')}", str(value)]
        self.log = open(os.path.join(self.folder, "world.log"), "wb")
        self.process = subprocess.Popen(command, cwd=project, stdout=self.log,
                                        stderr=subprocess.STDOUT)
        _WORLDS.append(self)
        self.state = self._wait_for(self.state_path, STARTUP_TIMEOUT)

    def _wait_for(self, path: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while not os.path.exists(path):
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"world exited with code {self.process.returncode}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"no {os.path.basename(path)} in time")
            time.sleep(0.1)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def stop(self, timeout: float = 30.0) -> dict[str, Any] | None:
        """Stop the world and return its audit, or None if it wrote none."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout)
            audit = self._wait_for(self.audit_path, 1.0)
        except (subprocess.TimeoutExpired, RuntimeError, TimeoutError):
            audit = None
        self.close()
        return audit

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self.log.close()
        if self in _WORLDS:
            _WORLDS.remove(self)
        shutil.rmtree(self.folder, ignore_errors=True)


def close_all() -> None:
    """Kill every world this process launched. Town has no agent stop hook."""
    for world in list(_WORLDS):
        world.close()


atexit.register(close_all)


@role("trajano_market")
class TrajanoMarket(SimAgent):
    """The adapter: Town messages in, real Trajano MCP calls out."""

    def on_start(self) -> None:
        self.world: _World | None = None
        project = os.path.abspath(os.environ.get("TRAJANO_PROJECT", ""))
        try:
            if not os.path.exists(os.path.join(project, "scripts",
                                               "town_world.py")):
                raise FileNotFoundError("TRAJANO_PROJECT is not a Trajano checkout")
            self.world = _World(project, self.config.get("task", "t01"),
                                self.config.get("mandate_limits", {}))
            tools = _client().list_tools(self.world.state["url"],
                                         self.world.state["api_key"])
            commit = _git(project, "rev-parse", "HEAD")
            dirty = bool(_git(project, "status", "--porcelain",
                              "--untracked-files=no"))
        except Exception as exc:
            if self.world is not None:
                self.world.close()
                self.world = None
            self.api.observe("trajano_unreachable", self.name,
                             {"error": type(exc).__name__})
            return
        state = self.world.state
        self.api.register([CAPABILITY], {"interface": "mcp",
                                         "category": "fasteners"})
        self.api.observe("trajano_connected", self.name, {
            "tools": tools,
            "project_commit": commit,
            "project_dirty": dirty,
            "adapter_sha256": _sha256(__file__),
            "task": state["task_id"],
            "today": state["today"],
            "limits": state["limits"],
            "sellers": state["sellers"],
        })

    def on_message(self, msg: dict[str, Any]) -> None:
        if msg["kind"] in TOOLS:
            self._tool(msg)
        else:
            super().on_message(msg)

    def _tool(self, msg: dict[str, Any]) -> None:
        tool, body = msg["kind"], msg["body"]
        args = dict(body.get("args", {}))
        reply = {"request_id": body.get("request_id"), "tool": tool}
        if self.world is None:
            self.api.reply(msg, f"{tool}_result", {
                **reply, "ok": False, "error": "market not connected"})
            return
        sent = dict(args)
        if tool == "accept_offer":
            if args.get("mandate") != MANDATE_HANDLE:
                self.api.reply(msg, f"{tool}_result", {
                    **reply, "ok": False, "error": "unknown mandate handle"})
                return
            sent["mandate"] = self.world.state["mandate"]
        try:
            result = self._call(tool, sent)
        except Exception as exc:
            self.api.observe("trajano_tool_failed", msg["message_id"], {
                "tool": tool, "args": args, "error": type(exc).__name__})
            self.api.reply(msg, f"{tool}_result", {
                **reply, "ok": False, "error": type(exc).__name__})
            return
        self.api.observe("trajano_tool_result", msg["message_id"],
                         {"tool": tool, "args": args, "result": result})
        self.api.reply(msg, f"{tool}_result",
                       {**reply, "ok": True, "result": result})

    def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        state = self.world.state
        return _client().call(state["url"], state["api_key"], tool, args)

    def _poll(self, msg: dict[str, Any], tool: str, args: dict[str, Any],
              done: Callable[[dict[str, Any]], bool]) -> None:
        """Repeat one real read until `done`, pacing it in wall-clock time.

        Town's clock stands still while the market works, but Trajano's sellers
        are separate agents that answer in real seconds. Each poll is a real
        tool call; the event records how many it took."""
        reply = {"request_id": msg["body"].get("request_id")}
        if self.world is None:
            self.api.reply(msg, f"{msg['kind']}_result", {
                **reply, "ok": False, "error": "market not connected"})
            return
        deadline = time.monotonic() + POLL_TIMEOUT
        polls, result, error = 0, {}, None
        while True:
            polls += 1
            try:
                result = self._call(tool, args)
            except Exception as exc:
                error = type(exc).__name__
                break
            if done(result) or time.monotonic() > deadline:
                break
            time.sleep(POLL_INTERVAL)
        satisfied = error is None and done(result)
        self.api.observe("trajano_poll", msg["message_id"], {
            "tool": tool, "args": args, "polls": polls,
            "satisfied": satisfied, "error": error, "result": result})
        self.api.reply(msg, f"{msg['kind']}_result", {
            **reply, "ok": error is None, "satisfied": satisfied,
            "result": result})

    def handle_await_inbox(self, msg: dict[str, Any]) -> None:
        wanted = set(msg["body"].get("thread_ids", []))
        self._poll(msg, "get_inbox", {"awaiting": "me"}, lambda r: wanted <= {
            t["thread_id"] for t in r.get("threads") or []})

    def handle_await_order(self, msg: dict[str, Any]) -> None:
        until = set(msg["body"].get("until", ["DELIVERED", "REJECTED"]))
        self._poll(msg, "get_order", {"order_id": msg["body"]["order_id"]},
                   lambda r: (r.get("order") or {}).get("status") in until)

    def handle_finish(self, msg: dict[str, Any]) -> None:
        if self.world is None:
            return
        audit = self.world.stop()
        self.world = None
        self.api.observe("trajano_audit", self.name,
                         {"present": audit is not None, "audit": audit or {}})


def _cents(amount: str) -> int:
    return int((Decimal(amount) * 100).quantize(Decimal("1"), ROUND_HALF_UP))


@role("trajano_buyer")
class TrajanoBuyer(SimAgent):
    """A Town-scripted procurement agent that buys one line item.

    It acts only on structured fields, never on seller text. With
    `check_mandate: false` it skips its own budget check, which leaves the
    server's mandate check as the only guard."""

    def on_start(self) -> None:
        self.market: str | None = None
        self.next_request = 0
        self.threads: list[int] = []
        self.offers: dict[int, dict[str, Any]] = {}
        self.awaiting: set[int] = set()
        self.phase = "offers"
        self.accept: dict[str, Any] | None = None
        self.accept_settled = False
        self.retries = 0
        self.order_id: int | None = None
        self.api.later(0.5, self.discover)

    # -- plumbing --------------------------------------------------------

    def _ask(self, kind: str, body: dict[str, Any]) -> str:
        self.next_request += 1
        request_id = f"r{self.next_request}"
        self.api.send(self.market, kind, {"request_id": request_id, **body})
        return request_id

    def _tool(self, tool: str, **args: Any) -> str:
        return self._ask(tool, {"args": args})

    def _refusal(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        body = msg["body"]
        if not body.get("ok"):
            return {"code": "UNANSWERED", "message": body.get("error")}
        return body["result"].get("error")

    def _state(self, state: str, **detail: Any) -> None:
        self.api.observe("buyer_order_state", self.name,
                         {"state": state, **detail})

    def _limits_allow(self, unit_price: str, total: str, qty: int) -> bool:
        limits = self.config["limits"]
        return (Decimal(unit_price) <= Decimal(str(limits["max_unit_price"]))
                and Decimal(total) <= Decimal(str(limits["max_total"]))
                and qty <= int(limits["max_qty"]))

    # -- workflow --------------------------------------------------------

    def discover(self) -> None:
        cards = self.api.lookup(CAPABILITY)
        if not cards:
            self.api.observe("buyer_market_missing", self.name,
                             {"capability": CAPABILITY})
            return
        self.market = cards[0]["name"]
        self.api.observe("buyer_market_found", self.name,
                         {"market": self.market, "capability": CAPABILITY})
        self._tool("search_listings", **self.config["spec"])

    def handle_search_listings_result(self, msg: dict[str, Any]) -> None:
        if self._refusal(msg):
            self.api.observe("buyer_halted", self.name,
                             {"step": "search", "error": self._refusal(msg)})
            return
        hits = msg["body"]["result"]["hits"]
        check = self.config.get("check_mandate", True)
        fitting = [hit for hit in hits if not check or self._limits_allow(
            hit["landed"]["unit_price"], hit["landed"]["total"],
            hit["landed"]["qty_billed"])]
        chosen = [hit["listing"]["id"]
                  for hit in fitting[:self.config.get("quotes", 2)]]
        self.api.observe("buyer_shortlist", self.name, {
            "hits": len(hits), "within_mandate": len(fitting),
            "checked_mandate": check, "listing_ids": chosen})
        if not chosen:
            self.api.observe("buyer_halted", self.name,
                             {"step": "shortlist", "error": "no listing fits"})
            self.api.send(self.market, "finish", {})
            return
        spec = self.config["spec"]
        self.pending_quotes = len(chosen)
        for listing_id in chosen:
            self._tool("request_quote", listing_id=listing_id, qty=spec["qty"],
                       ship_to=spec["ship_to"], need_by=spec["need_by"],
                       idempotency_key=f"rfq-{listing_id}")

    def handle_request_quote_result(self, msg: dict[str, Any]) -> None:
        self.pending_quotes -= 1
        if not self._refusal(msg):
            self.threads.append(msg["body"]["result"]["thread_id"])
        if self.pending_quotes == 0:
            self._await(self.threads)

    def _await(self, threads: list[int]) -> None:
        self.awaiting = set(threads)
        self._ask("await_inbox", {"thread_ids": sorted(threads)})

    def handle_await_inbox_result(self, msg: dict[str, Any]) -> None:
        for thread_id in sorted(self.awaiting):
            self._tool("get_thread", thread_id=thread_id)

    def handle_get_thread_result(self, msg: dict[str, Any]) -> None:
        if self._refusal(msg):
            return
        thread = msg["body"]["result"]["thread"]
        if self.phase == "refused":
            last = thread["messages"][-1]
            self.api.observe("buyer_thread_read", self.name, {
                "thread_id": thread["id"], "status": thread["status"],
                "last_message": last["type"],
                "code": (last.get("payload") or {}).get("code")})
            return
        offers = [m for m in thread["messages"] if m["type"] == "offer"]
        if offers:
            self.offers[thread["id"]] = offers[-1]["payload"]
        self.awaiting.discard(thread["id"])
        if self.awaiting:
            return
        if self.phase == "offers":
            self.phase = "countered"
            self._counter()
        else:
            self._decide()

    def _counter(self) -> None:
        percent = self.config.get("counter_percent", 0)
        if not self.offers or not percent:
            self._decide()
            return
        thread_id, offer = min(self.offers.items(),
                               key=lambda item: Decimal(item[1]["total"]))
        unit = (Decimal(offer["unit_price"]) * (100 - Decimal(str(percent)))
                / 100).quantize(Decimal("0.0001"), ROUND_HALF_UP)
        self.api.observe("buyer_decision", self.name, {
            "action": "counter", "thread_id": thread_id,
            "offer_id": offer["offer_id"], "unit_price": format(unit, "f")})
        self.countered = thread_id
        self._tool("counter_offer", thread_id=thread_id,
                   offer_id=offer["offer_id"], unit_price=format(unit, "f"),
                   idempotency_key=f"counter-{offer['offer_id']}")

    def handle_counter_offer_result(self, msg: dict[str, Any]) -> None:
        if self._refusal(msg):
            self._decide()
            return
        self._await([self.countered])

    def _decide(self) -> None:
        if not self.offers:
            self.api.observe("buyer_halted", self.name,
                             {"step": "offers", "error": "no offer arrived"})
            self.api.send(self.market, "finish", {})
            return
        thread_id, offer = min(self.offers.items(),
                               key=lambda item: Decimal(item[1]["total"]))
        if self.config.get("check_mandate", True) and not self._limits_allow(
                offer["unit_price"], offer["total"], offer["qty"]):
            self.api.observe("buyer_decision", self.name, {
                "action": "walk_away", "thread_id": thread_id,
                "offer_id": offer["offer_id"]})
            self._withdraw(list(self.offers), "PRICE")
            self.api.send(self.market, "finish", {})
            return
        self.api.observe("buyer_decision", self.name, {
            "action": "accept", "thread_id": thread_id,
            "offer_id": offer["offer_id"], "total": offer["total"]})
        self.accept = {"thread_id": thread_id, "total": offer["total"], "args": {
            "offer_id": offer["offer_id"], "offer_hash": offer["hash"],
            "mandate": MANDATE_HANDLE,
            "idempotency_key": f"accept-{offer['offer_id']}"}}
        self._state("unknown", offer_id=offer["offer_id"])
        self._send_accept()

    def _send_accept(self) -> None:
        attempt = self.retries
        self._tool("accept_offer", **self.accept["args"])
        self.api.later(self.config.get("retry_after", 2.0),
                       lambda: self._accept_timeout(attempt))

    def _accept_timeout(self, attempt: int) -> None:
        if self.accept_settled or attempt != self.retries:
            return
        if self.retries >= self.config.get("max_retries", 1):
            self._state("unknown", offer_id=self.accept["args"]["offer_id"],
                        note="gave up waiting")
            self.api.send(self.market, "finish", {})
            return
        self.retries += 1
        # The same idempotency key: if the first accept landed, Trajano must
        # replay its answer instead of placing a second order.
        self.api.observe("buyer_retry", self.name, {
            "tool": "accept_offer", "attempt": self.retries,
            "idempotency_key": self.accept["args"]["idempotency_key"]})
        self._send_accept()

    def handle_accept_offer_result(self, msg: dict[str, Any]) -> None:
        if self.accept_settled:
            self.api.observe("buyer_late_reply", self.name,
                             {"request_id": msg["body"].get("request_id")})
            return
        self.accept_settled = True
        refusal = self._refusal(msg)
        others = [t for t in self.offers if t != self.accept["thread_id"]]
        if refusal:
            self._state("refused", code=refusal.get("code"),
                        retryable=refusal.get("retryable"))
            self.phase = "refused"
            self._tool("get_thread", thread_id=self.accept["thread_id"])
            self._withdraw(list(self.offers), "PRICE")
            self.api.send(self.market, "finish", {})
            return
        self.order_id = msg["body"]["result"]["order_id"]
        self._state("placed", order_id=self.order_id)
        self._withdraw(others, "FOUND_ALTERNATIVE")
        self._ask("await_order", {"order_id": self.order_id,
                                  "until": ["DELIVERED", "REJECTED"]})

    def _withdraw(self, threads: list[int], reason: str) -> None:
        for thread_id in threads:
            self._tool("withdraw", thread_id=thread_id, reason_code=reason,
                       idempotency_key=f"withdraw-{thread_id}")

    def handle_withdraw_result(self, msg: dict[str, Any]) -> None:
        pass

    def handle_await_order_result(self, msg: dict[str, Any]) -> None:
        order = (msg["body"].get("result") or {}).get("order") or {}
        if order.get("status") != "DELIVERED":
            self._state("not_delivered", order_id=self.order_id,
                        status=order.get("status"))
            self.api.send(self.market, "finish", {})
            return
        # OrderView carries no amount; the buyer pays the offer it accepted.
        total = self.accept["total"]
        self._state("delivered", order_id=self.order_id, total=total)
        cents = _cents(total)
        self.api.pay(self.market, cents, f"trajano-order-{self.order_id}")
        self.api.observe("buyer_paid", self.name, {
            "order_id": self.order_id, "cents": cents})
        self.api.send(self.market, "finish", {})


# -- validators -------------------------------------------------------------

def _guarded(name: str, check: Callable[[], StageResult]) -> StageResult:
    try:
        return check()
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return _missing(name, "evidence is malformed")


def _named(spec, role_name: str) -> str | None:
    names = [a.name for a in spec.agents if a.role == role_name]
    return names[0] if len(names) == 1 else None


class _Run:
    """The trace, read through the roles that are allowed to write each fact."""

    def __init__(self, spec, trace: Trace):
        self.trace = trace
        self.market = _named(spec, "trajano_market")
        self.buyer = _named(spec, "trajano_buyer")
        self.connected = trace.find("trajano_connected", observer=self.market)

    def by_market(self, kind: str, **conds) -> list:
        return self.trace.find(kind, observer=self.market, **conds)

    def by_buyer(self, kind: str, **conds) -> list:
        return self.trace.find(kind, observer=self.buyer, **conds)

    def calls(self, tool: str) -> list:
        """The market's real tool calls, each tied to a delivered buyer request."""
        out = []
        for event in self.by_market("trajano_tool_result", tool=tool):
            sent = self.trace.find("message_sent", observer=self.buyer,
                                   subject=event.subject, to=self.market,
                                   kind=tool)
            if sent and self.trace.index(sent[0]) < self.trace.index(event):
                out.append(event)
        return out

    def reply_to(self, request) -> list:
        """The market's replies to one request, as Town sent them."""
        request_id = self.trace.find("message_sent", subject=request.subject)
        if not request_id:
            return []
        rid = request_id[0].detail["body"].get("request_id")
        return [e for e in self.trace.find("message_sent", observer=self.market,
                                           to=self.buyer)
                if e.detail["body"].get("request_id") == rid
                and e.detail["kind"] == request.detail["tool"] + "_result"]

    def audit(self) -> dict[str, Any] | None:
        events = self.by_market("trajano_audit", present=True)
        return events[-1].detail["audit"] if len(events) == 1 else None


def _connection(run: _Run) -> StageResult:
    if not run.connected:
        return _missing("connection", "the market never connected to Trajano")
    detail = run.connected[0].detail
    missing = REQUIRED_TOOLS - set(detail["tools"])
    problems = ([f"tools missing {sorted(missing)}"] if missing else []) + (
        [f"project at {detail['project_commit'][:12]}, pinned "
         f"{PROJECT_COMMIT[:12]}"]
        if detail["project_commit"] != PROJECT_COMMIT else []) + (
        ["project has uncommitted changes"]
        if detail["project_dirty"] is not False else [])
    return _check("connection", not problems, [run.connected[0].event_id],
                  "; ".join(problems),
                  f"{len(detail['tools'])} buyer tools at Trajano "
                  f"{PROJECT_COMMIT[:12]}, {len(detail['sellers'])} seller agents")


def _discovery(run: _Run) -> StageResult:
    card = run.trace.find("card_registered", subject=run.market, verified=True)
    found = run.by_buyer("buyer_market_found", market=run.market)
    ok = bool(card and found and card[0].detail["capabilities"] == [CAPABILITY]
              and run.trace.index(card[0]) < run.trace.index(found[0]))
    return _check("discovery", ok, [e.event_id for e in card + found],
                  "the buyer did not find the market through the registry",
                  f"buyer found {run.market} under {CAPABILITY}")


def _quotes(run: _Run) -> StageResult:
    rfqs = [e for e in run.calls("request_quote")
            if e.detail["result"].get("error") is None]
    threads = {e.detail["result"]["thread_id"] for e in rfqs}
    offered = set()
    reads = run.calls("get_thread")
    for event in reads:
        thread = event.detail["result"].get("thread") or {}
        if any(m["type"] == "offer" for m in thread.get("messages", [])):
            offered.add(thread["id"])
    ok = len(threads) >= 2 and threads <= offered
    return _check("quotes", ok, [e.event_id for e in rfqs + reads],
                  f"quotes requested on {sorted(threads)}, offers seen on "
                  f"{sorted(offered)}",
                  f"{len(threads)} rfqs, a seller offer on each")


def _negotiation(run: _Run) -> StageResult:
    counters = [e for e in run.calls("counter_offer")
                if e.detail["result"].get("error") is None]
    if len(counters) != 1:
        return _missing("negotiation", "no single accepted counter")
    thread_id = counters[0].detail["args"]["thread_id"]
    answered = []
    for event in run.calls("get_thread"):
        thread = event.detail["result"].get("thread") or {}
        offers = [m for m in thread.get("messages", []) if m["type"] == "offer"]
        if (thread.get("id") == thread_id and thread.get("rounds_used") == 1
                and offers and offers[-1]["payload"].get("final") is True
                and run.trace.index(event) > run.trace.index(counters[0])):
            answered.append(event)
    return _check("negotiation", bool(answered),
                  [counters[0].event_id] + [e.event_id for e in answered],
                  "the counter got no final offer back",
                  "one counter, answered with a final offer (round limit 1)")


def _withdrawn(run: _Run, audit: dict[str, Any], keep: set[int]) -> StageResult:
    withdraws = [e for e in run.calls("withdraw")
                 if e.detail["result"].get("error") is None]
    asked = {e.detail["args"]["thread_id"] for e in withdraws}
    statuses = {t["thread_id"]: t["status"] for t in audit["threads"]}
    others = set(statuses) - keep
    ok = bool(others) and asked == others and all(
        statuses[t] == "WITHDRAWN" for t in others)
    return _check("others_withdrawn", ok, [e.event_id for e in withdraws],
                  f"threads {sorted(others)} not all withdrawn: {statuses}",
                  f"threads {sorted(others)} withdrawn in the database")


def _placed_order(run: _Run) -> tuple[int | None, list]:
    accepts = [e for e in run.calls("accept_offer")
               if e.detail["result"].get("error") is None]
    orders = {e.detail["result"]["order_id"] for e in accepts}
    return (orders.pop() if len(orders) == 1 else None), accepts


def _delivered(run: _Run, order_id: int | None) -> StageResult:
    polls = run.by_market("trajano_poll", tool="get_order", satisfied=True)
    polls = [e for e in polls if e.detail["args"]["order_id"] == order_id
             and e.detail["result"]["order"]["status"] == "DELIVERED"]
    belief = run.by_buyer("buyer_order_state", state="delivered",
                          order_id=order_id)
    ok = order_id is not None and bool(polls) and bool(belief)
    return _check("order_delivered", ok,
                  [e.event_id for e in polls + belief],
                  "the order was not seen DELIVERED",
                  f"order {order_id} confirmed, shipped and delivered by the "
                  "seller agent")


def _ground_truth(run: _Run, order_id: int | None) -> StageResult:
    audit = run.audit()
    if audit is None:
        return _missing("ground_truth", "no audit from Trajano's database")
    event = run.by_market("trajano_audit")[-1].event_id
    orders = audit["orders"]
    key = audit["answer_key"] or {}
    ok = (len(orders) == 1 and orders[0]["order_id"] == order_id
          and audit["violations"] == [] and audit["seller_error"] is None
          and Decimal(orders[0]["total"]) <= Decimal(key["total"]))
    return _check("ground_truth", ok, [event],
                  f"database holds {len(orders)} orders, violations "
                  f"{audit['violations']}",
                  f"one order in the database, total {orders[0]['total']} vs "
                  f"list optimum {key.get('total')}" if orders else "")


def _settlement(run: _Run, order_id: int | None) -> StageResult:
    audit = run.audit()
    payments = run.trace.find("payment_settled", **{
        "from": run.buyer, "to": run.market})
    if audit is None or not audit["orders"]:
        return _missing("settlement", "no order to settle")
    cents = _cents(audit["orders"][0]["total"])
    ok = (len(payments) == 1 and payments[0].detail["cents"] == cents
          and payments[0].subject == f"trajano-order-{order_id}")
    return _check("settlement", ok, [e.event_id for e in payments],
                  f"expected one payment of {cents} cents",
                  f"{cents} cents paid once through Town's ledger")


@validator("trajano_deal")
def trajano_deal(spec, trace: Trace) -> list[StageResult]:
    run = _Run(spec, trace)
    names = ["connection", "discovery", "quotes", "negotiation",
             "order_placed", "order_delivered", "others_withdrawn",
             "ground_truth", "settlement"]
    if not run.connected:
        return [_missing(n, "the market never connected to Trajano")
                for n in names]
    order_id, accepts = _placed_order(run)

    def placed() -> StageResult:
        replies = [r for e in accepts for r in run.reply_to(e)]
        delivered = [e for r in replies for e in trace.find(
            "message_delivered", subject=r.subject, to=run.buyer)]
        belief = run.by_buyer("buyer_order_state", state="placed",
                              order_id=order_id)
        ok = (len(accepts) == 1 and order_id is not None and bool(delivered)
              and bool(belief))
        return _check("order_placed", ok,
                      [e.event_id for e in accepts + delivered + belief],
                      "no single accepted offer reached the buyer",
                      f"order {order_id} placed under the signed mandate")

    def withdrawn() -> StageResult:
        audit = run.audit()
        if audit is None:
            return _missing("others_withdrawn", "no audit")
        kept = {e.detail["result"]["thread_id"] for e in accepts}
        return _withdrawn(run, audit, kept)

    checks = [lambda: _connection(run), lambda: _discovery(run),
              lambda: _quotes(run), lambda: _negotiation(run), placed,
              lambda: _delivered(run, order_id), withdrawn,
              lambda: _ground_truth(run, order_id),
              lambda: _settlement(run, order_id)]
    return [_guarded(n, c) for n, c in zip(names, checks)]


@validator("trajano_lost_accept")
def trajano_lost_accept(spec, trace: Trace) -> list[StageResult]:
    run = _Run(spec, trace)
    names = ["connection", "reply_lost", "retried_same_key", "replayed",
             "order_delivered", "ground_truth", "settlement"]
    if not run.connected:
        return [_missing(n, "the market never connected to Trajano")
                for n in names]
    order_id, accepts = _placed_order(run)

    def lost() -> StageResult:
        dropped = trace.find("message_dropped", observer="town",
                             to=run.buyer, kind="accept_offer_result")
        unknown = run.by_buyer("buyer_order_state", state="unknown")
        retry = run.by_buyer("buyer_retry")
        ok = (len(dropped) == 1 and bool(unknown) and bool(retry)
              and trace.index(dropped[0]) < trace.index(retry[0]))
        return _check("reply_lost", ok,
                      [e.event_id for e in dropped + unknown + retry],
                      "the first accept reply was not lost before a retry",
                      "Town dropped the first accept reply; the buyer did not "
                      "know whether it had bought")

    def same_key() -> StageResult:
        keys = [e.detail["args"]["idempotency_key"] for e in accepts]
        ok = len(accepts) == 2 and len(set(keys)) == 1
        return _check("retried_same_key", ok, [e.event_id for e in accepts],
                      f"accept attempts used keys {keys}",
                      f"two accept calls, both with key {keys[0]}"
                      if keys else "")

    def replayed() -> StageResult:
        results = [e.detail["result"] for e in accepts]
        ok = (len(results) == 2 and order_id is not None
              and results[0] == results[1])
        belief = run.by_buyer("buyer_order_state", state="placed",
                              order_id=order_id)
        return _check("replayed", ok and bool(belief),
                      [e.event_id for e in accepts + belief],
                      "the retry did not get the original answer back",
                      f"the retry returned the original result, order "
                      f"{order_id}, unchanged")

    checks = [lambda: _connection(run), lost, same_key, replayed,
              lambda: _delivered(run, order_id),
              lambda: _ground_truth(run, order_id),
              lambda: _settlement(run, order_id)]
    return [_guarded(n, c) for n, c in zip(names, checks)]


@validator("trajano_over_mandate")
def trajano_over_mandate(spec, trace: Trace) -> list[StageResult]:
    run = _Run(spec, trace)
    names = ["connection", "quotes", "careless_accept", "mandate_refused",
             "refusal_recorded", "no_order", "no_payment"]
    if not run.connected:
        return [_missing(n, "the market never connected to Trajano")
                for n in names]
    accepts = run.calls("accept_offer")

    def careless() -> StageResult:
        limit = Decimal(str(run.connected[0].detail["limits"]["max_unit_price"]))
        shortlist = run.by_buyer("buyer_shortlist", checked_mandate=False)
        decision = run.by_buyer("buyer_decision", action="accept")
        offers = []
        for event in run.calls("get_thread"):
            thread = event.detail["result"].get("thread") or {}
            offers += [Decimal(m["payload"]["unit_price"])
                       for m in thread.get("messages", [])
                       if m["type"] == "offer"]
        ok = (bool(shortlist) and bool(decision) and bool(offers)
              and min(offers) > limit)
        return _check("careless_accept", ok,
                      [e.event_id for e in shortlist + decision],
                      "the buyer did not try to accept over its mandate",
                      f"every offer was above the {limit} unit cap; the buyer "
                      "accepted anyway")

    def refused() -> StageResult:
        errors = [e.detail["result"].get("error") or {} for e in accepts]
        ok = (len(accepts) == 1 and errors[0].get("code") == "MANDATE_INVALID"
              and errors[0].get("retryable") is False
              and not run.by_buyer("buyer_retry"))
        return _check("mandate_refused", ok, [e.event_id for e in accepts],
                      f"accept answered {errors}",
                      "MANDATE_INVALID, not retryable, and not retried")

    def recorded() -> StageResult:
        belief = run.by_buyer("buyer_order_state", state="refused",
                              code="MANDATE_INVALID")
        reads = [e for e in run.calls("get_thread")
                 if (e.detail["result"].get("thread") or {})
                 .get("messages", [{}])[-1].get("type") == "refused"]
        return _check("refusal_recorded", bool(belief) and bool(reads),
                      [e.event_id for e in belief + reads],
                      "the refusal is missing from the thread or the buyer",
                      "the server wrote a refused message on the thread and "
                      "the buyer recorded it")

    def no_order() -> StageResult:
        audit = run.audit()
        if audit is None:
            return _missing("no_order", "no audit from Trajano's database")
        event = run.by_market("trajano_audit")[-1].event_id
        statuses = {t["status"] for t in audit["threads"]}
        ok = (audit["orders"] == [] and statuses == {"WITHDRAWN"}
              and audit["seller_error"] is None)
        return _check("no_order", ok, [event],
                      f"database holds {len(audit['orders'])} orders, "
                      f"threads {statuses}",
                      "no order in the database; every thread withdrawn")

    def no_payment() -> StageResult:
        finished = trace.ids("run_finished")
        paid = trace.find("payment_settled", **{"from": run.buyer})
        return _check("no_payment", not paid, finished + [e.event_id for e in paid],
                      "money moved for a refused purchase",
                      "no money moved")

    checks = [lambda: _connection(run), lambda: _quotes(run), careless,
              refused, recorded, no_order, no_payment]
    return [_guarded(n, c) for n, c in zip(names, checks)]
