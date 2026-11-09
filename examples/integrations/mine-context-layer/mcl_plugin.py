"""Mine Context Layer in the Lab.

The subject is the Mine Context Layer: an MCP server that holds the shared
context of one underground-mine shift (plan, fleet, rules, events with
provenance, proposals, and a human decision log). It lives in its own
repository and runs as separate processes. This file holds only the Town side.

  mcl_layer        adapter, and the only role that talks MCP. It opens one MCP
                   session per Town participant, with that participant's layer
                   role (source or function), and turns Town messages into
                   real tool calls. It also records the human decision through
                   the project's own non-MCP command, and reads a final audit.
  mcl_radio        script source: publishes the failure heard on the radio.
  mcl_cmms         script source: adds the CMMS repair history, if the CMMS is up.
  mcl_maintenance  script function: estimates the repair; also tries to publish
                   an event, which the layer must refuse.
  mcl_planning     script function: proposes which loader to move.
  mcl_supervisor   the human: approves the action through the adapter.
  mcl_intruder     a Town agent that is not a layer participant.
  mcl_agent        speaks for one of the project's real agents (headless
                   Claude Code connected only to the layer) in the agents case.

Coordination ("your turn") travels as Town messages between roles; shared
state lives only in the layer. Scripts are integration fixtures, not
independently developed agents. Validators judge only the recorded trace,
and only records written by the adapter count as evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from statistics import median
from typing import Any

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import Trace, _check, _missing, validator

# Folder of this example, so the bridge module can be imported
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
# Environment variable that points to the project's checkout
PROJECT_ENV = "MCL_PROJECT"
# The project commit this check was written against; runs must use it unmodified
PROJECT_COMMIT = "4b3c9e017f1e85db55e251a21efd65f090847af5"
# The eight tools the layer's MCP server exposes (mcl/server.py)
EXPECTED_TOOLS = {"publish_event", "get_shift_snapshot", "get_open_events",
                  "get_equipment_state", "get_plan_slice", "submit_proposal",
                  "get_proposals", "get_decisions"}
# Answer key of incident INC-01 (eval/answer_key.json in the project):
# move LH-11 from TP-1250-02 to the failed high-grade stope TP-1310-04
KEY = {"equipment_id": "LH-11", "from_place": "TP-1250-02", "to_place": "TP-1310-04"}
# Productive time ends at 17:00 (shift plan)
SHIFT_END = "17:00"


# -- small helpers ------------------------------------------------------------


def _minutes(hhmm: str) -> int:
    # Turn "09:18" into minutes since midnight
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _clock(minutes: float) -> str:
    # Turn minutes since midnight back into "HH:MM"
    m = int(round(minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


def _oz(tonnes: float, grade: float) -> float:
    # Ounces of gold in some tonnes of ore at a grade in g/t (plan formula)
    return round(tonnes * grade / 31.1035, 1)


def _adapter_hash() -> str:
    # Fingerprint of the Town-side code, so a bundle says which adapter ran
    digest = hashlib.sha256()
    for name in ("mcl_plugin.py", "mcp_bridge.py"):
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


async def _invoke(session, tool: str, **arguments: Any) -> Any:
    """Call one MCP tool and return its decoded JSON result."""
    result = await session.call_tool(tool, arguments)
    texts = [c.text for c in result.content if getattr(c, "text", None)]
    # A tool error from the server becomes an exception for the bridge
    if result.isError:
        raise RuntimeError(" ".join(texts)[:200] or "tool error")
    # FastMCP sends structured results; lists come wrapped as {"result": [...]}
    structured = result.structuredContent
    if structured is not None:
        if set(structured) == {"result"}:
            return structured["result"]
        return structured
    if not texts:
        return None
    return json.loads(texts[0]) if len(texts) == 1 else [json.loads(t) for t in texts]


# -- the adapter --------------------------------------------------------------


@role("mcl_layer")
class Layer(SimAgent):
    """The only role that talks to the project. Everything it learns, it records."""

    def on_start(self):
        # Sessions, one per Town participant, plus a read-only auditor
        self.bridges: dict[str, Any] = {}
        self.auditor = None
        self.seen_events: set[str] = set()
        self.seen_proposals: set[str] = set()
        self.api.register(["mcl.layer"])
        # Who may use the layer, and as what: {town name: {agent, role, source}}
        self.participants = self.config.get("participants", {})
        self.supervisor = self.config.get("supervisor")
        try:
            self.project = os.path.abspath(os.environ[PROJECT_ENV])
            self.python = os.path.join(self.project, ".venv", "bin", "python")
            # A fresh shift database for this run only
            self.data_dir = tempfile.TemporaryDirectory(prefix="mcl-town-")
            self.db = os.path.join(self.data_dir.name, "layer.db")
            prepared = self._cli("prepare", "--db", self.db,
                                 "--case", self.config.get("case", "normal"))
            # The auditor reads the final state at the end; it never writes
            self.auditor = self._bridge("town-auditor", "function", "")
            tools = self.auditor.start()
            # Script participants each get their own session with their own role
            for name, p in self.participants.items():
                if p.get("mode", "script") == "script":
                    self.bridges[name] = self._bridge(p["agent"], p["role"], p.get("source", ""))
                    self.bridges[name].start()
        except Exception as exc:  # recorded, not hidden
            self.auditor = None
            self.api.observe("layer_unreachable", self.name,
                             {"error": f"{type(exc).__name__}: {str(exc)[:160]}"})
            return
        commit, dirty = _commit(self.project)
        self.api.observe("layer_connected", self.name, {
            "tools": sorted(tools), "project_commit": commit, "project_dirty": dirty,
            "adapter_sha256": _adapter_hash(), "case": prepared.get("case"),
            "sources": prepared.get("sources"),
            "participants": {n: {k: p.get(k) for k in ("agent", "role", "source", "mode")}
                             for n, p in self.participants.items()}})

    # -- plumbing

    def _bridge(self, agent: str, layer_role: str, source: str):
        # One MCP stdio session to the project's server, with this identity
        from mcp_bridge import McpBridge
        return McpBridge(
            self.python, [os.path.join(self.project, "mcl", "server.py")], _invoke,
            env={"MCL_DB": self.db, "MCL_AGENT": agent, "MCL_ROLE": layer_role,
                 "MCL_SOURCE": source,
                 # Keep the server's per-request log lines out of the run output
                 "FASTMCP_LOG_LEVEL": "WARNING"},
            cwd=self.project)

    def _cli(self, *args: str, timeout: float = 60.0) -> dict[str, Any]:
        # Run one of the project's non-MCP commands and read its JSON answer
        done = subprocess.run([self.python, "-m", "mcl.session", *args],
                              cwd=self.project, capture_output=True, text=True,
                              timeout=timeout, check=False)
        lines = done.stdout.strip().splitlines()
        if not lines:
            raise RuntimeError(f"mcl.session {args[0]} gave no answer: {done.stderr[-200:]}")
        return json.loads(lines[-1])

    def _refuse(self, msg, kind: str, reason: str) -> None:
        # Record and answer a request the layer will not serve
        body = msg["body"]
        self.api.observe("layer_refused", msg["sender"], {
            "request_id": body.get("request_id"), "asked": msg["kind"],
            "tool": body.get("tool"), "reason": reason})
        self.api.reply(msg, kind, {"request_id": body.get("request_id"),
                                   "ok": False, "error": reason})

    def on_message(self, msg):
        # If the project never connected, refuse every request instead of crashing
        if self.auditor is None:
            reply_kind = {"tool_request": "tool_result", "decision_request": "decision_result",
                          "agent_request": "agent_done", "audit_request": "audit_done"}.get(msg["kind"])
            if reply_kind:
                self._refuse(msg, reply_kind, "layer_unavailable")
            return
        super().on_message(msg)

    # -- requests from script participants: real MCP tool calls

    def handle_tool_request(self, msg):
        body, sender = msg["body"], msg["sender"]
        bridge = self.bridges.get(sender)
        # Only scenario participants have a layer identity; anyone else is refused
        if bridge is None:
            self._refuse(msg, "tool_result", "not_a_layer_participant")
            return
        from mcp_bridge import BridgeError
        p = self.participants[sender]
        try:
            result = bridge.call(body["tool"], **body.get("args", {}))
        except BridgeError as exc:
            self.api.observe("layer_tool_failed", body["tool"], {
                "request_id": body["request_id"], "participant": sender,
                "error": str(exc)[:200]})
            self.api.reply(msg, "tool_result", {"request_id": body["request_id"],
                                                "ok": False, "error": str(exc)[:200]})
            return
        self.api.observe("layer_tool_result", body["tool"], {
            "request_id": body["request_id"], "participant": sender,
            "agent": p["agent"], "layer_role": p["role"],
            "args": body.get("args", {}), "result": result})
        self.api.reply(msg, "tool_result", {"request_id": body["request_id"],
                                            "tool": body["tool"], "ok": True,
                                            "result": result})

    # -- the human decision: outside MCP, through the project's own command

    def handle_decision_request(self, msg):
        body = msg["body"]
        if msg["sender"] != self.supervisor:
            self._refuse(msg, "decision_result", "only_the_supervisor_decides")
            return
        out = self._cli("decide", "--db", self.db, "--proposal", body["proposal_id"],
                        "--decision", body["decision"], "--by", msg["sender"],
                        "--note", body.get("note", ""))
        self.api.observe("layer_decision_recorded", body["proposal_id"],
                         {"by": msg["sender"], "result": out})
        self.api.reply(msg, "decision_result", {"ok": "error" not in out, **out})

    # -- the real agents: headless Claude Code, connected only to the layer

    def handle_agent_request(self, msg):
        body, sender = msg["body"], msg["sender"]
        p = self.participants.get(sender)
        if p is None or p.get("mode") != "agent":
            self._refuse(msg, "agent_done", "not_a_real_agent_participant")
            return
        logs = os.path.join(self.data_dir.name, "agents")
        try:
            run = self._cli("agent", "--db", self.db, "--name", p["agent"],
                            "--log-dir", logs, timeout=900)
        except Exception as exc:
            run = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        # What the agent wrote, read back through MCP by the auditor
        new_events, new_proposals = self._new_records()
        self.api.observe("layer_agent_run", p["agent"], {
            "participant": sender, "layer_role": p["role"], "run": run,
            "new_events": new_events, "new_proposals": new_proposals})
        actions = [x["id"] for x in new_proposals if x["kind"] == "action"]
        self.api.reply(msg, "agent_done", {
            "ok": "error" not in run and run.get("ok", False),
            "new_event_ids": [e["id"] for e in new_events],
            "new_proposal_ids": [x["id"] for x in new_proposals],
            "action_proposal_id": actions[-1] if actions else None})

    def _new_records(self):
        # Events and proposals that appeared since the last look
        events = self.auditor.call("get_open_events")
        proposals = self.auditor.call("get_proposals")
        new_e = [e for e in events if e["id"] not in self.seen_events]
        new_p = [x for x in proposals if x["id"] not in self.seen_proposals]
        self.seen_events |= {e["id"] for e in events}
        self.seen_proposals |= {x["id"] for x in proposals}
        return new_e, new_p

    # -- the final audit: the layer's own view of what happened

    def handle_audit_request(self, msg):
        audit = {"events": self.auditor.call("get_open_events"),
                 "proposals": self.auditor.call("get_proposals"),
                 "decisions": self.auditor.call("get_decisions")}
        # The layer's call log has no MCP tool; read it from the layer's own file
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        audit["tool_calls"] = [
            {"agent": a, "tool": t, "ok": bool(ok)}
            for a, t, ok in con.execute("SELECT agent, tool, ok FROM tool_calls ORDER BY id")]
        con.close()
        self.api.observe("layer_audit", self.name, audit)
        self.api.reply(msg, "audit_done", {"ok": True})


# -- script participants ------------------------------------------------------


class LayerClient(SimAgent):
    """A role that asks the adapter for tool calls and continues on each answer."""

    def _setup(self, capability: str):
        self.api.register([capability])
        self.waiting: dict[str, Any] = {}
        self.count = 0

    def _ask(self, tool: str, then, **args: Any) -> None:
        # Send one tool request; `then` runs with the result when it comes back
        self.count += 1
        request_id = f"{self.name}-{self.count}"
        self.waiting[request_id] = then
        self.api.send(self.config["layer"], "tool_request",
                      {"request_id": request_id, "tool": tool, "args": args})

    def handle_tool_result(self, msg):
        body = msg["body"]
        then = self.waiting.pop(body["request_id"], None)
        if then is None:
            return
        if not body.get("ok"):
            self.api.observe("participant_halted", body.get("tool", "?"),
                             {"error": body.get("error")})
            return
        then(body["result"])

    def _notify(self, kind: str, body: dict[str, Any]) -> None:
        # Tell the next roles it is their turn (coordination, not state)
        for to in self.config.get("notify", []):
            self.api.send(to, kind, body)


@role("mcl_radio")
class Radio(LayerClient):
    def on_start(self):
        self._setup("mcl.source.radio")
        self.api.later(1.0, self._publish)

    def _publish(self):
        # The radio line as heard, with mentions as spoken ("scoop 7", "1310-04")
        line = self.config["line"]
        self._ask("publish_event", self._published,
                  type="equipment_failure", summary=line["summary"],
                  occurred_at=line["at"], mentions=line["mentions"],
                  location=line["location"], confidence=0.9,
                  raw_ref=line["ref"], raw_text=line["text"])

    def _published(self, result):
        self._notify("failure_published", {"event_id": result.get("event_id")})


@role("mcl_cmms")
class Cmms(LayerClient):
    def on_start(self):
        self._setup("mcl.source.cmms")

    def handle_failure_published(self, msg):
        # First check whether the CMMS itself is reachable
        self._ask("get_shift_snapshot", self._snapshot)

    def _snapshot(self, snap):
        status = {s["name"]: s["status"] for s in snap["sources"]}.get("cmms")
        if status != "online":
            # The connector cannot read a system that is down: publish nothing
            self._notify("cmms_done", {"cmms": status, "event_id": None})
            return
        orders = self.config["work_orders"]
        self._ask("publish_event", self._published,
                  type="maintenance_record",
                  summary=f"{self.config['equipment']}: {len(orders)} corrective hydraulic "
                          f"work orders in the last 60 days (chronic)",
                  occurred_at="09:31", mentions=[self.config["equipment"]], confidence=1.0,
                  raw_ref=",".join(f"cmms://{o['id']}" for o in orders),
                  payload={"work_orders": orders, "chronic": True})

    def _published(self, result):
        self._notify("cmms_done", {"cmms": "online", "event_id": result.get("event_id")})


@role("mcl_maintenance")
class Maintenance(LayerClient):
    def on_start(self):
        self._setup("mcl.function.maintenance")

    def handle_cmms_done(self, msg):
        self._ask("get_open_events", self._failures, type="equipment_failure")

    def _failures(self, failures):
        self.failure = failures[0]
        self.equipment = [s for s in self.failure["subject_ids"] if s.startswith("LH-")][0]
        self._ask("get_open_events", self._records, type="maintenance_record",
                  subject_id=self.equipment)

    def _records(self, records):
        self.records = records
        if self.config.get("probe_permission"):
            # Boundary probe: a function agent tries to publish; the layer must refuse
            self._ask("publish_event", self._probed, type="equipment_status",
                      summary=f"{self.equipment} back in service", occurred_at="09:31",
                      mentions=[self.equipment])
        else:
            self._assess()

    def _probed(self, result):
        self._assess()

    def _assess(self):
        hours = [float(o["hours"]) for r in self.records
                 for o in (r.get("payload") or {}).get("work_orders", [])]
        if hours:
            low, likely, high, confidence, missing = min(hours), median(hours), max(hours), 0.8, []
            how = f"range of {len(hours)} past corrective hydraulic repairs: {sorted(hours)} h"
        else:
            # No history: widen the range, lower confidence, say what is missing
            low, likely, high, confidence = 2.0, 4.0, 8.0, 0.5
            missing = [f"CMMS is down: no repair history for {self.equipment}. Ask the "
                       f"maintenance supervisor or check the paper work orders at the shop."]
            how = "no CMMS history; generic hydraulic repair range"
        back = _clock(_minutes(self.failure["occurred_at"]) + likely * 60)
        self._ask("submit_proposal", self._submitted, kind="assessment",
                  title=f"{self.equipment} repair estimate",
                  recommendation={"equipment_id": self.equipment, "repair_hours_low": low,
                                  "repair_hours_likely": likely, "repair_hours_high": high,
                                  "expected_back_at": back, "likely_cause": "hydraulic"},
                  options=[], calculation=how,
                  sources=[self.failure["id"]] + [r["id"] for r in self.records],
                  confidence=confidence, missing_info=missing)

    def _submitted(self, result):
        self._notify("assessment_ready", {"proposal_id": result.get("proposal_id")})


@role("mcl_planning")
class Planning(LayerClient):
    """Applies the site's dispatch rules mechanically. This is a fixture: in the
    agents case the project's own planning agent makes this decision."""

    def on_start(self):
        self._setup("mcl.function.planning")

    def handle_assessment_ready(self, msg):
        self._ask("get_shift_snapshot", self._snapshot)

    def _snapshot(self, snap):
        self.rules = [r["id"] for r in snap["operating_rules"]]
        self._ask("get_open_events", self._failures, type="equipment_failure")

    def _failures(self, failures):
        self.failure = failures[0]
        self._ask("get_proposals", self._assessments, kind="assessment")

    def _assessments(self, assessments):
        self.assessment = assessments[-1]
        self._ask("get_plan_slice", self._plan)

    def _plan(self, plan):
        stopes = {s["id"]: s for s in plan["stopes"]}
        failed = stopes[self.failure["location"]]
        # LOADER-BACKUP-02: only loading-only stopes can give up their loader;
        # LOADER-BACKUP-01: take it from the lowest-grade one
        donors = [s for s in plan["stopes"]
                  if s["phase"] == "carguio" and s["id"] != failed["id"] and s.get("loader")]
        donor = min(donors, key=lambda s: s["grade_gpt"])
        # MOVE-TIME-01: 0.5 h on the same level, 1 h through the ramp
        move_h = 0.5 if donor["level"] == failed["level"] else 1.0
        left_h = (_minutes(SHIFT_END) - _minutes(self.failure["occurred_at"])) / 60
        repair_h = self.assessment["recommendation"]["repair_hours_likely"]
        rate, donor_rate = failed["target_tonnes"] / 10, donor["target_tonnes"] / 10
        nothing_t = round(rate * left_h)
        wait_t = round(rate * min(repair_h, left_h))
        moved_loss_t = round(rate * move_h)
        donor_loss_t = round(donor_rate * left_h)
        protected_t = nothing_t - moved_loss_t
        rec = {"action_type": "reassign_loader", "equipment_id": donor["loader"],
               "from_place": donor["id"], "to_place": failed["id"],
               "start_by": self.failure["occurred_at"],
               "tonnes_protected": protected_t, "oz_protected": _oz(protected_t, failed["grade_gpt"]),
               "tonnes_lost_elsewhere": donor_loss_t,
               "oz_lost_elsewhere": _oz(donor_loss_t, donor["grade_gpt"]),
               "net_oz_protected": round(_oz(protected_t, failed["grade_gpt"])
                                         - _oz(donor_loss_t, donor["grade_gpt"]), 1)}
        options = [
            {"option_id": "do_nothing", "description": "stope idle until shift end",
             "tonnes_at_risk": nothing_t, "oz_at_risk": _oz(nothing_t, failed["grade_gpt"])},
            {"option_id": "wait_for_repair", "description": f"resume after {repair_h} h",
             "tonnes_at_risk": wait_t, "oz_at_risk": _oz(wait_t, failed["grade_gpt"])},
            {"option_id": "reassign_loader",
             "description": f"move {donor['loader']} from {donor['id']}",
             "tonnes_at_risk": moved_loss_t + donor_loss_t,
             "oz_at_risk": round(_oz(moved_loss_t, failed["grade_gpt"])
                                 + _oz(donor_loss_t, donor["grade_gpt"]), 1)}]
        self._ask("submit_proposal", self._submitted, kind="action",
                  title=f"Move {donor['loader']} to {failed['id']}",
                  recommendation=rec, options=options,
                  calculation=(f"rules {', '.join(self.rules)}; rate = plan tonnes / 10 h; "
                               f"{left_h:.1f} h left; move {move_h} h; oz = t x g/t / 31.1035"),
                  sources=[self.failure["id"], self.assessment["id"]],
                  confidence=self.assessment["confidence"],
                  missing_info=self.assessment.get("missing_info") or [])

    def _submitted(self, result):
        self._notify("proposal_ready", {"proposal_id": result.get("proposal_id")})


@role("mcl_supervisor")
class Supervisor(SimAgent):
    """The shift supervisor. Approves outside MCP, through the adapter."""

    def on_start(self):
        self.api.register(["mcl.human"])

    def handle_proposal_ready(self, msg):
        self._decide(msg["body"].get("proposal_id"))

    def handle_go(self, msg):
        # In the agents case the planning agent's turn ends with its proposal id
        self._decide(msg["body"].get("action_proposal_id"))

    def _decide(self, proposal_id):
        if not proposal_id:
            self.api.observe("supervisor_nothing_to_decide", self.name, {})
            self.api.send(self.config["layer"], "audit_request", {})
            return
        decision = "approved" if self.config.get("approve", True) else "rejected"
        self.api.send(self.config["layer"], "decision_request",
                      {"proposal_id": proposal_id, "decision": decision,
                       "note": "approved in the Nanda Town compatibility run"})

    def handle_decision_result(self, msg):
        self.api.send(self.config["layer"], "audit_request", {})

    def handle_audit_done(self, msg):
        pass


@role("mcl_intruder")
class Intruder(SimAgent):
    """A Town agent with no layer identity tries to read the shift plan."""

    def on_start(self):
        self.api.register(["mcl.intruder"])
        self.api.later(0.5, lambda: self.api.send(
            self.config["layer"], "tool_request",
            {"request_id": f"{self.name}-1", "tool": "get_plan_slice", "args": {}}))

    def handle_tool_result(self, msg):
        pass


@role("mcl_agent")
class RealAgent(SimAgent):
    """Speaks for one of the project's real agents. When it is this agent's
    turn, it asks the adapter to run it, then passes the turn on."""

    def on_start(self):
        self.api.register([f"mcl.agent.{self.config['agent']}"])
        if self.config.get("start"):
            self.api.later(1.0, self._run)

    def handle_go(self, msg):
        self._run()

    def _run(self):
        self.api.send(self.config["layer"], "agent_request", {"agent": self.config["agent"]})

    def handle_agent_done(self, msg):
        for to in self.config.get("notify", []):
            self.api.send(to, "go", msg["body"])


# -- validators ---------------------------------------------------------------
#
# Each stage rebuilds what happened from records the adapter wrote: the real
# MCP results, the decision write and the final audit. A script role's own
# observations never count.

_DAMAGED = (KeyError, IndexError, TypeError, AttributeError, ValueError, StopIteration)


def _adapter(spec) -> str:
    [a] = [a for a in spec.agents if a.role == "mcl_layer"]
    return a.name


def _by(trace: Trace, adapter: str, kind: str, **conds) -> list:
    # Records of one kind, written by the adapter only
    return [e for e in trace.find(kind, **conds) if e.observer == adapter]


def _ids(events) -> list[str]:
    return [e.event_id for e in events]


def _calls(trace, adapter, tool, layer_role=None):
    # Real MCP results for one tool, optionally from one layer role
    return [e for e in _by(trace, adapter, "layer_tool_result", subject=tool)
            if layer_role is None or e.detail["layer_role"] == layer_role]


def _audit(trace, adapter):
    [audit] = _by(trace, adapter, "layer_audit")
    return audit


def _guarded(name: str, check) -> StageResult:
    """Evidence too damaged to read is missing evidence, not a crash."""
    try:
        return check()
    except _DAMAGED:
        return _missing(name, "required evidence is missing or malformed")


def _connection(trace, adapter):
    connected = _by(trace, adapter, "layer_connected")
    d = connected[0].detail
    return _check("connection",
                  set(d["tools"]) == EXPECTED_TOOLS
                  and d["project_commit"] == PROJECT_COMMIT
                  and d["project_dirty"] is False,
                  _ids(connected),
                  f"expected the layer's eight MCP tools, from unmodified project "
                  f"commit {PROJECT_COMMIT[:7]}",
                  "live MCP stdio sessions, one per participant role")


def _failure_event(audit):
    # The failure as the layer stores it
    return [e for e in audit.detail["events"] if e["type"] == "equipment_failure"][0]


def _id_resolution(trace, adapter):
    # The radio said "scoop 7" and "1310-04"; the layer must turn them into system ids
    [pub] = [e for e in _calls(trace, adapter, "publish_event", "source")
             if e.detail["args"]["type"] == "equipment_failure"]
    r = pub.detail["result"]
    ok = ("LH-07" in r["resolved_ids"] and r["location_id"] == "TP-1310-04"
          and "scoop 7" in pub.detail["args"]["mentions"])
    return _check("id_resolution", ok, _ids([pub]),
                  "spoken mentions 'scoop 7' / '1310-04' must resolve to LH-07 at TP-1310-04")


def _id_resolution_from_audit(trace, adapter):
    audit = _audit(trace, adapter)
    f = _failure_event(audit)
    ok = "LH-07" in f["subject_ids"] and f["location"] == "TP-1310-04" \
        and f["author_agent"] == "radio-agent"
    return _check("id_resolution", ok, _ids([audit]),
                  "the radio agent's failure event must resolve to LH-07 at TP-1310-04")


def _provenance(trace, adapter):
    # CMMS facts carry their work-order ids; the assessment cites what it used
    [rec] = [e for e in _calls(trace, adapter, "publish_event", "source")
             if e.detail["args"]["type"] == "maintenance_record"]
    [assess] = [e for e in _calls(trace, adapter, "submit_proposal", "function")
                if e.detail["args"]["kind"] == "assessment"]
    sources = assess.detail["args"]["sources"]
    ok = (rec.detail["args"]["raw_ref"].startswith("cmms://")
          and rec.detail["result"]["event_id"] in sources
          and "proposal_id" in assess.detail["result"])
    return _check("provenance", ok, _ids([rec, assess]),
                  "the CMMS record must link to its work orders and be cited by the assessment")


def _permission_boundary(trace, adapter, spec):
    # (a) a function agent cannot publish; (b) a non-participant gets nothing;
    # (c) the layer's own log shows the denial and no event by that agent
    denied = [e for e in _calls(trace, adapter, "publish_event", "function")
              if e.detail["result"].get("error") == "permission_denied"]
    intruders = [a.name for a in spec.agents if a.role == "mcl_intruder"]
    refused = [e for e in _by(trace, adapter, "layer_refused")
               if e.subject in intruders and e.detail["reason"] == "not_a_layer_participant"]
    audit = _audit(trace, adapter)
    agent = denied[0].detail["agent"]
    no_event = all(e["author_agent"] != agent for e in audit.detail["events"])
    logged = any(c["agent"] == agent and c["tool"] == "publish_event" and not c["ok"]
                 for c in audit.detail["tool_calls"])
    ok = len(denied) == 1 and len(refused) == len(intruders) == 1 and no_event and logged
    return _check("permission_boundary", ok, _ids(denied + refused + [audit]),
                  "a function agent's publish must be denied and logged, and a Town agent "
                  "without a layer identity must be refused")


def _action(trace, adapter, degraded=False):
    # The action proposal must match the answer key and cite real records
    [act] = [e for e in _calls(trace, adapter, "submit_proposal", "function")
             if e.detail["args"]["kind"] == "action"]
    args = act.detail["args"]
    rec = args["recommendation"]
    matches = all(rec[k] == v for k, v in KEY.items())
    audit = _audit(trace, adapter)
    failure = _failure_event(audit)
    cited = failure["id"] in args["sources"] and any(s.startswith("PR-") for s in args["sources"])
    flagged = bool(args["missing_info"]) and args["confidence"] < 0.7
    ok = matches and cited and "proposal_id" in act.detail["result"] \
        and (flagged if degraded else not args["missing_info"])
    return _check("action_matches_key", ok, _ids([act, audit]),
                  f"expected move {KEY['equipment_id']} from {KEY['from_place']} to "
                  f"{KEY['to_place']}, citing the failure and the assessment"
                  + (", with missing info and confidence below 0.7" if degraded else ""))


def _action_from_audit(trace, adapter):
    audit = _audit(trace, adapter)
    actions = [p for p in audit.detail["proposals"] if p["kind"] == "action"]
    act = actions[-1]
    failure = _failure_event(audit)
    ok = all(act["recommendation"].get(k) == v for k, v in KEY.items()) \
        and failure["id"] in act["sources"] and act["author_agent"] == "planning-agent"
    return _check("action_matches_key", ok, _ids([audit]),
                  f"the planning agent must propose moving {KEY['equipment_id']} from "
                  f"{KEY['from_place']} to {KEY['to_place']}, citing the failure")


def _human_decision(trace, adapter, spec):
    # Approval happens outside MCP, by the supervisor, and the layer records it
    [sup] = [a.name for a in spec.agents if a.role == "mcl_supervisor"]
    recorded = [e for e in _by(trace, adapter, "layer_decision_recorded")
                if e.detail["by"] == sup and e.detail["result"].get("decision") == "approved"]
    audit = _audit(trace, adapter)
    pid = recorded[0].subject
    in_log = any(d["proposal_id"] == pid and d["decision"] == "approved"
                 for d in audit.detail["decisions"])
    status = [p["status"] for p in audit.detail["proposals"] if p["id"] == pid]
    no_tool = not any("approve" in t or "decide" in t for t in EXPECTED_TOOLS)
    ok = len(recorded) == 1 and in_log and status == ["approved"] and no_tool
    return _check("human_decision", ok, _ids(recorded + [audit]),
                  "the supervisor's approval must be in the decision log, with no MCP "
                  "tool able to approve")


def _degraded_source(trace, adapter):
    # CMMS down: no CMMS facts reach the layer, and the gap is said out loud
    connected = _by(trace, adapter, "layer_connected")
    records = [e for e in _calls(trace, adapter, "publish_event", "source")
               if e.detail["args"]["type"] == "maintenance_record"]
    [assess] = [e for e in _calls(trace, adapter, "submit_proposal", "function")
                if e.detail["args"]["kind"] == "assessment"]
    a = assess.detail["args"]
    ok = (connected[0].detail["sources"]["cmms"] == "down" and not records
          and a["missing_info"] and a["confidence"] < 0.7
          and a["recommendation"]["repair_hours_high"] > a["recommendation"]["repair_hours_low"])
    return _check("degraded_source", ok, _ids(connected + [assess]),
                  "with the CMMS down, no CMMS record may appear and the assessment must "
                  "name what is missing with lower confidence")


def _agents_ran(trace, adapter, spec):
    # Every real agent ran, used only the layer's tools, and stayed in its role
    runs = _by(trace, adapter, "layer_agent_run")
    expected = {a.config["agent"] for a in spec.agents if a.role == "mcl_agent"}
    ran = {e.subject for e in runs if e.detail["run"].get("ok")}
    only_layer = all(c.startswith("mcp__mcl__") for e in runs
                     for c in e.detail["run"].get("tool_calls", []))
    audit = _audit(trace, adapter)
    denied = [c for c in audit.detail["tool_calls"] if not c["ok"]]
    ok = ran == expected and only_layer and not denied
    return _check("real_agents", ok, _ids(runs + [audit]),
                  "all four real agents must finish, call only layer tools, and get no "
                  "permission denials")


def _stages(spec, trace: Trace, case: str):
    names = {"workflow": ("connection", "id_resolution", "provenance",
                          "permission_boundary", "action_matches_key", "human_decision"),
             "cmms_down": ("connection", "id_resolution", "degraded_source",
                           "action_matches_key", "human_decision"),
             "agents": ("connection", "real_agents", "id_resolution",
                        "action_matches_key", "human_decision")}[case]
    try:
        adapter = _adapter(spec)
    except _DAMAGED:
        return [_missing(n, "no adapter in the scenario; not judged") for n in names]
    if not _by(trace, adapter, "layer_connected"):
        return [_missing(n, "project not connected; not judged") for n in names]
    checks = {
        "connection": lambda: _connection(trace, adapter),
        "id_resolution": (lambda: _id_resolution_from_audit(trace, adapter)) if case == "agents"
        else (lambda: _id_resolution(trace, adapter)),
        "provenance": lambda: _provenance(trace, adapter),
        "permission_boundary": lambda: _permission_boundary(trace, adapter, spec),
        "degraded_source": lambda: _degraded_source(trace, adapter),
        "real_agents": lambda: _agents_ran(trace, adapter, spec),
        "action_matches_key": (lambda: _action_from_audit(trace, adapter)) if case == "agents"
        else (lambda: _action(trace, adapter, degraded=case == "cmms_down")),
        "human_decision": lambda: _human_decision(trace, adapter, spec),
    }
    return [_guarded(n, checks[n]) for n in names]


@validator("mcl_workflow")
def mcl_workflow(spec, trace: Trace):
    return _stages(spec, trace, "workflow")


@validator("mcl_cmms_down")
def mcl_cmms_down(spec, trace: Trace):
    return _stages(spec, trace, "cmms_down")


@validator("mcl_agents")
def mcl_agents(spec, trace: Trace):
    return _stages(spec, trace, "agents")
