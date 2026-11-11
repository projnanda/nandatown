"""Common Ground in NANDA Town: three people's agents plan a meetup through
the real Common Ground room.

Common Ground (https://github.com/javiersgp/common-ground-mvp) is a neutral
MCP server where each person's agent posts only typed fields (free windows,
per-venue travel and fit scores). The room filters venues against the brief's
must-haves and picks the option whose worst-off person scores highest.

Roles in this file:

- ``cg_room_adapter`` publishes a signed ``meetup.plan`` card and turns Town
  messages into MCP calls on the room. It maps the (signed) Town sender to
  that person's room token, so no agent ever holds another person's token.
- ``cg_person`` acts for one person. It finds the room and the other members
  through the registry, keeps its person's home, travel limit and taste
  private (declared in ``redact_fields``), and posts only what the room asks.

Modes (``CG_MODE``):

- ``live`` calls the room at ``CG_ROOM_URL`` with tokens from
  ``CG_ROOM_AGENTS`` (the room's agents.json). ``run_check.py`` starts the
  room and sets these. With ``CG_RECORD=path`` it also writes every call and
  answer to a fixture.
- ``replay`` (the default) answers from ``fixtures/<scenario>.json``,
  recorded from the live room, so reviewers need no Common Ground checkout.
  Replay reports ``live_room`` as not_tested: it is not live evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any

import httpx

from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import (StageResult, Trace, _check, _missing,
                                      validator)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOM_TOOLS = ["create_plan", "join_plan", "post_availability",
              "post_scores", "wait_for_result"]
CALL_KINDS = {f"cg_{t}": t for t in ROOM_TOOLS}

# Travel estimate, as in Common Ground's private tools: straight line x 1.3,
# a speed per mode, plus fixed minutes (locking a bike, waiting for transit).
SPEED_KMH = {"walk": 4.8, "bike": 14.0, "transit": 20.0}
FIXED_MIN = {"walk": 0, "bike": 3, "transit": 9}


def _digest(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


# -- a minimal MCP client (streamable HTTP, JSON-RPC 2.0) ---------------


class RoomClient:
    """Just enough MCP to call the room: initialize, list tools, call."""

    PROTOCOL = "2025-06-18"

    def __init__(self, url: str):
        self.url = url
        self.session: str | None = None
        self.http = httpx.Client(timeout=90)
        self._id = 0
        self.server_info: dict[str, Any] = {}

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.PROTOCOL}
        if self.session:
            headers["mcp-session-id"] = self.session
        resp = self.http.post(self.url, json=payload, headers=headers)
        resp.raise_for_status()
        self.session = resp.headers.get("mcp-session-id", self.session)
        if "id" not in payload or not resp.content:
            return None
        if resp.headers.get("content-type", "").startswith(
                "text/event-stream"):
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if msg.get("id") == payload["id"]:
                        return msg
            raise RuntimeError("no JSON-RPC response in the event stream")
        return resp.json()

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        self._id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._id,
                          "method": method, "params": params})
        if msg is None or "error" in msg:
            raise RuntimeError(f"{method}: {msg and msg.get('error')}")
        return msg["result"]

    def initialize(self) -> list[str]:
        result = self._request("initialize", {
            "protocolVersion": self.PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "nandatown-common-ground-adapter",
                           "version": "0.1.0"}})
        self.server_info = result.get("serverInfo", {})
        self._post({"jsonrpc": "2.0",
                    "method": "notifications/initialized"})
        tools = self._request("tools/list", {})
        return sorted(t["name"] for t in tools.get("tools", []))

    def call(self, tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
        result = self._request("tools/call",
                               {"name": tool, "arguments": args})
        text = "".join(c.get("text", "") for c in result.get("content", [])
                       if c.get("type") == "text")
        if result.get("isError"):
            return False, text
        return True, json.loads(text)


# -- the adapter ---------------------------------------------------------


@role("cg_room_adapter")
class RoomAdapter(SimAgent):
    """The only role that talks to Common Ground."""

    def on_start(self):
        self.mode = os.environ.get("CG_MODE", "replay")
        self.scenario = self.api._engine.spec.name
        self.seats: dict[str, str] = self.config["seats"]  # Town agent -> person
        self.recorded: list[dict[str, Any]] = []
        self.calls_seen: set[str] = set()
        if self.mode == "live":
            agents = json.load(open(os.environ["CG_ROOM_AGENTS"]))
            self.tokens = {pid: tok for tok, pid in agents["tokens"].items()}
            self.client = RoomClient(os.environ["CG_ROOM_URL"])
            tools = self.client.initialize()
            server = self.client.server_info
        else:
            path = os.environ.get("CG_FIXTURE") or os.path.join(
                HERE, "fixtures", f"{self.scenario}.json")
            fixture = json.load(open(path))
            self.replay = list(fixture["calls"])
            tools, server = fixture["tools"], fixture["server"]
            self.fixture_meta = {"tools": tools, "server": server}
        self.tools = tools
        self.server = server
        self.api.observe("cg_room_interface", "common-ground", {
            "mode": self.mode, "server": server.get("name", ""),
            "server_version": server.get("version", ""), "tools": tools})
        self.api.register(["meetup.plan"],
                          {"service": "Common Ground room",
                           "interface": "mcp.streamable-http",
                           "tools": [t for t in tools if t in ROOM_TOOLS]})

    def _call_room(self, person: str, tool: str,
                   args: dict[str, Any]) -> tuple[bool, Any]:
        if self.mode == "live":
            ok, out = self.client.call(
                tool, dict(args, agent_token=self.tokens[person]))
            self.recorded.append({"person": person, "tool": tool,
                                  "args_digest": _digest(args),
                                  "ok": ok, "out": out})
            record = os.environ.get("CG_RECORD")
            if record:
                with open(record, "w") as f:
                    json.dump({"scenario": self.scenario,
                               "server": self.server, "tools": self.tools,
                               "calls": self.recorded}, f, indent=1)
            return ok, out
        want = {"person": person, "tool": tool, "args_digest": _digest(args)}
        if not self.replay:
            return False, "replay: no recorded answer left for this call"
        rec = self.replay.pop(0)
        got = {k: rec[k] for k in want}
        if got != want:
            return False, f"replay mismatch: expected {got}, got {want}"
        return rec["ok"], rec["out"]

    def on_message(self, msg):
        tool = CALL_KINDS.get(msg["kind"])
        if tool is None:
            return super().on_message(msg)
        body = msg["body"]
        call_id = body.get("call_id", "")
        person = self.seats.get(msg["sender"])
        if person is None:
            self.api.observe("cg_refused", msg["sender"],
                             {"tool": tool, "reason": "no seat in this room"})
            self.api.reply(msg, "cg_result", {
                "call_id": call_id, "tool": tool, "ok": False,
                "error": "no seat in this room"})
            return
        args = body.get("args", {})
        repeat = call_id in self.calls_seen
        self.calls_seen.add(call_id)
        try:
            ok, out = self._call_room(person, tool, args)
        except Exception as exc:  # the room is down or unreachable
            ok, out = False, f"room unreachable: {type(exc).__name__}"
        detail: dict[str, Any] = {"tool": tool, "person": person,
                                  "call_id": call_id, "repeat": repeat,
                                  "ok": ok}
        if not ok:
            detail["error"] = str(out)[:200]
        elif tool == "create_plan":
            detail["plan_id"] = out["plan_id"]
        elif tool == "join_plan":
            detail["candidates"] = len(out["candidate_venues"])
        elif tool == "post_availability":
            detail["windows"] = args["windows"]
        elif tool == "post_scores":
            detail["scores"] = args["scores"]
        elif tool == "wait_for_result":
            detail["status"] = out.get("status")
            if out.get("status") == "waiting":
                detail["waiting_for"] = out.get("waiting_for", [])
        self.api.observe("cg_room_call", person, detail)
        reply = {"call_id": call_id, "tool": tool, "ok": ok}
        reply["result" if ok else "error"] = out
        self.api.reply(msg, "cg_result", reply)


# -- a person's agent ------------------------------------------------------


def _km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _travel_score(minutes: int, limit: int) -> int:
    r = minutes / limit
    if r > 1:
        return 0
    for cap, s in ((0.2, 5), (0.4, 4), (0.6, 3), (0.8, 2)):
        if r <= cap:
            return s
    return 1


@role("cg_person")
class Person(SimAgent):
    """A scripted stand-in for one person's agent (Claude Code, Codex or
    Hermes in the real project). Deterministic, so runs repeat."""

    def on_start(self):
        c = self.config
        self.person = c["person"]
        self.seq = 0
        self.pending: str | None = None
        self.polls = 0
        self.plan_id: str | None = None
        self.room: str | None = None
        self.candidates: dict[str, dict[str, Any]] = {}
        self.api.register(["meetup.member"], {"person": self.person})
        if c.get("organizer"):
            self.api.later(0.5, self.organize)

    # -- calls through the adapter --

    def call(self, tool: str, args: dict[str, Any]) -> None:
        self.seq += 1
        self.pending = f"{self.name}-{self.seq}"
        self.api.send(self.room, f"cg_{tool}",
                      {"call_id": self.pending, "args": args})
        timeout = self.config.get("reply_timeout", 5.0)
        call_id = self.pending
        self.api.later(timeout, lambda: self._check_reply(call_id, tool))

    def _check_reply(self, call_id: str, tool: str) -> None:
        if self.pending == call_id:
            self.pending = None
            self.api.observe("cg_no_reply", self.person,
                             {"tool": tool, "call_id": call_id,
                              "action": "stopped (no retry)"})

    def handle_cg_result(self, msg):
        body = msg["body"]
        if body.get("call_id") != self.pending:
            self.api.observe("cg_stale_reply_ignored", self.person,
                             {"call_id": body.get("call_id"),
                              "tool": body.get("tool"),
                              "ok": body.get("ok")})
            return
        self.pending = None
        if not body["ok"]:
            self.api.observe("cg_call_failed", self.person,
                             {"tool": body["tool"],
                              "error": str(body.get("error"))[:200]})
            return
        getattr(self, "after_" + body["tool"])(body["result"])

    # -- the workflow --

    def organize(self):
        rooms = self.api.lookup("meetup.plan")
        members = [m for m in self.api.lookup("meetup.member")
                   if m["name"] != self.name]
        self.api.observe("cg_room_found", self.person, {
            "rooms": [r["name"] for r in rooms],
            "members": sorted(m["facts"]["person"] for m in members)})
        if not rooms:
            self.api.observe("cg_gave_up", self.person,
                             {"reason": "no meetup.plan service found"})
            return
        self.room = rooms[0]["name"]
        self.members = [m["name"] for m in members]
        brief = dict(self.config["brief"])
        brief["invitees"] = [m["facts"]["person"] for m in members]
        self.call("create_plan", brief)

    def after_create_plan(self, result):
        self.plan_id = result["plan_id"]
        for m in self.members:
            self.api.send(m, "cg_invite", {"plan_id": self.plan_id,
                                           "room": self.room})
        self.call("join_plan", {"plan_id": self.plan_id})

    def handle_cg_invite(self, msg):
        if self.plan_id:
            return
        self.plan_id = msg["body"]["plan_id"]
        self.room = msg["body"]["room"]
        self.call("join_plan", {"plan_id": self.plan_id})

    def after_join_plan(self, result):
        self.candidates = {v["venue_id"]: v
                           for v in result["candidate_venues"]}
        self.call("post_availability", {
            "plan_id": self.plan_id,
            "windows": [{"start": s, "end": e}
                        for s, e in self.config["windows"]]})

    def after_post_availability(self, result):
        if result.get("format") == "video":
            self.call("wait_for_result", {"plan_id": self.plan_id,
                                          "wait_seconds": 1})
            return
        c = self.config
        home, mode, limit = c["home"], c["travel_mode"], c["max_travel_min"]
        tags = c["tags"]
        scores = []
        for vid in sorted(self.candidates):
            v = self.candidates[vid]
            minutes = round(_km(home["lat"], home["lon"], v["lat"], v["lon"])
                            * 1.3 / SPEED_KMH[mode] * 60 + FIXED_MIN[mode])
            fit = min(5, 1 + len(set(tags) & set(v["vibe"])))
            scores.append({"venue_id": vid, "travel_minutes": minutes,
                           "travel_score": _travel_score(minutes, limit),
                           "fit_score": fit})
        self.my_scores = {s["venue_id"]: s for s in scores}
        self.call("post_scores", {"plan_id": self.plan_id, "tags": tags,
                                  "scores": scores})

    def after_post_scores(self, result):
        self.call("wait_for_result", {"plan_id": self.plan_id,
                                      "wait_seconds": 1})

    def after_wait_for_result(self, result):
        status = result.get("status")
        if status == "agreed":
            venue = result.get("venue", {}).get("name", "video call")
            vid = next((k for k, v in self.candidates.items()
                        if v["name"] == venue), "video")
            mine = result.get("your_contract", {}).get(
                "your_scores_here", {})
            plan = {"plan_id": self.plan_id, "venue_id": vid,
                    "venue": venue, "start": result["start"],
                    "end": result["end"],
                    "lowest_person_score": result.get("group", {}).get(
                        "lowest_person_score"),
                    "my_total": mine.get("total")}
            self.api.remember("meetup_plan", plan)
            self.api.observe("cg_plan_received", self.person, plan)
            return
        if status == "failed":
            self.api.observe("cg_gave_up", self.person,
                             {"reason": result.get("reason", "failed")})
            return
        self.polls += 1
        if self.polls >= self.config.get("max_polls", 5):
            self.api.observe("cg_gave_up", self.person, {
                "reason": f"no decision after {self.polls} checks",
                "last_status": status,
                "waiting_for": result.get("waiting_for", [])})
            return
        self.api.later(1.0, lambda: self.call(
            "wait_for_result", {"plan_id": self.plan_id, "wait_seconds": 1}))


# -- the check -----------------------------------------------------------


def _inside(start: str, end: str, windows: list[dict[str, str]]) -> bool:
    # Windows here are as the agent posted them: same date, plan time zone,
    # 'YYYY-MM-DDTHH:MM' (this scenario posts no UTC offsets).
    return any(w["start"] <= start and end <= w["end"] for w in windows)


@validator("common_ground")
def common_ground(spec, trace: Trace) -> list[StageResult]:
    people = sorted(a.config["person"] for a in spec.agents
                    if a.role == "cg_person")
    faults = {f.action for f in spec.faults}
    stages = []

    iface = trace.find("cg_room_interface")
    tools = iface[0].detail.get("tools", []) if iface else []
    stages.append(_check(
        "room_interface", set(ROOM_TOOLS) <= set(tools),
        [e.event_id for e in iface],
        f"the room must offer {ROOM_TOOLS} over MCP"))
    live = bool(iface) and iface[0].detail.get("mode") == "live"
    stages.append(StageResult(
        name="live_room", status="passed" if live else "not_tested",
        evidence=[e.event_id for e in iface][:1] if live else [],
        note=("calls answered by a running Common Ground room" if live else
              "replayed from a recorded fixture; not live evidence")))

    found = trace.find("cg_room_found")
    stages.append(_check(
        "discovery",
        bool(found) and bool(found[0].detail.get("rooms"))
        and len(found[0].detail.get("members", [])) == len(people) - 1,
        [e.event_id for e in found],
        "the organizer must find the room and every other member"))

    def ok_calls(tool):
        return [e for e in trace.find("cg_room_call", tool=tool, ok=True)
                if not e.detail.get("repeat")]

    created = ok_calls("create_plan")
    stages.append(_check("plan_created", len(created) == 1,
                         [e.event_id for e in created],
                         "the room must create exactly one plan"))
    joined = ok_calls("join_plan")
    stages.append(_check(
        "all_joined", sorted(e.subject for e in joined) == people,
        [e.event_id for e in joined], "every person's agent must join"))
    avail = {e.subject: e for e in ok_calls("post_availability")}
    scores = {e.subject: e for e in ok_calls("post_scores")}
    stages.append(_check(
        "posts_accepted",
        sorted(avail) == people and sorted(scores) == people,
        [e.event_id for e in list(avail.values()) + list(scores.values())],
        f"the room must accept windows and scores from all of {people}"))

    got = trace.find("cg_plan_received")
    keys = {(e.detail["plan_id"], e.detail["venue_id"], e.detail["start"],
             e.detail["end"]) for e in got}
    stages.append(_check(
        "same_plan_for_everyone",
        sorted(e.subject for e in got) == people and len(keys) == 1,
        [e.event_id for e in got],
        "every person's agent must receive one and the same plan"))

    if got and sorted(avail) == people and sorted(scores) == people:
        plan = got[0].detail
        in_windows = all(_inside(plan["start"], plan["end"],
                                 avail[p].detail["windows"]) for p in people)
        stages.append(_check(
            "slot_in_every_window", in_windows,
            [got[0].event_id] + [avail[p].event_id for p in people],
            "the slot must lie inside a window every person posted"))
        posted = {p: {s["venue_id"]: s for s in scores[p].detail["scores"]}
                  for p in people}
        vid = plan["venue_id"]
        totals = {p: posted[p][vid]["travel_score"]
                  + posted[p][vid]["fit_score"] for p in people
                  if vid in posted[p]}
        mine = {e.subject: e.detail.get("my_total") for e in got}
        fair = (len(totals) == len(people)
                and all(posted[p][vid]["travel_score"] > 0 for p in people)
                and all(mine[p] == totals[p] for p in people)
                and plan["lowest_person_score"] == min(totals.values()))
        stages.append(_check(
            "fairness_consistent", fair,
            [got[0].event_id] + [scores[p].event_id for p in people],
            "the chosen place must be within everyone's travel limit and its"
            " reported scores must match what each agent posted",
            "nobody vetoed the place; each agent's total and the lowest"
            " score match the posted scores"))
    else:
        stages.append(_missing("slot_in_every_window",
                               "no agreed plan to check against the windows"))
        stages.append(_missing("fairness_consistent",
                               "no agreed plan to check against the scores"))
    stages.append(StageResult(
        name="fairest_option", status="not_tested",
        note="the trace has no opening hours, so Town cannot redo the"
             " room's search; Common Ground's verify.py does that check"))

    if "duplicate" in faults:
        dup = trace.find("message_duplicated")
        repeats = [e for e in trace.find("cg_room_call")
                   if e.detail.get("repeat")]
        stale = trace.find("cg_stale_reply_ignored")
        one_each = (sorted(e.subject for e in got) == people
                    and len(keys) == 1)
        stages.append(_check(
            "duplicate_contained",
            bool(dup) and bool(repeats) and bool(stale) and one_each,
            [e.event_id for e in dup + repeats + stale],
            "the repeated call must reach the room, its answer must be"
            " ignored, and every agent must still hold one plan",
            "the room answered the repeated call"
            + (" with an error" if any(not e.detail["ok"] for e in repeats)
               else " again")
            + "; the agent ignored the stale answer and kept one plan"))
    if "drop" in faults:
        dropped = trace.find("message_dropped")
        quiet = trace.find("cg_no_reply")
        gave_up = trace.find("cg_gave_up")
        silent = {e.subject for e in quiet}
        named = all(set(e.detail.get("waiting_for", []))
                    & {s.capitalize() for s in silent} for e in gave_up)
        stages.append(_check(
            "dropout_reported",
            bool(dropped) and bool(silent) and bool(gave_up) and named,
            [e.event_id for e in dropped + quiet + gave_up],
            "after a lost call, the waiting agents must stop and say whom"
            " they were waiting for",
            "the room kept the plan open with no deadline; the other agents"
            " stopped on their own limit and named the missing person"))
    return stages
