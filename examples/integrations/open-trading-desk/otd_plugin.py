"""Open Trading Desk in the Lab.

The subject is the Open Trading Desk MCP server, hosted in its own
repository and started as a separate process. This file holds only the
Town side:

  otd_desk   adapter role, and the only role that talks MCP. It turns Town
             messages into real tool calls. Real delegation tokens stay
             here; Town records and messages carry only opaque handles.
  otd_owner  grants the delegation and later revokes it.
  otd_pm     finds providers, collects views, decides with the project's
             own rule, and sends orders.

Owner and PM are integration scripts, not independently developed agents.
Validators judge only the recorded trace.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from typing import Any, NamedTuple

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import Trace, _check, _missing, validator

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ENV = "OTD_PROJECT"
PROJECT_COMMIT = "7c9fc6b52742700a9b8c5680406779b8da0f5124"
EXPECTED_TOOLS = {"close_day", "find_providers", "get_scores",
                  "issue_delegation", "place_order", "request_view",
                  "revoke_delegation"}
STAGES = ("connection", "discovery_and_views", "decision_and_fill",
          "risk_refusals", "scoring_and_chain", "pm_knowledge")
FAULT_STAGES = ("fault_and_retry",)


def _adapter_hash() -> str:
    digest = hashlib.sha256()
    for name in ("otd_plugin.py", "mcp_bridge.py"):
        with open(os.path.join(HERE, name), "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()


def _commit(project: str) -> tuple[str, bool]:
    """The project's HEAD, and whether its files differ from that commit.
    If git cannot say, the commit is unknown and the files count as changed."""
    head, status = (
        subprocess.run(["git", "-C", project, *args], capture_output=True,
                       text=True, check=False)
        for args in (("rev-parse", "HEAD"), ("status", "--porcelain")))
    if head.returncode or status.returncode:
        return "unknown", True
    return head.stdout.strip(), bool(status.stdout.strip())


def _load_project():
    """The project's own MCP result decoder and PM decision rule."""
    project = os.environ[PROJECT_ENV]
    for path in (HERE, os.path.join(project, "src")):
        if path not in sys.path:
            sys.path.insert(0, path)
    from opentradingdesk.pm_agent import call, decide
    return project, call, decide


# -- the adapter ------------------------------------------------------------


@role("otd_desk")
class Desk(SimAgent):
    def on_start(self):
        self.bridge = None
        self.tokens: dict[str, str] = {}
        self.api.register(["otd.desk"])
        try:
            project, invoke, _ = _load_project()
            from mcp_bridge import McpBridge
            python = os.path.join(project, ".venv", "bin", "python")
            self.data_dir = tempfile.TemporaryDirectory(prefix="otd-town-")
            self.bridge = McpBridge(
                python, ["-m", "opentradingdesk.server"], invoke,
                env={"OTD_DATA_DIR": self.data_dir.name},
                cwd=os.path.join(project, "src"))
            tools = self.bridge.start()
        except Exception as exc:  # recorded, not hidden
            self.bridge = None
            self.api.observe("desk_unreachable", self.name,
                             {"error": type(exc).__name__})
            return
        commit, dirty = _commit(project)
        self.api.observe("desk_connected", self.name,
                         {"tools": sorted(tools),
                          "project_commit": commit,
                          "project_dirty": dirty,
                          "adapter_sha256": _adapter_hash()})

    def _resolve(self, args: dict[str, Any]) -> dict[str, Any]:
        out = dict(args)
        if "delegation_token" in out:
            out["delegation_token"] = self.tokens[out["delegation_token"]]
        return out

    def _safe(self, text: str) -> str:
        """Error text can quote the resolved arguments, so swap tokens out."""
        for handle, token in self.tokens.items():
            text = text.replace(token, handle)
        return text[:200]

    def _run(self, request_id: str, tool: str,
             args: dict[str, Any]) -> dict[str, Any]:
        if self.bridge is None:
            return {"ok": False, "error": "desk_unavailable"}
        from mcp_bridge import BridgeError
        try:
            result = self.bridge.call(tool, **self._resolve(args))
        except (BridgeError, KeyError) as exc:
            error = self._safe(str(exc))
            self.api.observe("otd_tool_failed", tool,
                             {"request_id": request_id, "error": error})
            return {"ok": False, "error": error}
        if tool == "issue_delegation":
            handle = f"delegation-{len(self.tokens) + 1}"
            self.tokens[handle] = result
            result = handle
        self.api.observe("otd_tool_result", tool, {
            "request_id": request_id, "args": args, "result": result})
        return {"ok": True, "result": result}

    def _answer(self, msg: dict[str, Any], kind: str) -> None:
        body = msg["body"]
        outcome = self._run(body["request_id"], body["tool"], body["args"])
        self.api.reply(msg, kind, {"request_id": body["request_id"],
                                   "tool": body["tool"], **outcome})

    def handle_tool_request(self, msg):
        self._answer(msg, "tool_result")

    def handle_order_request(self, msg):
        self._answer(msg, "order_result")


# -- the owner --------------------------------------------------------------


@role("otd_owner")
class Owner(SimAgent):
    def on_start(self):
        self.api.register(["otd.owner"])
        self._ask("issue_delegation", owner="demo-owner",
                  max_position_pct=0.10, long_only=True, flat_by_minute=385,
                  daily_loss_stop_pct=0.03, ttl_minutes=480)

    def _ask(self, tool: str, **args: Any) -> None:
        self.api.send(self.config["desk"], "tool_request",
                      {"request_id": f"{self.name}-{tool}", "tool": tool,
                       "args": args})

    def handle_tool_result(self, msg):
        body = msg["body"]
        if not body["ok"]:
            self.api.observe("owner_halted", body["tool"],
                             {"error": body["error"]})
        elif body["tool"] == "issue_delegation":
            self.handle = body["result"]
            self.api.send(self.config["pm"], "delegation_granted",
                          {"delegation": self.handle})
        else:
            self.api.send(self.config["pm"], "delegation_revoked", {})

    def handle_revoke_now(self, msg):
        self._ask("revoke_delegation", delegation_token=self.handle)


# -- the portfolio manager --------------------------------------------------


@role("otd_pm")
class PortfolioManager(SimAgent):
    """One linear workflow, written as a generator that yields each step."""

    def on_start(self):
        self.api.register(["otd.pm"])
        self.pending: str | None = None
        self.sent = 0
        self.script = None

    def _advance(self, value: Any = None) -> None:
        try:
            self.script.send(value)()
        except StopIteration:
            return
        except Exception as exc:  # recorded, so the run still leaves evidence
            self.pending = None
            self.script.close()
            self.api.observe("pm_halted", "workflow",
                             {"error": type(exc).__name__})

    def _send(self, kind: str, tool: str, args: dict[str, Any]) -> str:
        self.sent += 1
        request_id = f"pm-{self.sent}"
        self.pending = request_id
        self.api.send(self.config["desk"], kind,
                      {"request_id": request_id, "tool": tool, "args": args})
        return request_id

    def _ask(self, tool: str, **args: Any):
        return lambda: self._send("tool_request", tool, args)

    def _order(self, ticker, side, position_pct, minute):
        """Send one order. With no reply by the deadline, retry once."""
        args = {"ticker": ticker, "side": side, "position_pct": position_pct,
                "delegation_token": self.delegation, "as_of_minute": minute}

        def expire(request_id: str, attempt: int) -> None:
            if self.pending != request_id:
                return
            self.api.observe("pm_order_state", ticker,
                             {"state": "unknown", "basis": "no_reply",
                              "attempt": attempt})
            if attempt == 2:
                self.pending = None
                self._advance(None)
                return
            self.retried = True
            self.api.observe("pm_order_retry", ticker, {"attempt": 2})
            self._arm(args, ticker, expire, 2)

        def step() -> None:
            self.retried = False
            self._arm(args, ticker, expire, 1)
        return step

    def _arm(self, args, ticker, expire, attempt) -> None:
        request_id = self._send("order_request", "place_order", args)
        self.api.later(float(self.config.get("retry_after", 1.0)),
                       lambda: expire(request_id, attempt))

    def handle_delegation_granted(self, msg):
        self.delegation = msg["body"]["delegation"]
        self.script = self._workflow()
        self._advance()

    def handle_delegation_revoked(self, msg):
        self._advance()

    def _on_result(self, msg) -> None:
        body = msg["body"]
        if body["request_id"] != self.pending:
            self.api.observe("pm_late_reply", body["request_id"], {})
            return
        self.pending = None
        if not body["ok"]:
            self.api.observe("pm_halted", body["tool"],
                             {"error": body["error"]})
            return
        self._advance(body["result"])

    handle_tool_result = _on_result
    handle_order_result = _on_result

    def _workflow(self):
        _, _, decide = _load_project()
        tickers, minutes = self.config["tickers"], self.config["checkpoints"]
        cards = yield self._ask("find_providers", ticker=tickers[0])
        ids = [c["provider_id"] for c in cards]
        self.api.observe("pm_providers", tickers[0], {"providers": ids})

        asked, decision, at = set(), None, None
        for minute in minutes:
            for ticker in tickers:
                views = []
                for provider in ids:
                    asked.add((minute, ticker, provider))
                    r = yield self._ask("request_view", provider_id=provider,
                                        ticker=ticker, horizon_minutes=30,
                                        deadline_seconds=20,
                                        as_of_minute=minute)
                    if r["status"] == "ok":
                        views.append(r["view"])
                self.api.observe("pm_views", ticker, {
                    "minute": minute,
                    "directions": {v["provider_id"]: v["direction"]
                                   for v in views}})
                decision = decide(ticker, views)
                if decision:
                    at = minute
                    break
            if decision:
                break
        if decision is None:
            self.api.observe("pm_no_trade", "day", {})
            return
        ticker = decision["ticker"]
        self.api.observe("pm_decision", ticker, {"minute": at, **decision})

        first = yield self._order(ticker, "buy", decision["position_pct"], at)
        self._record_first(ticker, first)

        others = [t for t in tickers if t != ticker]
        oversized = yield self._order(others[0], "buy", 0.25, at)
        self.api.observe("pm_refusal_seen", others[0], {"case": "oversized",
                                                       "reply": oversized})
        short = yield self._order(others[1], "sell", 0.05, at)
        self.api.observe("pm_refusal_seen", others[1], {"case": "short",
                                                       "reply": short})

        for minute in minutes:
            for symbol in tickers:
                for provider in ids:
                    if (minute, symbol, provider) not in asked:
                        asked.add((minute, symbol, provider))
                        yield self._ask("request_view", provider_id=provider,
                                        ticker=symbol, horizon_minutes=30,
                                        deadline_seconds=20,
                                        as_of_minute=minute)
        self.api.observe("pm_views_requested", "day", {"count": len(asked)})
        yield self._ask("get_scores", as_of_minute=minutes[-1])

        def revoke() -> None:
            self.api.send(self.config["owner"], "revoke_now", {})
        yield revoke
        revoked = yield self._order(others[0], "buy", 0.05, at)
        self.api.observe("pm_refusal_seen", others[0], {"case": "revoked",
                                                       "reply": revoked})

        summary = yield self._ask("close_day", as_of_minute=390)
        fills = [o for o in summary["orders"]
                 if o["ticker"] == ticker and o["approved"]]
        self.api.observe("pm_order_state", ticker, {
            "state": "filled" if fills else "not_filled",
            "basis": "close_day_orders", "fills": len(fills)})

    def _record_first(self, ticker: str, reply: dict[str, Any] | None) -> None:
        if reply is None:
            return
        if reply["approved"]:
            self.api.observe("pm_order_state", ticker,
                             {"state": "filled", "basis": "order_result"})
        elif reply["rule_triggered"] == "DUPLICATE_POSITION":
            # A position exists in this ticker. That does not prove this
            # order filled, and the project has no order-status tool.
            self.api.observe("pm_order_state", ticker, {
                "state": "unknown", "basis": "duplicate_position_is_not_proof"})
        else:
            self.api.observe("pm_order_state", ticker,
                             {"state": "refused", "basis": "order_result",
                              "rule": reply["rule_triggered"]})


# -- validators -------------------------------------------------------------
#
# Each stage rebuilds what happened from the trace: the request a role sent,
# the desk's MCP result, the reply, and whether that reply was delivered.
# A role's own observation never counts unless those records back it up.


class Cast(NamedTuple):
    """Who the scenario says plays each part, and the day it says to cover."""
    desk: str
    owner: str
    pm: str
    providers: list[str]
    tickers: list[str]
    checkpoints: list[int]


def _cast(spec) -> Cast:
    def one(name: str):
        [agent] = [a for a in spec.agents if a.role == name]
        return agent
    pm = one("otd_pm")
    return Cast(one("otd_desk").name, one("otd_owner").name, pm.name,
                pm.config["expected_providers"], pm.config["tickers"],
                pm.config["checkpoints"])


def _attributed(trace: Trace, cast: Cast) -> Trace:
    """Keep a role's records only when that role is the one that wrote them."""
    author = {"desk_connected": cast.desk, "otd_tool_result": cast.desk,
              **dict.fromkeys(PM_RECORDS, cast.pm)}
    return Trace([e for e in trace.events
                  if e.observer == author.get(e.kind, e.observer)],
                 trace.run_id)


class Exchange(NamedTuple):
    request_id: str
    tool: str
    args: dict[str, Any]
    sender: str
    result: Any
    sent_at: int
    reply_at: int | None  # trace position of delivery; None if never arrived
    reply_sent_at: int
    reply_id: str
    evidence: list[str]

    @property
    def arrived(self) -> bool:
        return self.reply_at is not None


REPLY_KIND = {"tool_request": "tool_result", "order_request": "order_result"}
OWNER_TOOLS = {"issue_delegation", "revoke_delegation"}
PM_RECORDS = ("pm_providers", "pm_views", "pm_decision", "pm_order_state",
              "pm_order_retry", "pm_refusal_seen", "pm_views_requested",
              "pm_halted")


def _exchanges(trace: Trace, cast: Cast) -> list[Exchange]:
    """Requests the right role sent and the desk really received, matched to
    intact replies, each step in causal order: sent, delivered, MCP result,
    reply, reply delivery."""
    sent = trace.find("message_sent")
    arrived = {}
    for event in trace.find("message_delivered"):
        arrived.setdefault(event.subject, event)
    replies = {e.detail["body"]["request_id"]: e for e in sent
               if e.detail["kind"] in REPLY_KIND.values()}
    calls = {e.detail["request_id"]: e
             for e in trace.find("otd_tool_result")}
    out = []
    for request in sent:
        wanted = REPLY_KIND.get(request.detail["kind"])
        if wanted is None:
            continue
        body = request.detail["body"]
        rid = body["request_id"]
        call, reply = calls.get(rid), replies.get(rid)
        got = arrived.get(request.subject)
        if got is None or call is None or reply is None:
            continue
        back = reply.detail["body"]
        sender = cast.owner if body["tool"] in OWNER_TOOLS else cast.pm
        intact = (
            request.observer == sender
            and got.detail["to"] == request.detail["to"] == cast.desk
            == call.observer == reply.observer
            and reply.detail["to"] == request.observer
            and reply.detail["kind"] == wanted
            and call.subject == body["tool"] == back["tool"]
            and call.detail["args"] == body["args"]
            and back["ok"] is True
            and back["result"] == call.detail["result"]
            and trace.index(request) < trace.index(got) < trace.index(call)
            < trace.index(reply))
        if not intact:
            continue
        landed = arrived.get(reply.subject)
        reply_at = None
        if landed is not None:
            if (landed.detail["to"] != reply.detail["to"]
                    or trace.index(landed) < trace.index(reply)):
                continue
            reply_at = trace.index(landed)
        out.append(Exchange(
            rid, body["tool"], body["args"], request.observer,
            call.detail["result"], trace.index(request), reply_at,
            trace.index(reply), reply.subject,
            [request.event_id, call.event_id, reply.event_id]))
    return out


def _of(exchanges: list[Exchange], tool: str) -> list[Exchange]:
    return [x for x in exchanges if x.tool == tool]


def _close(exchanges: list[Exchange]) -> Exchange | None:
    """The one delivered close_day, provided every other exchange ended
    before it was asked for: its summary cannot count what came later."""
    closes = [x for x in _of(exchanges, "close_day") if x.arrived]
    if len(closes) != 1:
        return None
    close = closes[0]
    ended = all((x.reply_at if x.arrived else x.reply_sent_at) < close.sent_at
                for x in exchanges if x is not close)
    return close if ended else None


def _approved(close: Exchange | None) -> list[dict[str, Any]]:
    return ([o for o in close.result["orders"] if o["approved"]]
            if close else [])


def _round_views(exchanges, ticker, minute, before) -> list[Exchange]:
    return [x for x in _of(exchanges, "request_view")
            if x.arrived and x.reply_at < before
            and x.args["ticker"] == ticker
            and x.args["as_of_minute"] == minute]


_DAMAGED = (KeyError, IndexError, TypeError, AttributeError, ValueError,
            StopIteration, ZeroDivisionError)


def _guarded(name: str, check) -> StageResult:
    """Evidence too damaged to read is missing evidence, not a crash."""
    try:
        return check()
    except _DAMAGED:
        return _missing(name, "required evidence is missing or malformed")


def _connection(trace):
    connected = trace.find("desk_connected")
    detail = connected[0].detail if connected else {}
    tools = set(detail.get("tools", ()))
    return _check("connection",
                  tools == EXPECTED_TOOLS
                  and detail.get("project_commit") == PROJECT_COMMIT
                  and detail.get("project_dirty") is False,
                  _ids(connected),
                  f"expected the project's seven MCP tools, from unmodified "
                  f"commit {PROJECT_COMMIT[:7]}",
                  "live MCP stdio session; validator otd-3")


def _ids(events) -> list[str]:
    return [e.event_id for e in events]


def _key(x: Exchange) -> tuple[int, str, str]:
    return x.args["as_of_minute"], x.args["ticker"], x.args["provider_id"]


def _rounds(cast: Cast) -> list[tuple[int, str]]:
    return [(m, t) for m in cast.checkpoints for t in cast.tickers]


def _delivered(trace, kind: str, sender: str, to: str):
    """The one sent event of a control message, and where it arrived."""
    [sent] = trace.find("message_sent", kind=kind, observer=sender, to=to)
    [got] = [e for e in trace.find("message_delivered", subject=sent.subject,
                                   to=to)
             if trace.index(e) > trace.index(sent)]
    return sent, trace.index(got)


def _view_ok(x: Exchange) -> bool:
    """A reply is a no-show, or a view from the provider and ticker asked."""
    if x.result["status"] == "no_show":
        return True
    view = x.result["view"]
    return (x.result["status"] == "ok" and view["ticker"] == x.args["ticker"]
            and view["provider_id"] == x.args["provider_id"])


def _discovery(trace, exchanges, cast):
    found = [x for x in _of(exchanges, "find_providers") if x.arrived]
    decisions = trace.find("pm_decision")
    names = [c["provider_id"] for c in found[0].result]
    decision = decisions[0]
    decision_at = trace.index(decision)
    views = [x for x in _of(exchanges, "request_view")
             if x.arrived and x.reply_at < decision_at]
    rounds = _rounds(cast)
    played = rounds[:rounds.index((decision.detail["minute"],
                                   decision.subject)) + 1]
    ok = (len(found) == 1 and sorted(names) == sorted(cast.providers)
          and trace.find("pm_providers")[0].detail["providers"] == names
          and len(decisions) == 1
          and Counter(map(_key, views)) == Counter(
              (m, t, p) for m, t in played for p in cast.providers)
          and all(_view_ok(x) for x in views))
    return _check("discovery_and_views", ok,
                  found[0].evidence + [i for x in views for i in x.evidence],
                  "every round up to the decision needs each expected "
                  "provider asked once, and its reply delivered")


def _first_order(exchanges, decision) -> Exchange:
    return next(x for x in _of(exchanges, "place_order")
                if x.args["ticker"] == decision.subject
                and x.args["side"] == "buy")


def _decision(trace, exchanges, lost_reply):
    decision = trace.find("pm_decision")[0]
    d = decision.detail
    views = [x.result["view"] for x in _round_views(
        exchanges, decision.subject, d["minute"], trace.index(decision))
        if x.result["status"] == "ok"]
    longs = [v for v in views if v["direction"] == "long"]
    average = sum(v["confidence"] for v in longs) / len(longs)
    first = _first_order(exchanges, decision)
    close = _close(exchanges)
    fills = _approved(close)
    ok = (len(longs) * 2 > len(views)
          and sorted(d["supporting_providers"])
          == sorted(v["provider_id"] for v in longs)
          and d["position_pct"] == round(min(0.08, 0.04 + average * 0.05), 3)
          and first.sent_at > trace.index(decision)
          and first.args["position_pct"] == d["position_pct"]
          and first.args["as_of_minute"] == d["minute"]
          and first.result["approved"] is True
          and first.arrived is (not lost_reply)
          and len(fills) == 1 and fills[0]["ticker"] == decision.subject
          and fills[0]["position_pct"] == d["position_pct"]
          and fills[0]["minute"] == d["minute"]
          and bool(trace.find("message_dropped")) is lost_reply)
    return _check("decision_and_fill", ok,
                  [decision.event_id] + first.evidence + close.evidence,
                  "the decision must follow the delivered views and the "
                  "project's rule, and match exactly one approved fill")


def _refusal(exchanges, trace, case, match, rule, after=-1):
    x = next(x for x in _of(exchanges, "place_order") if match(x.args)
             and x.arrived and x.reply_at > after and not x.result["approved"]
             and x.result["rule_triggered"] == rule)
    seen = [e for e in trace.find("pm_refusal_seen")
            if e.detail["case"] == case and e.detail["reply"] == x.result
            and trace.index(e) > x.reply_at]
    return x, seen


def _risk(trace, exchanges, cast):
    close = _close(exchanges)
    over, a = _refusal(exchanges, trace, "oversized",
                       lambda v: v["position_pct"] == 0.25,
                       "POSITION_LIMIT_EXCEEDED")
    short, b = _refusal(exchanges, trace, "short",
                        lambda v: v["side"] == "sell", "SHORT_NOT_ALLOWED")
    revoke = _of(exchanges, "revoke_delegation")[0]
    [issued] = _of(exchanges, "issue_delegation")
    orders = _of(exchanges, "place_order")
    granted, granted_at = _delivered(trace, "delegation_granted",
                                     cast.owner, cast.pm)
    asked, asked_at = _delivered(trace, "revoke_now", cast.pm, cast.owner)
    told, told_at = _delivered(trace, "delegation_revoked",
                               cast.owner, cast.pm)
    late, c = _refusal(exchanges, trace, "revoked",
                       lambda v: v["position_pct"] == 0.05,
                       "DELEGATION_REVOKED", after=told_at)
    worked_before = any(
        x.result["approved"] for x in _of(exchanges, "place_order")
        if x.args["delegation_token"] == late.args["delegation_token"])
    logged = [o["rule_triggered"] for o in close.result["orders"]]
    ok = (revoke.result == "revoked" and revoke.arrived and worked_before
          and revoke.args["delegation_token"] == late.args["delegation_token"]
          and issued.arrived and issued.reply_at < trace.index(granted)
          and issued.result == granted.detail["body"]["delegation"]
          == late.args["delegation_token"]
          and all(x.sent_at > granted_at for x in orders)
          and asked_at < revoke.sent_at and revoke.reply_at < trace.index(told)
          and len(a) == len(b) == len(c) == 1
          and all(rule in logged for rule in (
              "POSITION_LIMIT_EXCEEDED", "SHORT_NOT_ALLOWED",
              "DELEGATION_REVOKED")))
    evidence = (over.evidence + short.evidence + revoke.evidence + late.evidence
                + _ids([granted, asked, told]))
    return _check("risk_refusals", ok, evidence,
                  "oversized, short and revoked orders must each be refused "
                  "by the gate, delivered, and logged")


def _scoring(trace, exchanges, cast):
    close = _close(exchanges)
    summary = close.result
    views = _of(exchanges, "request_view")
    day = Counter((m, t, p) for m, t in _rounds(cast) for p in cast.providers)
    logged = int(re.fullmatch(r"chain verified, (\d+) entries, .*",
                              summary["chain_message"]).group(1))
    ok = (summary["chain_verified"] is True and logged == day.total()
          and Counter(map(_key, views)) == day
          and all(x.arrived and _view_ok(x) for x in views)
          and trace.find("pm_views_requested")[0].detail["count"] == day.total()
          and sorted(s["provider_id"] for s in summary["scores"])
          == sorted(cast.providers))
    for score in summary["scores"]:
        mine = [x for x in views
                if x.args["provider_id"] == score["provider_id"]]
        shows = sum(x.result["status"] == "ok" for x in mine)
        ok = ok and score["n_views"] == shows
        decided = score["hits"] + score["misses"]
        ok = ok and decided + score["expired"] == shows
        ok = ok and min(score["hits"], score["misses"], score["expired"],
                        score["no_shows"]) >= 0
        rate = score["hits"] / decided if decided else 0.0
        ok = ok and math.isclose(score["hit_rate"], rate)
        ok = ok and score["no_shows"] == len(mine) - shows
    return _check("scoring_and_chain", ok, close.evidence,
                  "the hash chain must verify and every provider's score "
                  "must cover exactly the views it gave")


def _knowledge(trace, exchanges, lost_reply):
    name = "pm_knowledge"
    decision = trace.find("pm_decision")[0]
    ticker = decision.subject
    close = _close(exchanges)
    states = [e for e in trace.find("pm_order_state") if e.subject == ticker]
    final = [e for e in states if e.detail["basis"] == "close_day_orders"]
    before = [e for e in states if e not in final]
    fills = [o for o in close.result["orders"]
             if o["ticker"] == ticker and o["approved"]]
    settled = (len(final) == 1 and final[0].detail["state"] == "filled"
               and final[0].detail["fills"] == len(fills) == 1
               and trace.index(final[0]) > close.reply_at
               and not trace.find("pm_halted"))
    first = _first_order(exchanges, decision)
    evidence = _ids(states) + close.evidence
    if not lost_reply:
        told = [e for e in before if e.detail["state"] == "filled"]
        return _check(name, settled and len(told) == len(before) == 1
                      and trace.index(told[0]) > first.reply_at, evidence,
                      "the PM must learn the fill from the delivered reply "
                      "and match it at close")
    retry = _order_retry(exchanges, first)
    bases = [(e.detail["state"], e.detail["basis"]) for e in before]
    ok = (settled and bases == [("unknown", "no_reply"),
                                ("unknown", "duplicate_position_is_not_proof")]
          and trace.index(before[1]) > retry.reply_at
          and trace.index(before[1]) < close.reply_at)
    return _check(name, ok, evidence,
                  "the PM must stay unknown until the delivered close-day "
                  "record shows the fill")


def _order_retry(exchanges, first) -> Exchange:
    orders = _of(exchanges, "place_order")
    return orders[orders.index(first) + 1]


def _fault(trace, exchanges):
    decision = trace.find("pm_decision")[0]
    first = _first_order(exchanges, decision)
    retry = _order_retry(exchanges, first)
    dropped = trace.find("message_dropped", kind="order_result")
    retries = trace.find("pm_order_retry")
    close = _close(exchanges)
    rows = [o for o in close.result["orders"]
            if o["ticker"] == decision.subject]
    ok = (len(dropped) == 1 and dropped[0].subject == first.reply_id
          and first.result["approved"] is True and not first.arrived
          and len(retries) == 1
          and first.reply_sent_at < trace.index(dropped[0])
          < trace.index(retries[0]) < retry.sent_at
          and retry.args == first.args
          and retry.request_id != first.request_id
          and retry.result["approved"] is False
          and retry.result["rule_triggered"] == "DUPLICATE_POSITION"
          and retry.arrived
          and [(o["approved"], o["rule_triggered"]) for o in rows]
          == [(1, None), (0, "DUPLICATE_POSITION")])
    return _check("fault_and_retry", ok,
                  _ids(dropped + retries) + first.evidence + retry.evidence,
                  "the approved order's reply must be lost, the same order "
                  "retried and refused, leaving exactly one fill")


def _stages(spec, trace: Trace, lost_reply: bool):
    if not trace.find("desk_connected"):
        names = STAGES + (FAULT_STAGES if lost_reply else ())
        return [_missing(n, "project not connected; not judged") for n in names]
    try:
        c = _cast(spec)
        trace = _attributed(trace, c)
        x = _exchanges(trace, c)
    except _DAMAGED:
        c, x = None, []
    checks = {
        "connection": lambda: _connection(trace),
        "discovery_and_views": lambda: _discovery(trace, x, c),
        "decision_and_fill": lambda: _decision(trace, x, lost_reply),
        "risk_refusals": lambda: _risk(trace, x, c),
        "scoring_and_chain": lambda: _scoring(trace, x, c),
        "pm_knowledge": lambda: _knowledge(trace, x, lost_reply),
    }
    if lost_reply:
        checks["fault_and_retry"] = lambda: _fault(trace, x)
    return [_guarded(n, check) for n, check in checks.items()]


@validator("otd_workflow")
def otd_workflow(spec, trace: Trace):
    return _stages(spec, trace, lost_reply=False)


@validator("otd_lost_reply")
def otd_lost_reply(spec, trace: Trace):
    return _stages(spec, trace, lost_reply=True)
