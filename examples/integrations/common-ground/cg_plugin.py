"""Common Ground in NANDA Town: three people's real agents plan a meetup
through the real Common Ground room.

Common Ground (https://github.com/javiersgp/common-ground-mvp) is a neutral
MCP server where each person's agent posts only typed fields (free windows,
per-venue travel and fit scores). The room filters venues against the brief's
must-haves and picks the option whose worst-off person scores highest. Each
person also has a private MCP server (calendar, profile, travel estimate).

Roles in this file:

- ``cg_room_adapter`` publishes a signed ``meetup.plan`` card and turns Town
  messages into MCP calls on the room. It maps the (signed) Town sender to
  that person's room token, so no agent ever holds another person's token.
- ``cg_person`` is one person's agent inside Town. Its decisions (which free
  windows to share, 3-4 taste tags, a travel and fit score for every venue)
  come from a real Claude Code agent (``claude -p``, no built-in tools)
  connected only to that person's private Common Ground MCP server. The Town
  side handles discovery, invitations, every message to the room, and polling.

Modes (``CG_MODE``):

- ``live`` calls the room at ``CG_ROOM_URL`` with tokens from
  ``CG_ROOM_AGENTS``, and runs Claude Code against the private server given by
  ``CG_ME_PYTHON``, ``CG_ME_SERVER`` and ``CG_PEOPLE_RUN``. ``run_check.py``
  sets all of these. With ``CG_RECORD=path`` every room call and every agent
  decision is written to a fixture.
- ``replay`` (the default) answers both from ``fixtures/<scenario>.json``,
  recorded from a live run, so reviewers need no checkout, model or login.
  Replay reports ``live_room`` and ``live_agents`` as not_tested.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import (StageResult, Trace, _check, _missing,
                                      validator)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOM_TOOLS = ["create_plan", "join_plan", "post_availability",
              "post_scores", "wait_for_result"]
CALL_KINDS = {f"cg_{t}": t for t in ROOM_TOOLS}
AGENT_MODEL = "sonnet"
AGENT_TIMEOUT = 420


def _digest(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


# -- live calls are recorded; replay answers from the recording -----------

_RUNS: dict[str, dict[str, Any]] = {}


def _state(engine) -> dict[str, Any]:
    st = _RUNS.get(engine.run_id)
    if st is None:
        mode = os.environ.get("CG_MODE", "replay")
        st = {"mode": mode, "scenario": engine.spec.name, "calls": [],
              "server": {}, "tools": []}
        if mode != "live":
            path = os.environ.get("CG_FIXTURE") or os.path.join(
                HERE, "fixtures", f"{engine.spec.name}.json")
            with open(path) as f:
                fixture = json.load(f)
            st.update(replay=list(fixture["calls"]),
                      server=fixture["server"], tools=fixture["tools"])
        _RUNS[engine.run_id] = st
    return st


def _save(st: dict[str, Any]) -> None:
    path = os.environ.get("CG_RECORD")
    if path:
        with open(path, "w") as f:
            json.dump({"scenario": st["scenario"], "server": st["server"],
                       "tools": st["tools"], "calls": st["calls"]}, f,
                      indent=1)


def _live_or_replay(engine, person: str, tool: str, args: dict[str, Any],
                    live) -> tuple[bool, Any, dict[str, Any]]:
    """Run ``live()`` and record it, or answer from the recording."""
    st = _state(engine)
    want = {"person": person, "tool": tool, "args_digest": _digest(args)}
    if st["mode"] == "live":
        ok, out, meta = live()
        st["calls"].append(dict(want, ok=ok, out=out, meta=meta))
        _save(st)
        return ok, out, meta
    if not st["replay"]:
        return False, "replay: no recorded answer left for this call", {}
    rec = st["replay"].pop(0)
    got = {k: rec[k] for k in want}
    if got != want:
        return False, f"replay mismatch: expected {got}, got {want}", {}
    return rec["ok"], rec["out"], rec.get("meta", {})


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
                           "version": "0.2.0"}})
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
    """The only role that talks to Common Ground's room."""

    def on_start(self):
        engine = self.api._engine
        st = _state(engine)
        self.seats: dict[str, str] = self.config["seats"]  # Town agent -> person
        self.calls_seen: set[str] = set()
        if st["mode"] == "live":
            with open(os.environ["CG_ROOM_AGENTS"]) as f:
                agents = json.load(f)
            self.tokens = {pid: tok for tok, pid in agents["tokens"].items()}
            self.client = RoomClient(os.environ["CG_ROOM_URL"])
            st["tools"] = self.client.initialize()
            st["server"] = self.client.server_info
        self.api.observe("cg_room_interface", "common-ground", {
            "mode": st["mode"], "server": st["server"].get("name", ""),
            "server_version": st["server"].get("version", ""),
            "tools": st["tools"]})
        self.api.register(["meetup.plan"],
                          {"service": "Common Ground room",
                           "interface": "mcp.streamable-http",
                           "tools": [t for t in st["tools"]
                                     if t in ROOM_TOOLS]})

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

        def live():
            try:
                ok, out = self.client.call(
                    tool, dict(args, agent_token=self.tokens[person]))
            except Exception as exc:  # the room is down or unreachable
                ok, out = False, f"room unreachable: {type(exc).__name__}"
            return ok, out, {}

        ok, out, _ = _live_or_replay(self.api._engine, person, tool, args,
                                     live)
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


# -- the real agent behind each person ------------------------------------

PROMPT = """You are {name}'s personal AI agent, running on Claude Code. {name}, \
Javier, Sergio and Jack are an MIT team, and each has their own agent.

{name}'s agent is planning this meetup in a shared room with the other \
agents (you do not talk to the room yourself; your answer is posted for you):

Brief: {brief}

Room rules: {rules}

Candidate venues (public): {venues}

You have one MCP server, `me`, private to {name}: profile, calendar, free \
blocks, travel estimates. Never include {name}'s home, calendar titles, notes \
or reasons in your answer: it is shared with the room.

Work on your own, with no questions to {name}:
1. Availability: read {name}'s profile (`get_my_profile`) and free blocks \
(`find_free_blocks` for the brief's dates, min_minutes = the duration; it \
knows only the calendar). Apply the notes {name} gave you; they override the \
calendar. Pick at most 5 windows that suit {name}, each exactly in the form \
`find_free_blocks` gives (with its UTC offset), each at least the duration \
and on the 30-minute grid.
2. Taste and venues: turn {name}'s preferences into 3-4 short tags (each at \
most 30 characters). Get travel for every candidate with `estimate_travel` \
and use its travel_minutes and travel_score unchanged. Judge a fit_score \
(1-5) for every candidate against the tags, using its vibe, description and \
attributes.

Answer with ONLY this JSON, no other text:
{{"windows": [{{"start": "...", "end": "..."}}], "tags": ["..."], \
"scores": [{{"venue_id": "...", "travel_minutes": 0, "travel_score": 0, \
"fit_score": 1}}]}}"""


def _parse_decision(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no JSON object in the agent's answer")
    data = json.loads(match.group(0))
    if not {"windows", "tags", "scores"} <= set(data):
        raise ValueError("the answer lacks windows, tags or scores")
    return {"windows": data["windows"], "tags": data["tags"],
            "scores": data["scores"]}


def _run_claude(person: str, name: str, prompt: str
                ) -> tuple[bool, Any, dict[str, Any]]:
    """One Claude Code run with only this person's private MCP server."""
    work = tempfile.mkdtemp(prefix=f"cg-agent-{person}-")
    cfg = os.path.join(work, "mcp.json")
    with open(cfg, "w") as f:
        json.dump({"mcpServers": {"me": {
            "type": "stdio", "command": os.environ["CG_ME_PYTHON"],
            "args": [os.environ["CG_ME_SERVER"], "--person", person,
                     "--run", os.environ["CG_PEOPLE_RUN"]]}}}, f)
    cmd = ["claude", "-p", prompt, "--model", AGENT_MODEL,
           "--mcp-config", cfg, "--strict-mcp-config", "--tools", "",
           "--allowedTools", "mcp__me", "--setting-sources", "",
           "--no-session-persistence", "--output-format", "json"]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    started = datetime.now()
    try:
        proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True,
                              text=True, timeout=AGENT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"agent timed out after {AGENT_TIMEOUT} s", {}
    seconds = round((datetime.now() - started).total_seconds(), 1)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, f"agent exited {proc.returncode} without JSON", {
            "seconds": seconds}
    meta = {"harness": "Claude Code", "model": AGENT_MODEL,
            "seconds": seconds, "turns": out.get("num_turns"),
            "cost_usd": round(out.get("total_cost_usd") or 0, 4)}
    if out.get("is_error"):
        return False, str(out.get("result"))[:300], meta
    try:
        return True, _parse_decision(out.get("result", "")), meta
    except (ValueError, json.JSONDecodeError) as exc:
        return False, f"unusable answer: {exc}", meta


@role("cg_person")
class Person(SimAgent):
    """One person's agent in Town; a real Claude Code agent decides."""

    def on_start(self):
        c = self.config
        self.person = c["person"]
        self.seq = 0
        self.pending: str | None = None
        self.polls = 0
        self.plan_id: str | None = None
        self.room: str | None = None
        self.candidates: dict[str, dict[str, Any]] = {}
        self.decision: dict[str, Any] = {}
        self.prompt = ""
        self.repairs = 0
        self.api.register(["meetup.member"], {"person": self.person})
        if c.get("organizer"):
            self.api.later(0.5, self.organize)

    # -- calls through the adapter --

    def call(self, tool: str, args: dict[str, Any]) -> None:
        self.seq += 1
        self.pending = f"{self.name}-{self.seq}"
        self.api.send(self.room, f"cg_{tool}",
                      {"call_id": self.pending, "args": args})
        call_id = self.pending
        self.api.later(self.config.get("reply_timeout", 5.0),
                       lambda: self._check_reply(call_id, tool))

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
            error = str(body.get("error"))
            if (body["tool"] in ("post_availability", "post_scores")
                    and self.repairs < self.config.get("max_repairs", 1)):
                self.repair(body["tool"], error)
                return
            self.api.observe("cg_call_failed", self.person,
                             {"tool": body["tool"], "error": error[:200]})
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
        venues = [{k: v[k] for k in ("venue_id", "name", "lat", "lon",
                                     "opening_hours", "vibe", "about",
                                     "attributes")}
                  for v in result["candidate_venues"]]
        self.prompt = PROMPT.format(name=self.person.capitalize(),
                                    brief=json.dumps(result["brief"]),
                                    rules=json.dumps(result["rules"]),
                                    venues=json.dumps(venues))
        self.decide("agent_decide", self.prompt,
                    {"brief": result["brief"],
                     "venues": sorted(self.candidates)})
        if self.decision:
            self.call("post_availability", {"plan_id": self.plan_id,
                                            "windows": self.decision["windows"]})

    def decide(self, step: str, prompt: str, key: dict[str, Any]) -> None:
        """Ask this person's Claude Code agent (or its recorded answer)."""
        ok, out, meta = _live_or_replay(
            self.api._engine, self.person, step, key,
            lambda: _run_claude(self.person, self.person.capitalize(),
                                prompt))
        mode = _state(self.api._engine)["mode"]
        self.decision = {}
        if not ok:
            self.api.observe("cg_agent_failed", self.person,
                             dict(meta, mode=mode, step=step,
                                  error=str(out)[:300]))
            return
        self.decision = out
        self.api.observe("cg_agent_decided", self.person, dict(
            meta, mode=mode, step=step, windows=len(out["windows"]),
            tags=out["tags"], scored=len(out["scores"])))

    def repair(self, tool: str, error: str) -> None:
        """The room refused a post: show the agent the error, once, as the
        project's agents see room errors, and post its corrected answer."""
        self.repairs += 1
        self.api.observe("cg_agent_repair", self.person,
                         {"tool": tool, "room_error": error[:200]})
        prompt = (self.prompt + "\n\nYour previous answer was:\n"
                  + json.dumps(self.decision) + "\n\nThe room refused your "
                  + tool + " with: " + error + "\nAnswer again with the"
                  " corrected JSON only.")
        self.decide("agent_repair", prompt,
                    {"tool": tool, "error": error,
                     "previous": _digest(self.decision)})
        if not self.decision:
            return
        if tool == "post_availability":
            self.call("post_availability", {"plan_id": self.plan_id,
                                            "windows": self.decision["windows"]})
        else:
            self.after_post_availability({})

    def after_post_availability(self, result):
        self.call("post_scores", {"plan_id": self.plan_id,
                                  "tags": self.decision["tags"],
                                  "scores": self.decision["scores"]})

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


def _local(s: str, tz: str) -> datetime:
    """A posted time in the plan's time zone, without offset."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    return dt


def _inside(start: str, end: str, windows: list[dict[str, str]],
            tz: str) -> bool:
    s, e = _local(start, tz), _local(end, tz)
    return any(_local(w["start"], tz) <= s and e <= _local(w["end"], tz)
               for w in windows)


@validator("common_ground")
def common_ground(spec, trace: Trace) -> list[StageResult]:
    persons = [a for a in spec.agents if a.role == "cg_person"]
    people = sorted(a.config["person"] for a in persons)
    tz = next((a.config["brief"].get("timezone") for a in persons
               if a.config.get("brief")), None) or "America/New_York"
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

    decided = trace.find("cg_agent_decided")
    failed_agents = trace.find("cg_agent_failed")
    by_agent = sorted({e.subject for e in decided
                       if e.detail.get("step") == "agent_decide"})
    if decided and all(e.detail.get("mode") == "live" for e in decided):
        stages.append(_check(
            "live_agents", by_agent == people and not failed_agents,
            [e.event_id for e in decided + failed_agents],
            "every person's windows and scores must come from its own"
            " Claude Code agent",
            "each person's windows, tags and scores came from a Claude Code"
            " agent using only that person's private MCP server"))
    else:
        stages.append(StageResult(
            name="live_agents", status="not_tested",
            note="agent answers replayed from a recorded live run"))

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
    rejected = [e for e in trace.find("cg_room_call", ok=False)
                if not e.detail.get("repeat")]
    repairs = trace.find("cg_agent_repair")
    stages.append(_check(
        "posts_accepted",
        sorted(avail) == people and sorted(scores) == people,
        [e.event_id for e in list(avail.values()) + list(scores.values())
         + rejected + repairs],
        f"the room must accept windows and scores from all of {people}",
        "accepted from every agent"
        + (f"; {len(repairs)} refused post(s) fixed by the agent after"
           " seeing the room's error" if repairs else "")))

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
                                 avail[p].detail["windows"], tz)
                         for p in people)
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
