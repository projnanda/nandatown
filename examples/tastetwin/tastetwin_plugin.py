"""Taste Twin in the Lab: a shared taste memory used by independent agents.

Taste Twin (https://cafe-taste-twin.lovable.app) is an MCP server that ranks
cafes the way one person would and learns from what that person picks. It is
hosted separately; this file only connects it to the town.

Roles (loaded through the scenario's ``plugin_files:``):

* ``tt_adapter``  publishes a signed ``taste.rank`` card and turns town
  messages into MCP tool calls (get_profile_context, get_taste_profile,
  rank_options, record_feedback, record_choice).
* ``tt_cafe``     acts for one cafe: publishes a ``cafe.listing`` card with
  its own facts. Cafes never talk to Taste Twin.
* ``tt_planner``  acts for one person (an assistant such as Claude or a
  calendar app): finds cafes and Taste Twin through the registry, asks for
  a ranking and, if it is the one talking to the person, reports the pick.
* ``tt_user``     stands in for the person: picks a cafe and says one thing.

Environment (read by the adapter, recorded in the trace):

* ``TASTETWIN_MODE``     ``replay`` (default) serves recorded responses from
  ``fixtures/``; ``live`` calls the real server.
* ``TASTETWIN_DEDUPE``   ``1`` (default) drops a redelivered choice message by
  message id; ``0`` forwards every delivery to record_choice.
* ``TASTETWIN_RECORD``   in live mode, write the reduced responses to this
  path so the run can be replayed without credentials.
* ``TASTETWIN_FIXTURE``  replay from this file instead of the default.

Recorded responses keep only what the agents use: no profile text, quotes
or weights. Replay checks Town-side behaviour against those responses; it
is not evidence that the live service is compatible.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import Trace, validator

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_URL = os.environ.get("TASTETWIN_URL", "https://cafe-taste-twin.lovable.app/mcp")
PROTOCOL_VERSION = "2025-06-18"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CLOSE_CALL_GAP = 0.05


# -- talking to Taste Twin ----------------------------------------------


class ToolFailed(Exception):
    """The service answered, but with an error for this call."""


def _load_auth():
    spec = importlib.util.spec_from_file_location(
        "tastetwin_auth", os.path.join(HERE, "tt_auth.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveClient:
    """Minimal MCP client over Streamable HTTP with a bearer token."""

    def __init__(self, url: str = SERVER_URL):
        self.url = url
        self.token = _load_auth().token()
        self.session: str | None = None
        self.next_id = 0
        self.server_info: dict[str, Any] = {}

    def _post(self, message: dict[str, Any]) -> dict[str, Any] | None:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {self.token}",
            "user-agent": "nandatown-tastetwin-example/0.1",
        }
        if self.session:
            headers["mcp-session-id"] = self.session
        req = urllib.request.Request(self.url, json.dumps(message).encode(), headers)
        with urllib.request.urlopen(req, timeout=180) as resp:
            self.session = resp.headers.get("mcp-session-id") or self.session
            body = resp.read().decode()
        if "id" not in message or not body.strip():
            return None
        if body.lstrip().startswith("{"):
            return json.loads(body)
        data = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        reply = self._post({"jsonrpc": "2.0", "id": self.next_id,
                            "method": method, "params": params})
        if reply is None or "error" in reply:
            raise ToolFailed(f"{method}: {(reply or {}).get('error')}")
        return reply["result"]

    def connect(self) -> None:
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "nandatown-tastetwin", "version": "0.1"}})
        self.server_info = result.get("serverInfo", {})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            raise ToolFailed(text[:300] or f"{name} failed")
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and len(structured) == 1:
            return next(iter(structured.values()))
        return json.loads(text)


def _scalars(obj: dict[str, Any]) -> dict[str, Any]:
    """Top-level numbers, booleans and ids only: no free text leaves."""
    out = {}
    for key, value in obj.items():
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str) and (UUID_RE.match(value) or len(value) <= 24) \
                and not any(w in key for w in ("note", "quote", "feedback", "text", "reason")):
            out[key] = value
    return out


class TasteTwinService:
    """Live calls reduced to what the agents use, or the same from a fixture."""

    def __init__(self, mode: str, record_path: str | None, fixture_path: str | None,
                 dedupe: bool = True):
        self.mode = mode
        self.dedupe = dedupe
        self.record_path = record_path
        self.recorded: list[dict[str, Any]] = []
        self.replay: list[dict[str, Any]] = []
        self.fixture_info: dict[str, Any] = {}
        self.client: LiveClient | None = None
        if mode == "live":
            self.client = LiveClient()
            self.client.connect()
        else:
            with open(fixture_path) as f:
                data = json.load(f)
            self.fixture_info = data.get("recorded_with", {})
            self.replay = list(data["calls"])

    # Each operation returns a small dict; live results are recorded as-is.
    def run(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.mode != "live":
            if not self.replay:
                raise ToolFailed(f"replay fixture has no response left for {op}")
            entry = self.replay.pop(0)
            if entry["op"] != op:
                raise ToolFailed(f"replay fixture expected {entry['op']}, got {op}")
            if not entry["ok"]:
                raise ToolFailed(entry["error"])
            return entry["response"]
        try:
            response = getattr(self, "_live_" + op)(**args)
        except (ToolFailed, urllib.error.URLError, TimeoutError) as exc:
            self.recorded.append({"op": op, "ok": False, "error": str(exc)[:300]})
            self.save_recording()
            raise ToolFailed(str(exc)[:300]) from exc
        self.recorded.append({"op": op, "ok": True, "response": response})
        self.save_recording()
        return response

    def _live_context(self, situation, phrases):
        ctx = self.client.tool("get_profile_context", {"situation": situation})
        profile = self.client.tool("get_taste_profile", {})
        said = json.dumps([profile.get("recent_stated"), ctx.get("universal_rules")]).lower()
        return {
            "choice_count": profile.get("choice_count"),
            "confidence": profile.get("confidence"),
            "matched_conversations": ctx.get("matched_conversations"),
            "stated_matches": {p: p.lower() in said for p in phrases},
            # Real values in the run, redacted on export, never recorded.
            "weights": "[not recorded]",
            "_weights_live": ctx.get("weights"),
        }

    def _live_rank(self, situation, candidates):
        ranking = self.client.tool("rank_options",
                                   {"situation": situation, "candidates": candidates})
        return {
            "request_id": ranking.get("request_id"),
            "results": [{"name": r.get("place", {}).get("name"),
                         "score": r.get("score"),
                         "confidence": r.get("confidence"),
                         "reason": r.get("reason")}
                        for r in ranking.get("results", [])],
            "dropped": [d.get("place") for d in ranking.get("dropped", [])],
            "close_call": ranking.get("close_call"),
            "scored_by": (ranking.get("scored_by") or {}).get("model"),
        }

    def _live_feedback(self, type, feedback, about_place=None):
        args = {"type": type, "feedback": feedback}
        if about_place:
            args["about_place"] = about_place
        return _scalars(self.client.tool("record_feedback", args))

    def _live_choice(self, request_id, picked_place, outcome):
        return _scalars(self.client.tool("record_choice", {
            "request_id": request_id, "picked_place": picked_place, "outcome": outcome}))

    def save_recording(self) -> None:
        if not self.record_path:
            return
        calls = []
        for entry in self.recorded:
            entry = dict(entry)
            if entry.get("ok") and entry["op"] == "context":
                entry["response"] = {k: v for k, v in entry["response"].items()
                                     if not k.startswith("_")}
            calls.append(entry)
        info = {
            "server": self.client.server_info if self.client else {},
            "url": SERVER_URL,
            "dedupe": self.dedupe,
            "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": "Reduced responses only: counts, ids, scores and reasons."
                    " No profile text, quotes or weights.",
        }
        os.makedirs(os.path.dirname(os.path.abspath(self.record_path)), exist_ok=True)
        with open(self.record_path, "w") as f:
            json.dump({"recorded_with": info, "calls": calls}, f, indent=2)
            f.write("\n")


# -- agents ---------------------------------------------------------------


@role("tt_adapter")
class TasteTwinAdapter(SimAgent):
    """Taste Twin's representative in the town."""

    def on_start(self):
        self.mode = os.environ.get("TASTETWIN_MODE", "replay")
        self.dedupe = os.environ.get("TASTETWIN_DEDUPE", "1") != "0"
        default_fixture = os.path.join(
            HERE, "fixtures", "dedupe_on.json" if self.dedupe else "dedupe_off.json")
        fixture = os.environ.get("TASTETWIN_FIXTURE", default_fixture)
        record = os.environ.get("TASTETWIN_RECORD") if self.mode == "live" else None
        self.seen_choices: dict[str, dict[str, Any]] = {}
        try:
            self.service = TasteTwinService(self.mode, record, fixture, self.dedupe)
        except Exception as exc:  # no token, no fixture, server unreachable
            self.service = None
            self.api.observe("tt_adapter_unavailable", self.name,
                             {"mode": self.mode, "error": str(exc)[:200]})
            return
        info = (self.service.client.server_info if self.service.client
                else self.service.fixture_info.get("server", {}))
        self.api.observe("tt_adapter_ready", self.name, {
            "mode": self.mode, "dedupe": self.dedupe,
            "server": info.get("name"), "server_version": info.get("version"),
            "fixture_recorded_at": self.service.fixture_info.get("recorded_at"),
        })
        self.api.register(["taste.rank"], {
            "service": "cafe-taste-twin", "protocol": "mcp", "url": SERVER_URL,
            "tools": ["get_profile_context", "get_taste_profile", "rank_options",
                      "record_feedback", "record_choice"],
        })

    def _call(self, msg, op: str, args: dict[str, Any], extra: dict[str, Any] | None = None):
        detail = {"op": op, "caller": msg["sender"], "message_id": msg["message_id"],
                  "mode": self.mode}
        detail.update(extra or {})
        if self.service is None:
            self.api.observe("tt_mcp_call", msg["message_id"],
                             dict(detail, ok=False, error="adapter unavailable"))
            return None
        try:
            out = self.service.run(op, args)
        except ToolFailed as exc:
            self.api.observe("tt_mcp_call", msg["message_id"],
                             dict(detail, ok=False, error=str(exc)[:200]))
            return None
        self.api.observe("tt_mcp_call", msg["message_id"], dict(detail, ok=True))
        return out

    def handle_context_request(self, msg):
        body = msg["body"]
        out = self._call(msg, "context", {"situation": body["situation"],
                                          "phrases": body.get("phrases", [])})
        if out is None:
            self.api.reply(msg, "service_error", {"op": "context"})
            return
        live_weights = out.get("_weights_live")
        reply = {k: v for k, v in out.items() if not k.startswith("_")}
        if live_weights is not None:
            reply["weights"] = live_weights
        self.api.reply(msg, "context", reply)

    def handle_rank_request(self, msg):
        body = msg["body"]
        out = self._call(msg, "rank", {"situation": body["situation"],
                                       "candidates": body["candidates"]})
        if out is None:
            self.api.reply(msg, "service_error", {"op": "rank"})
            return
        self.api.reply(msg, "ranking", out)

    def handle_feedback_report(self, msg):
        body = msg["body"]
        out = self._call(msg, "feedback", {"type": body.get("type", "preference"),
                                           "feedback": body["feedback"],
                                           "about_place": body.get("about_place")})
        self.api.reply(msg, "feedback_recorded", {"saved": bool(out and out.get("saved"))})

    def handle_choice_report(self, msg):
        body = msg["body"]
        if self.dedupe and msg["message_id"] in self.seen_choices:
            self.api.observe("duplicate_recognized", msg["message_id"],
                             {"kind": "choice_report", "request_id": body["request_id"]})
            self.api.reply(msg, "choice_recorded", self.seen_choices[msg["message_id"]])
            return
        out = self._call(msg, "choice",
                         {"request_id": body["request_id"],
                          "picked_place": body["picked_place"],
                          "outcome": body["outcome"]},
                         {"request_id": body["request_id"], "picked": body["picked_place"]})
        reply = {"ok": out is not None, "request_id": body["request_id"]}
        self.seen_choices[msg["message_id"]] = reply
        self.api.reply(msg, "choice_recorded", reply)


@role("tt_cafe")
class Cafe(SimAgent):
    """Acts for one cafe: publishes its own listing and nothing else."""

    def on_start(self):
        facts = {k: v for k, v in self.config.items() if k != "balance_cents"}
        self.api.register(["cafe.listing"], facts)


@role("tt_planner")
class Planner(SimAgent):
    """An assistant acting for one person."""

    def on_start(self):
        self.api.later(float(self.config.get("start_at", 0.5)), self.begin)

    def begin(self):
        c = self.config
        pending = self.api.recall("pending_request_id")
        self.api.observe("tt_private_memory_checked", self.name,
                         {"key": "pending_request_id", "found": pending is not None})
        cafes = self.api.lookup("cafe.listing")
        self.candidates = [dict(card["facts"]) for card in cafes][:10]
        self.api.observe("tt_candidates_found", self.name, {
            "count": len(self.candidates),
            "names": [cand.get("name") for cand in self.candidates]})
        services = self.api.lookup("taste.rank")
        if not services or not self.candidates:
            self.api.observe("tt_gave_up", self.name,
                             {"reason": "no taste service" if not services else "no cafes"})
            return
        service = services[0]["name"]
        self.service = service
        self.api.remember("taste_service", service)
        self.api.observe("tt_service_found", service, {"acts_for": c["acts_for"]})
        self.api.send(service, "context_request",
                      {"situation": c["situation"], "phrases": c.get("check_stated", [])})

    def handle_context(self, msg):
        body = msg["body"]
        self.api.observe("tt_context_received", self.name, {
            "acts_for": self.config["acts_for"],
            "choice_count": body.get("choice_count"),
            "confidence": body.get("confidence"),
            "stated_matches": body.get("stated_matches", {}),
            "weights": body.get("weights"),
        })
        rank_situation = dict(self.config["situation_for_rank"])
        self.api.send(msg["sender"], "rank_request",
                      {"situation": rank_situation, "candidates": self.candidates})

    def handle_ranking(self, msg):
        body = msg["body"]
        self.api.observe("tt_ranking_received", self.name, {
            "acts_for": self.config["acts_for"],
            "request_id": body.get("request_id"),
            "ranked": [{"name": r.get("name"), "score": r.get("score")}
                       for r in body.get("results", [])],
            "dropped": body.get("dropped", []),
            "close_call": body.get("close_call"),
            "scored_by": body.get("scored_by"),
            "candidates": [cand.get("name") for cand in self.candidates],
        })
        self.api.remember("pending_request_id", body.get("request_id"))
        self.api.remember("last_options", [r.get("name") for r in body.get("results", [])])
        user = self.config.get("ask_user")
        if user:
            self.api.send(user, "options", {
                "request_id": body.get("request_id"),
                "options": [r.get("name") for r in body.get("results", [])],
                "candidates": [cand.get("name") for cand in self.candidates]})

    def handle_user_pick(self, msg):
        body = msg["body"]
        if body.get("said"):
            self.api.send(self.service, "feedback_report", {
                "type": "preference", "feedback": body["said"],
                "about_place": body.get("said_about")})
        self.api.send(self.service, "choice_report", {
            "request_id": self.api.recall("pending_request_id"),
            "picked_place": body["picked"], "outcome": body["outcome"]})

    def handle_feedback_recorded(self, msg):
        self.api.observe("tt_feedback_ack", self.name, {"saved": msg["body"].get("saved")})

    def handle_choice_recorded(self, msg):
        self.api.observe("tt_choice_ack", self.name, dict(msg["body"]))

    def handle_service_error(self, msg):
        self.api.observe("tt_service_error", self.name, dict(msg["body"]))


@role("tt_user")
class SimulatedUser(SimAgent):
    """Stands in for the person: picks a cafe and says one lasting thing."""

    def handle_options(self, msg):
        c = self.config
        self.api.observe("tt_user_decided", self.name, {
            "picked": c["picks"], "was_in_top3": c["picks"] in msg["body"]["options"]})
        self.api.reply(msg, "user_pick", {
            "picked": c["picks"], "outcome": c.get("outcome", "loved"),
            "said": c.get("says"), "said_about": c.get("says_about")})


# -- evaluation -------------------------------------------------------------


def _stage(name, status, evidence, note):
    return StageResult(name=name, status=status, evidence=evidence[:8], note=note)


def _planners(spec):
    return [a for a in spec.agents if a.role == "tt_planner"]


@validator("tastetwin_shared_taste")
def tastetwin_shared_taste(spec, trace: Trace) -> list[StageResult]:
    stages: list[StageResult] = []
    planners = _planners(spec)
    writers = [a for a in planners if a.config.get("ask_user")]
    readers = [a for a in planners if not a.config.get("ask_user")]
    cafes = [a.name for a in spec.agents if a.role == "tt_cafe"]
    adapters = [a.name for a in spec.agents if a.role == "tt_adapter"]
    ready = trace.find("tt_adapter_ready")

    # 1. Mode: replay is labelled as such, never as live compatibility.
    if not ready:
        stages.append(_stage("live_service", "not_enough_evidence", trace.ids("tt_adapter_unavailable"),
                             "the adapter never reached Taste Twin or its fixture"))
    elif ready[0].detail.get("mode") == "live":
        calls = trace.find("tt_mcp_call")
        bad = [e.event_id for e in calls if not e.detail.get("ok")]
        stages.append(_stage(
            "live_service", "failed" if bad else ("passed" if calls else "not_enough_evidence"),
            bad or [ready[0].event_id] + [e.event_id for e in calls],
            "a live MCP call failed" if bad else
            f"every MCP call to {ready[0].detail.get('server')} {ready[0].detail.get('server_version')} succeeded"))
    else:
        stages.append(_stage("live_service", "not_tested", [ready[0].event_id],
                             "replay mode: recorded responses, not a live compatibility observation"))

    # 2. Discovery through the registry, by every planner.
    found, missing = [], []
    for p in planners:
        svc = trace.find("tt_service_found", observer=p.name)
        cands = trace.find("tt_candidates_found", observer=p.name)
        if svc and svc[0].subject in adapters and cands and cands[0].detail.get("count") == len(cafes):
            found += [svc[0].event_id, cands[0].event_id]
        else:
            missing.append(p.name)
    cards = trace.find("card_registered")
    taste_cards = [e for e in cards if "taste.rank" in e.detail.get("capabilities", [])]
    if not planners:
        stages.append(_stage("discovery", "not_enough_evidence", [], "no planners declared"))
    else:
        ok = not missing and len(taste_cards) == 1
        stages.append(_stage(
            "discovery", "passed" if ok else "failed",
            [e.event_id for e in taste_cards] + found,
            f"{len(planners)} planners found the verified taste.rank card and all {len(cafes)} cafe listings"
            if ok else f"discovery incomplete for {missing or 'the taste.rank card'}"))

    # 3. Ranking contract on every ranking a planner received.
    rankings = trace.find("tt_ranking_received")
    problems = []
    for e in rankings:
        d = e.detail
        ranked, names = d.get("ranked") or [], set(d.get("candidates") or [])
        scores = [r.get("score") for r in ranked]
        if not 1 <= len(ranked) <= 3:
            problems.append(f"{e.observer}: {len(ranked)} results")
        if not isinstance(d.get("request_id"), str) or not UUID_RE.match(d["request_id"]):
            problems.append(f"{e.observer}: request_id is not a UUID")
        if any(r.get("name") not in names for r in ranked):
            problems.append(f"{e.observer}: a result is not one of the candidates sent")
        if any(x not in names for x in d.get("dropped") or []):
            problems.append(f"{e.observer}: a dropped place is not one of the candidates sent")
        if any(not isinstance(s, (int, float)) for s in scores) or scores != sorted(scores, reverse=True):
            problems.append(f"{e.observer}: scores missing or not in descending order")
        elif len(scores) >= 2:
            gap = scores[0] - scores[1]
            if abs(gap - CLOSE_CALL_GAP) > 0.001 and bool(d.get("close_call")) != (gap < CLOSE_CALL_GAP):
                problems.append(f"{e.observer}: close_call={d.get('close_call')} but the top-two gap is {gap:.3f}")
    if not rankings:
        stages.append(_stage("ranking_contract", "not_enough_evidence", [], "no ranking was received"))
    else:
        stages.append(_stage(
            "ranking_contract", "failed" if problems else "passed", [e.event_id for e in rankings],
            problems[0] if problems else
            f"{len(rankings)} rankings: 1-3 results from the sent candidates, valid request_id,"
            " descending scores, close_call consistent with the top-two gap"))

    # 4. The recorded choice is bound to the ranking the person saw.
    choice_calls = [e for e in trace.find("tt_mcp_call", op="choice") if e.detail.get("ok")]
    writer_rankings = [e for w in writers for e in rankings if e.observer == w.name]
    if not writer_rankings or not choice_calls:
        stages.append(_stage("choice_binding", "not_enough_evidence",
                             [e.event_id for e in writer_rankings + choice_calls],
                             "needs a ranking for the person and a successful record_choice"))
    else:
        ranking = writer_rankings[0]
        call = choice_calls[0]
        ok = (call.detail.get("request_id") == ranking.detail.get("request_id")
              and call.detail.get("picked") in (ranking.detail.get("candidates") or []))
        stages.append(_stage(
            "choice_binding", "passed" if ok else "failed", [ranking.event_id, call.event_id],
            "record_choice used the ranking's request_id and a cafe that was offered"
            if ok else "record_choice did not match the ranking the person saw"))

    # 5. A redelivered choice is applied once.
    dup = trace.find("message_duplicated", kind="choice_report")
    contexts = trace.find("tt_context_received")
    before = [e for w in writers for e in contexts if e.observer == w.name]
    after = [e for r in readers for e in contexts if e.observer == r.name]
    if not dup:
        stages.append(_stage("choice_applied_once", "not_tested", [],
                             "no duplicate delivery was injected"))
    elif not before or not after or before[0].detail.get("choice_count") is None \
            or after[0].detail.get("choice_count") is None:
        stages.append(_stage("choice_applied_once", "not_enough_evidence",
                             [e.event_id for e in dup], "needs a choice count before and after"))
    else:
        delta = after[0].detail["choice_count"] - before[0].detail["choice_count"]
        recognized = trace.ids("duplicate_recognized", kind="choice_report")
        evidence = [e.event_id for e in dup] + recognized + [e.event_id for e in choice_calls] \
            + [before[0].event_id, after[0].event_id]
        stages.append(_stage(
            "choice_applied_once", "passed" if delta == 1 else "failed", evidence,
            f"the choice message was delivered twice; choice count moved by {delta}"
            f" ({len(choice_calls)} record_choice call(s) succeeded)"))

    # 6. Shared memory: a planner never told anything sees the first one's writes.
    if not before or not after:
        stages.append(_stage("shared_memory", "not_enough_evidence",
                             [e.event_id for e in before + after],
                             "needs a context read by the reporting planner and by an independent one"))
    else:
        b, a = before[0].detail, after[0].detail
        direct = [e for e in trace.find("message_sent")
                  if e.observer in {w.name for w in writers}
                  and e.detail.get("to") in {r.name for r in readers}]
        private = [e for r in readers for e in trace.find("tt_private_memory_checked", observer=r.name)]
        same_person = {w.config.get("acts_for") for w in writers} == {r.config.get("acts_for") for r in readers}
        stated = a.get("stated_matches") or {}
        ok = (same_person and not direct and (a.get("choice_count") or 0) > (b.get("choice_count") or 0)
              and bool(stated) and all(stated.values())
              and all(not e.detail.get("found") for e in private))
        stages.append(_stage(
            "shared_memory", "passed" if ok else "failed",
            [before[0].event_id, after[0].event_id] + [e.event_id for e in private],
            f"{after[0].observer} got no message from the reporting planner and an empty private memory,"
            f" yet saw choice count {b.get('choice_count')} -> {a.get('choice_count')} and the stated"
            f" preference {sorted(stated)}" if ok else
            f"{after[0].observer} did not see the earlier writes (count {b.get('choice_count')} ->"
            f" {a.get('choice_count')}, stated {stated}, direct messages {len(direct)})"))
    return stages
