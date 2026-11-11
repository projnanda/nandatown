"""Porch in NANDA Town: a Lab plugin that connects Town agents to a running
Porch home gateway.

Porch is a permissioned gateway between AI agents and a connected home.
Agents reach it over MCP with one key each. Porch checks every request
against that agent's scopes and the home's safety rules, dry-runs it,
executes it through its Device Hub, reads the device back, and writes
the decision to an audit log. Porch runs separately; this file holds
only the Town side:

* ``porch_gateway``: the adapter. A Town agent that publishes a card in
  the town index, takes signed Town messages, and forwards each one to
  Porch over MCP under the sending agent's own Porch key.
* ``porch_home_agent``: a scripted agent that finds the gateway through
  the index and walks a plan of home actions.
* ``porch_home_owner``: the household owner. Approves held requests
  through Porch's owner API and reads Porch's audit log at the end.
* ``porch_hub_auditor``: an independent observer. Reads device state
  straight from the Device Hub, not from Porch's answer.
* ``porch_impersonator``: sends a request that claims another agent's
  name but is signed with its own key.
* the ``porch_home`` validator, which judges the run from the trace.

Connection settings come from the environment, never from the scenario:

    PORCH_CONFIG   path to Porch's porch_config.json (keys stay in memory)
    PORCH_MCP_URL  default http://127.0.0.1:8000/mcp
    PORCH_HUB_URL  default: hub_url from PORCH_CONFIG

Run it:      nandatown run examples/porch/porch_home.yaml
Step table:  python examples/porch/porch_town.py table runs/<run id>
Verify:      python examples/porch/porch_town.py verify runs/<run id>

``nandatown verify`` does not load plugin files, so it cannot replay a
validator that lives in one; the ``verify`` command above loads this
file first and then runs the same check.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any
from urllib.parse import urlsplit

import httpx

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import (
    Trace,
    _check,
    _failed,
    _missing,
    validator,
)

ADAPTER_VERSION = "porch-town-adapter/0.1.0"
DEFAULT_MCP_URL = "http://127.0.0.1:8000/mcp"
MCP_PROTOCOL_VERSION = "2025-06-18"
TIMEOUT_SECONDS = 30.0

# Porch has no request id of its own. The adapter writes the Town message
# id into the reason Porch stores in its audit log, so one Town request
# can be matched to its Porch audit entries.
REF_PATTERN = re.compile(r"\[town:([^\]]+)\]\s*$")

# Porch's audit log and its answer to the agent name decisions differently.
AUDIT_DECISION = {
    "allow": "allowed",
    "approved": "allowed",
    "deny": "denied",
    "hold": "held_for_owner_approval",
    "waiting": "held_for_owner_approval",
}


class PorchProtocolError(Exception):
    """Porch answered, but not with the MCP response the adapter expects."""


class Settings:
    """Where Porch is, read from the environment once per agent."""

    def __init__(self) -> None:
        self.mcp_url = os.environ.get("PORCH_MCP_URL", DEFAULT_MCP_URL)
        parts = urlsplit(self.mcp_url)
        self.owner_url = f"{parts.scheme}://{parts.netloc}"
        self.hub_url = os.environ.get("PORCH_HUB_URL", "")
        self.keys: dict[str, str] = {}
        self.owner_key = ""
        self.problem = ""
        path = os.environ.get("PORCH_CONFIG", "")
        if not path:
            self.problem = ("PORCH_CONFIG is not set; point it at Porch's"
                            " porch_config.json")
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.keys = {agent["id"]: key
                         for key, agent in data["agents"].items()}
            self.owner_key = data["owner_key"]
            if not self.hub_url:
                self.hub_url = data["hub_url"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.problem = ("PORCH_CONFIG could not be read:"
                            f" {type(exc).__name__}")


def _http() -> httpx.Client:
    # Like Town's own clients: no ambient proxy or environment settings.
    return httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False)


def _short(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:120]}"


class PorchSession:
    """One agent's MCP session with Porch (streamable HTTP, JSON-RPC)."""

    def __init__(self, url: str, key: str) -> None:
        self.url = url
        self._key = key
        self._session_id: str | None = None
        self._next_id = 0

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {self._key}",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        with _http() as http:
            response = http.post(self.url, json=body, headers=headers)
        response.raise_for_status()
        return response

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        response = self._post({"jsonrpc": "2.0", "id": request_id,
                               "method": method, "params": params})
        if method == "initialize":
            self._session_id = response.headers.get("mcp-session-id")
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/event-stream"):
            messages = [json.loads(line[5:].strip())
                        for line in response.text.splitlines()
                        if line.startswith("data:")]
        else:
            messages = [response.json()]
        for message in messages:
            if isinstance(message, dict) and message.get("id") == request_id:
                if "error" in message:
                    raise PorchProtocolError(
                        f"{method} returned a JSON-RPC error")
                return message.get("result") or {}
        raise PorchProtocolError(f"{method} returned no JSON-RPC result")

    def open(self) -> None:
        self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "nandatown-porch-adapter",
                           "version": ADAPTER_VERSION},
        })
        self._post({"jsonrpc": "2.0",
                    "method": "notifications/initialized"})

    def call(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
        """Returns (True, structured result) or (False, Porch's refusal)."""
        result = self._request("tools/call",
                               {"name": tool, "arguments": arguments})
        content = result.get("content") or [{}]
        text = content[0].get("text", "") if isinstance(content[0], dict) else ""
        if result.get("isError"):
            return False, text
        structured = result.get("structuredContent")
        return True, structured if structured is not None else json.loads(text)


def _flatten(clusters: dict[str, Any]) -> dict[str, Any]:
    """{"DoorLock": {"attributes": {"LockState": "Locked"}}}
    becomes {"DoorLock.LockState": "Locked"}."""
    return {f"{cluster}.{name}": value
            for cluster, body in (clusters or {}).items()
            for name, value in (body.get("attributes") or {}).items()}


def _first(api, capability: str) -> str | None:
    cards = api.lookup(capability)
    return cards[0]["name"] if cards else None


# -- the adapter -------------------------------------------------------


@role("porch_gateway")
class PorchGateway(SimAgent):
    """Forwards signed Town requests to Porch over MCP, once each."""

    def on_start(self):
        self.settings = Settings()
        self.dedup = bool(self.config.get("dedup", True))
        self.sessions: dict[str, PorchSession] = {}
        self.answered: set[str] = set()
        self.run_ref = self.api._engine.run_id
        self.api.register(["home.describe", "home.act"],
                          {"gateway": "porch",
                           "interface": "mcp-streamable-http",
                           "adapter": ADAPTER_VERSION})
        if self.settings.problem:
            self.api.observe("porch_adapter_misconfigured", self.name,
                             {"problem": self.settings.problem})
            return
        self.api.observe("porch_adapter_configured", self.name, {
            "adapter": ADAPTER_VERSION,
            "mcp_url": self.settings.mcp_url,
            "hub_url": self.settings.hub_url,
            "agents_with_a_porch_key": sorted(self.settings.keys),
            "dedup": self.dedup,
        })

    def _call(self, agent: str, tool: str,
              arguments: dict[str, Any]) -> tuple[str, Any]:
        if self.settings.problem:
            return "unreachable", self.settings.problem
        try:
            session = self.sessions.get(agent)
            if session is None:
                # A Town agent with no Porch key is still forwarded, under
                # a key Porch does not know, so the refusal is Porch's own.
                key = (self.settings.keys.get(agent)
                       or f"town-agent-without-a-porch-key:{agent}")
                session = PorchSession(self.settings.mcp_url, key)
                session.open()
                self.sessions[agent] = session
            ok, payload = session.call(tool, arguments)
        except (httpx.HTTPError, PorchProtocolError, ValueError) as exc:
            self.sessions.pop(agent, None)
            return "unreachable", _short(exc)
        return ("ok" if ok else "tool_error"), payload

    def _already_answered(self, msg, ref: str) -> bool:
        if self.dedup and msg["message_id"] in self.answered:
            self.api.observe("porch_duplicate_absorbed", ref,
                             {"agent": msg["sender"],
                              "message_id": msg["message_id"],
                              "step": msg["body"].get("step")})
            return True
        self.answered.add(msg["message_id"])
        return False

    def _unreachable(self, msg, ref: str, error: str, kind: str) -> None:
        self.api.observe("porch_unreachable", ref,
                         {"agent": msg["sender"],
                          "message_id": msg["message_id"],
                          "mcp_url": self.settings.mcp_url, "error": error})
        self.api.reply(msg, kind, {"in_reply_to": msg["message_id"],
                                   "step": msg["body"].get("step"),
                                   "ok": False,
                                   "decision": "gateway_unreachable",
                                   "error": error})

    def handle_home_describe_request(self, msg):
        agent, mid = msg["sender"], msg["message_id"]
        ref = f"{self.run_ref}:{mid}"
        if self._already_answered(msg, ref):
            return
        status, payload = self._call(agent, "describe_home", {})
        if status == "unreachable":
            self._unreachable(msg, ref, payload, "home_describe_result")
            return
        if status == "tool_error":
            self.api.observe("porch_rejected", ref,
                             {"agent": agent, "message_id": mid,
                              "tool": "describe_home", "step": "describe",
                              "error": str(payload)[:200]})
            self.api.reply(msg, "home_describe_result",
                           {"in_reply_to": mid, "ok": False,
                            "decision": "rejected",
                            "error": str(payload)[:200]})
            return
        devices = payload.get("devices") or []
        summary = {
            "devices": len(devices),
            "platforms": sorted({str(d.get("platform")) for d in devices}),
            "hardware": payload.get("hardware"),
        }
        self.api.observe("porch_described", ref,
                         {"agent": agent, "message_id": mid, **summary})
        self.api.reply(msg, "home_describe_result",
                       {"in_reply_to": mid, "ok": True, **summary,
                        "device_ids": sorted(str(d.get("device_id"))
                                             for d in devices)})

    def handle_home_action_request(self, msg):
        agent, mid, body = msg["sender"], msg["message_id"], msg["body"]
        ref = f"{self.run_ref}:{mid}"
        if self._already_answered(msg, ref):
            return
        arguments: dict[str, Any] = {
            "device_id": body["device_id"],
            "command": body["command"],
            "reason": f"{body.get('reason', '')} [town:{ref}]",
        }
        if body.get("params"):
            arguments["params"] = body["params"]
        if body.get("approval_id"):
            arguments["approval_id"] = body["approval_id"]
        status, payload = self._call(agent, "request_action", arguments)
        if status == "unreachable":
            self._unreachable(msg, ref, payload, "home_action_result")
            return
        request = {"agent": agent, "message_id": mid,
                   "step": body.get("step"), "device": body["device_id"],
                   "command": body["command"],
                   "params": body.get("params") or {}}
        if status == "tool_error":
            self.api.observe("porch_rejected", ref,
                             {**request, "tool": "request_action",
                              "error": str(payload)[:200]})
            self.api.reply(msg, "home_action_result",
                           {"in_reply_to": mid, "step": body.get("step"),
                            "decision": "rejected", "executed": False,
                            "verified": False,
                            "error": str(payload)[:200]})
            return
        outcome = {
            "decision": payload.get("decision"),
            "executed": bool(payload.get("executed")),
            "verified": bool(payload.get("verified")),
            "audit_id": payload.get("audit_id"),
        }
        if payload.get("approval_id"):
            outcome["approval_id"] = payload["approval_id"]
        self.api.observe("porch_decision", ref, {
            **request, **outcome,
            "porch_reason": str(payload.get("reason", ""))[:200],
            "latency_ms": payload.get("latency_ms"),
            "control_path": payload.get("control_path"),
        })
        self.api.reply(msg, "home_action_result",
                       {"in_reply_to": mid, "step": body.get("step"),
                        **outcome,
                        "reason": str(payload.get("reason", ""))[:200]})


# -- agents that use the home -------------------------------------------


@role("porch_home_agent")
class HomeAgent(SimAgent):
    """Finds the gateway in the town index and walks its plan in order.

    Each step waits for Porch's answer, then asks the auditor to read the
    device from the hub. A step held for the owner asks the owner once.
    The agent stops at the first unreachable gateway and retries nothing.
    """

    def on_start(self):
        self.plan = list(self.config.get("plan") or [])
        self.index = -1
        self.pending: str | None = None
        self.approvals: dict[str, str] = {}
        self.api.later(float(self.config.get("start_at", 1.0)), self.begin)

    def begin(self):
        self.gateway = _first(self.api, "home.act")
        self.owner = _first(self.api, "home.owner")
        self.auditor = _first(self.api, "home.audit")
        if self.gateway is None:
            self.api.observe("gateway_not_found", self.name,
                             {"capability": "home.act"})
            return
        self.api.observe("gateway_found", self.gateway,
                         {"capability": "home.act"})
        if self.config.get("describe", True):
            sent = self.api.send(self.gateway, "home_describe_request", {})
            self.pending = sent["message_id"]
        else:
            self.next_step()

    def _abort(self, body) -> None:
        self.api.observe("plan_aborted", self.name,
                         {"at_step": body.get("step"),
                          "error": body.get("error")})

    def handle_home_describe_result(self, msg):
        body = msg["body"]
        if body.get("in_reply_to") != self.pending:
            return
        self.pending = None
        if body.get("decision") == "gateway_unreachable":
            self._abort(body)
            return
        self.next_step()

    def next_step(self):
        self.index += 1
        if self.index >= len(self.plan):
            self.api.observe("plan_finished", self.name,
                             {"steps": len(self.plan)})
            return
        step = self.plan[self.index]
        body = {"step": step["step"], "device_id": step["device"],
                "command": step["command"],
                "params": step.get("params") or {},
                "reason": step.get("reason", "")}
        if step.get("approval_from"):
            body["approval_id"] = self.approvals.get(step["approval_from"])
        sent = self.api.send(self.gateway, "home_action_request", body)
        self.pending = sent["message_id"]

    def handle_home_action_result(self, msg):
        body = msg["body"]
        if body.get("in_reply_to") != self.pending:
            self.api.observe("late_result_ignored", msg["message_id"],
                             {"step": body.get("step")})
            return
        self.pending = None
        step = self.plan[self.index]
        self.api.observe("home_result_received", msg["message_id"],
                         {"step": step["step"],
                          "decision": body.get("decision"),
                          "executed": body.get("executed"),
                          "verified": body.get("verified")})
        if body.get("decision") == "gateway_unreachable":
            self._abort(body)
            return
        if body.get("approval_id"):
            self.approvals[step["step"]] = body["approval_id"]
        if step.get("expect_state") and self.auditor:
            self.api.send(self.auditor, "hub_check_request",
                          {"step": step["step"],
                           "device_id": step["device"]})
        else:
            self.after_check()

    def handle_hub_check_result(self, msg):
        self.after_check()

    def after_check(self):
        step = self.plan[self.index]
        approval_id = self.approvals.get(step["step"])
        if step.get("ask_owner") and approval_id and self.owner:
            self.api.send(self.owner, "approval_request",
                          {"step": step["step"],
                           "approval_id": approval_id})
        else:
            self.next_step()

    def handle_approval_answer(self, msg):
        self.next_step()


@role("porch_impersonator")
class Impersonator(SimAgent):
    """Sends one request that names another agent as its sender.

    The envelope is signed with this agent's own key, which is all it
    has. Town's auth layer checks the signature against the claimed
    sender before delivery.
    """

    def on_start(self):
        self.api.later(float(self.config.get("start_at", 1.0)), self.forge)

    def forge(self):
        engine = self.api._engine
        gateway = _first(self.api, "home.act")
        if gateway is None:
            return
        victim = self.config["claim_to_be"]
        body = {"step": self.config["step"],
                "device_id": self.config["device"],
                "command": self.config["command"], "params": {},
                "reason": self.config.get("reason", "")}
        engine.layers["identity"].create(self.name)
        envelope = engine.layers["communication"].envelope(
            victim, gateway, "home_action_request", body)
        envelope["signature"] = engine.layers["auth"].sign_as(
            self.name, envelope["body"])
        engine.record_intent(self.name, "send_as",
                             {"claimed_sender": victim, "to": gateway,
                              "kind": "home_action_request", "body": body})
        self.api.observe("forged_request_sent", envelope["message_id"],
                         {"claimed_sender": victim,
                          "step": self.config["step"],
                          "device": self.config["device"],
                          "command": self.config["command"]})
        engine.layers["transport"].send(self.name, gateway, envelope)
        self.api.later(1.0, self.ask_auditor)

    def ask_auditor(self):
        auditor = _first(self.api, "home.audit")
        if auditor:
            self.api.send(auditor, "hub_check_request",
                          {"step": self.config["step"],
                           "device_id": self.config["device"]})

    def handle_hub_check_result(self, msg):
        pass


# -- the household's side and the independent observer ------------------


@role("porch_home_owner")
class HomeOwner(SimAgent):
    """The owner: sets who is home, answers held requests, reads the audit."""

    def on_start(self):
        self.settings = Settings()
        self.run_ref = self.api._engine.run_id
        self.api.register(["home.owner"])
        occupied = bool(self.config.get("occupied", False))
        ok, _ = self._owner("POST", "/owner/occupancy",
                            {"occupied": occupied})
        if ok:
            self.api.observe("home_precondition_set", "occupancy",
                             {"occupied": occupied})
        self.api.later(float(self.config.get("audit_at", 40.0)),
                       self.read_audit)

    def _owner(self, method: str, path: str,
               body: dict[str, Any] | None = None) -> tuple[bool, Any]:
        error = self.settings.problem
        if not error:
            try:
                with _http() as http:
                    response = http.request(
                        method, self.settings.owner_url + path, json=body,
                        headers={"x-owner-key": self.settings.owner_key})
                response.raise_for_status()
                return True, response.json()
            except (httpx.HTTPError, ValueError) as exc:
                error = _short(exc)
        self.api.observe("owner_action_failed", path, {"error": error})
        return False, error

    def handle_approval_request(self, msg):
        agent, body = msg["sender"], msg["body"]
        approval_id = str(body.get("approval_id"))
        ok, pending = self._owner("GET", "/owner/approvals")
        waiting = next((a for a in pending if a.get("id") == approval_id),
                       None) if ok else None
        wanted = waiting is not None and any(
            rule.get("agent") == waiting.get("agent") == agent
            and rule.get("device") == waiting.get("device")
            and rule.get("command") == waiting.get("command")
            for rule in self.config.get("approve") or [])
        status = "not_found"
        if waiting is not None:
            verdict = "approve" if wanted else "decline"
            ok, answer = self._owner(
                "POST", f"/owner/approvals/{approval_id}/{verdict}")
            status = answer.get("status") if ok else "failed"
        self.api.observe("owner_answered", approval_id,
                         {"agent": agent, "step": body.get("step"),
                          "status": status,
                          "device": (waiting or {}).get("device"),
                          "command": (waiting or {}).get("command")})
        self.api.reply(msg, "approval_answer",
                       {"step": body.get("step"),
                        "approval_id": approval_id, "status": status})

    def read_audit(self):
        ok, log = self._owner("GET", "/owner/audit")
        if not ok:
            return
        entries = []
        for entry in log:
            match = REF_PATTERN.search(str(entry.get("agent_reason", "")))
            if not match or not match.group(1).startswith(self.run_ref + ":"):
                continue
            verification = entry.get("verification") or {}
            entries.append({
                "audit_id": entry.get("id"), "ref": match.group(1),
                "agent": entry.get("agent"), "device": entry.get("device"),
                "command": entry.get("command"),
                "decision": entry.get("decision"),
                "executed": bool(verification.get("executed")),
                "verified": bool(verification.get("verified")),
            })
        self.api.observe("porch_audit_observed", self.run_ref,
                         {"entries": entries,
                          "entries_in_log": len(log)})


@role("porch_hub_auditor")
class HubAuditor(SimAgent):
    """Reads a device's state from the Device Hub, not from Porch."""

    def on_start(self):
        self.settings = Settings()
        self.api.register(["home.audit"])

    def handle_hub_check_request(self, msg):
        body = msg["body"]
        device_id = str(body.get("device_id"))
        state: dict[str, Any] | None = None
        error = self.settings.problem
        if not error:
            try:
                with _http() as http:
                    response = http.get(
                        f"{self.settings.hub_url}/devices/{device_id}/state")
                response.raise_for_status()
                state = _flatten(response.json())
            except (httpx.HTTPError, ValueError) as exc:
                error = _short(exc)
        if state is None:
            self.api.observe("hub_unreachable", device_id,
                             {"agent": msg["sender"],
                              "step": body.get("step"), "error": error})
        else:
            self.api.observe("hub_state_observed", device_id,
                             {"agent": msg["sender"],
                              "step": body.get("step"), "state": state})
        self.api.reply(msg, "hub_check_result",
                       {"step": body.get("step"), "state": state})


# -- the validator ------------------------------------------------------


def _expectations(spec) -> list[dict[str, Any]]:
    """Every expectation the scenario declares, one row per step."""
    rows = []
    for agent in spec.agents:
        if agent.role == "porch_home_agent":
            if agent.config.get("describe", True):
                rows.append({"agent": agent.name, "step": "describe",
                             "expect": agent.config.get("expect_describe",
                                                        "described")})
            for step in agent.config.get("plan") or []:
                rows.append({"agent": agent.name, **step})
        elif agent.role == "porch_impersonator":
            rows.append({"agent": agent.name, "step": agent.config["step"],
                         "device": agent.config["device"],
                         "command": agent.config["command"],
                         "expect": "not_delivered",
                         "expect_state": agent.config.get("expect_state"),
                         "claim_to_be": agent.config["claim_to_be"]})
    return rows


def _for_step(events, agent: str, step: str):
    return [e for e in events
            if e.detail.get("agent") == agent and e.detail.get("step") == step]


def _observed(trace: Trace, row: dict[str, Any]) -> dict[str, Any]:
    """What the trace holds for one expectation row."""
    agent, step = row["agent"], row["step"]
    decisions = _for_step(trace.find("porch_decision"), agent, step)
    rejections = _for_step(trace.find("porch_rejected"), agent, step)
    described = [e for e in trace.find("porch_described")
                 if e.detail.get("agent") == agent] if step == "describe" else []
    checks = _for_step(trace.find("hub_state_observed"), agent, step)
    if row.get("expect") == "not_delivered":
        forged = [e for e in trace.find("forged_request_sent")
                  if e.detail.get("step") == step and e.observer == agent]
        ids = {e.subject for e in forged}
        refused = [e for e in trace.find("delivery_failed")
                   if e.subject in ids
                   and e.detail.get("reason") == "bad signature"]
        handled = [e for e in trace.find("porch_decision")
                   + trace.find("porch_rejected")
                   if e.detail.get("message_id") in ids]
        outcome = ("not_delivered" if forged and refused and not handled
                   else "delivered" if handled else "no_evidence")
        events = forged + refused + handled
    elif decisions:
        outcome = decisions[-1].detail.get("decision")
        events = decisions
    elif rejections:
        outcome, events = "rejected", rejections
    elif described:
        outcome, events = "described", described
    else:
        outcome, events = "no_evidence", []
    return {"outcome": outcome, "events": events, "decisions": decisions,
            "state": checks[-1].detail.get("state") if checks else None,
            "checks": checks}


def _state_matches(expected: dict[str, Any] | None,
                   state: dict[str, Any] | None) -> bool:
    return state is not None and all(
        state.get(name) == value for name, value in (expected or {}).items())


def _audit(trace: Trace):
    observed = trace.find("porch_audit_observed")
    if not observed:
        return None, []
    entries = observed[-1].detail.get("entries")
    return observed[-1], entries if isinstance(entries, list) else []


@validator("porch_home")
def porch_home(spec, trace: Trace) -> list[StageResult]:
    rows = [(row, _observed(trace, row)) for row in _expectations(spec)]
    gateways = [a for a in spec.agents if a.role == "porch_gateway"]
    stages: list[StageResult] = []

    # 1. Porch answered at all.
    down = (trace.find("porch_unreachable")
            + trace.find("porch_adapter_misconfigured")
            + trace.find("owner_action_failed")
            + trace.find("hub_unreachable"))
    answered = (trace.find("porch_described") + trace.find("porch_decision")
                + trace.find("porch_rejected"))
    if down:
        stages.append(_failed(
            "gateway_reachable", [e.event_id for e in down],
            "Porch did not answer: "
            + str(down[0].detail.get("error")
                  or down[0].detail.get("problem"))
            + "; start Porch, set PORCH_CONFIG, and run again"))
    else:
        stages.append(_check(
            "gateway_reachable", bool(answered),
            [e.event_id for e in answered],
            "no MCP call to Porch was recorded",
            f"Porch answered {len(answered)} MCP calls"))

    # 2. Discovery through the town index.
    cards = [e for e in trace.find("card_registered", verified=True)
             if e.subject in {g.name for g in gateways}
             and "home.act" in (e.detail.get("capabilities") or [])]
    agents = [a.name for a in spec.agents if a.role == "porch_home_agent"]
    found = [e for e in trace.find("gateway_found") if e.observer in agents]
    stages.append(_check(
        "discovery",
        bool(cards) and {e.observer for e in found} == set(agents),
        [e.event_id for e in cards + found],
        "every home agent must find the gateway's verified card in the"
        " town index",
        f"{len(found)} agents found the gateway's verified card"))

    # 3. describe_home through the adapter.
    expect_home = (gateways[0].config.get("expect_home") or {}
                   if gateways else {})
    wrong, evidence = [], []
    for row, seen in rows:
        if row["step"] != "describe" or row["expect"] != "described":
            continue
        evidence += [e.event_id for e in seen["events"]]
        detail = seen["events"][-1].detail if seen["events"] else {}
        if seen["outcome"] != "described" or any(
                detail.get(name) != value
                for name, value in expect_home.items()):
            wrong.append(f"{row['agent']}: expected {expect_home},"
                         f" observed {seen['outcome']}"
                         f" {({k: detail.get(k) for k in expect_home})}")
    stages.append(_check("home_described", not wrong, evidence,
                         "; ".join(wrong) or "no describe_home answer"
                         " was recorded",
                         "describe_home matched the expected home"))

    # 4. Porch's decision for every step is the one the policy requires.
    wrong, evidence, total = [], [], 0
    for row, seen in rows:
        if row.get("expect") not in ("allowed", "denied",
                                     "held_for_owner_approval"):
            continue
        total += 1
        evidence += [e.event_id for e in seen["decisions"]]
        acted = row["expect"] == "allowed"
        ok = bool(seen["decisions"]) and all(
            e.detail.get("decision") == row["expect"]
            and e.detail.get("executed") is acted
            and e.detail.get("verified") is acted
            for e in seen["decisions"])
        if not ok:
            wrong.append(f"{row['agent']}/{row['step']}: expected"
                         f" {row['expect']}, observed {seen['outcome']}")
    stages.append(_check(
        "decisions_match_policy", not wrong, evidence,
        "; ".join(wrong) or "no Porch decision was recorded",
        f"{total} of {total} steps got the expected decision; every"
        " allowed step was executed and verified by Porch, no other"
        " step was executed"))

    # 5. The hub, read independently, shows the expected device state.
    wrong, evidence, total = [], [], 0
    for row, seen in rows:
        if not row.get("expect_state"):
            continue
        total += 1
        evidence += [e.event_id for e in seen["checks"]]
        if not _state_matches(row["expect_state"], seen["state"]):
            wrong.append(f"{row['agent']}/{row['step']}: expected"
                         f" {row['expect_state']}, hub showed"
                         f" {seen['state']}")
    stages.append(_check(
        "hub_state_confirmed", not wrong, evidence,
        "; ".join(wrong) or "the auditor recorded no hub state",
        f"{total} of {total} hub reads matched the expected state"))

    # 6. Owner approval: held, answered by the owner, usable once.
    asked = [(row, seen) for row, seen in rows if row.get("ask_owner")]
    if not asked:
        stages.append(StageResult(name="owner_approval", status="not_tested",
                                  note="no step asks the owner"))
    else:
        wrong, evidence = [], []
        for row, seen in asked:
            ids = [e.detail.get("approval_id") for e in seen["decisions"]
                   if e.detail.get("approval_id")]
            answers = [e for e in trace.find("owner_answered",
                                             status="approved")
                       if e.subject in ids]
            used = [other for other, _ in rows
                    if other["agent"] == row["agent"]
                    and other.get("approval_from") == row["step"]
                    and other.get("expect") == "allowed"]
            used_ok = [
                e for other in used
                for e in _for_step(trace.find("porch_decision"),
                                   other["agent"], other["step"])
                if e.detail.get("decision") == "allowed"
                and e.detail.get("verified") is True]
            evidence += [e.event_id
                         for e in seen["decisions"] + answers + used_ok]
            if not ids or not answers or len(used_ok) != len(used):
                wrong.append(f"{row['agent']}/{row['step']}: held="
                             f"{bool(ids)}, owner approved="
                             f"{bool(answers)}, approved retries"
                             f" verified={len(used_ok)}/{len(used)}")
        stages.append(_check(
            "owner_approval", not wrong, evidence,
            "; ".join(wrong) or "no held request was recorded",
            "the request was held, the owner approved it through Porch,"
            " and the retry with the approval id was executed and"
            " verified"))

    # 7. An agent Porch does not know gets nothing.
    strangers = [(row, seen) for row, seen in rows
                 if row.get("expect") == "rejected"]
    if not strangers:
        stages.append(StageResult(
            name="unknown_agent_rejected", status="not_tested",
            note="no agent without a Porch key in this scenario"))
    else:
        wrong = [f"{row['agent']}/{row['step']}: observed {seen['outcome']}"
                 for row, seen in strangers if seen["outcome"] != "rejected"]
        stages.append(_check(
            "unknown_agent_rejected", not wrong,
            [e.event_id for _, seen in strangers for e in seen["events"]],
            "; ".join(wrong) or "no rejection was recorded",
            f"Porch refused all {len(strangers)} calls from an agent"
            " with no Porch key"))

    # 8. A forged sender never reaches Porch.
    forgeries = [(row, seen) for row, seen in rows
                 if row.get("expect") == "not_delivered"]
    if not forgeries:
        stages.append(StageResult(
            name="impersonation_blocked", status="not_tested",
            note="no impersonator in this scenario"))
    else:
        unread = [row for row, seen in forgeries
                  if seen["outcome"] == "not_delivered"
                  and row.get("expect_state") and seen["state"] is None]
        wrong = [f"{row['agent']} as {row['claim_to_be']}: request was"
                 f" {seen['outcome']}, hub showed {seen['state']}"
                 for row, seen in forgeries if row not in unread
                 and (seen["outcome"] != "not_delivered"
                      or not _state_matches(row.get("expect_state"),
                                            seen["state"]))]
        if unread and not wrong:
            stages.append(_missing(
                "impersonation_blocked",
                "the forged request was refused, but the hub was not read,"
                " so the device state is unconfirmed"))
        else:
            stages.append(_check(
                "impersonation_blocked", not wrong,
                [e.event_id for _, seen in forgeries
                 for e in seen["events"] + seen["checks"]],
                "; ".join(wrong) or "no forged request was recorded",
                "the request signed with the wrong key failed Town's"
                " signature check, never reached Porch, and the device"
                " did not change"))

    # 9. A request Town delivered twice reached the device once.
    audit_event, entries = _audit(trace)
    duplicated = [e for e in trace.find("message_duplicated")
                  if e.detail.get("kind") == "home_action_request"]
    if not duplicated:
        stages.append(StageResult(
            name="duplicate_applied_once", status="not_tested",
            note="no duplicated request in this scenario"))
    elif audit_event is None:
        stages.append(_missing(
            "duplicate_applied_once",
            "Porch's audit log was not read, so the number of executions"
            " is unknown"))
    else:
        wrong = []
        for e in duplicated:
            ref = f"{trace.run_id}:{e.subject}"
            count = sum(1 for entry in entries if entry.get("ref") == ref)
            if count != 1:
                wrong.append(f"request {e.subject} was delivered twice and"
                             f" Porch's audit log holds {count} entries"
                             " for it")
        absorbed = [a for a in trace.find("porch_duplicate_absorbed")
                    if a.detail.get("message_id")
                    in {e.subject for e in duplicated}]
        stages.append(_check(
            "duplicate_applied_once", not wrong,
            [e.event_id for e in duplicated + absorbed]
            + [audit_event.event_id],
            "; ".join(wrong),
            f"{len(duplicated)} request was delivered twice by Town and"
            " Porch's audit log holds exactly one entry for it"))

    # 10. Porch's own audit log agrees with what the adapter saw.
    decisions = trace.find("porch_decision")
    if audit_event is None:
        stages.append(_missing("audit_complete",
                               "Porch's audit log was not read"))
    else:
        by_id = {entry.get("audit_id"): entry for entry in entries}
        seen_ids = {e.detail.get("audit_id") for e in decisions}
        wrong = []
        for e in decisions:
            entry = by_id.get(e.detail.get("audit_id"))
            if (entry is None or entry.get("ref") != e.subject
                    or AUDIT_DECISION.get(entry.get("decision"))
                    != e.detail.get("decision")
                    or entry.get("agent") != e.detail.get("agent")):
                wrong.append(f"decision {e.detail.get('audit_id')} for"
                             f" {e.subject} has no matching audit entry")
        wrong += [f"audit entry {entry.get('audit_id')} matches no"
                  " adapter decision"
                  for entry in entries
                  if entry.get("audit_id") not in seen_ids]
        stages.append(_check(
            "audit_complete", not wrong,
            [audit_event.event_id] + [e.event_id for e in decisions],
            "; ".join(wrong) or "no decision was recorded",
            f"{len(entries)} Porch audit entries match {len(decisions)}"
            " adapter decisions by audit id, agent, request and"
            " decision"))
    return stages


# -- command line: step table and verification --------------------------


def step_table(bundle_dir: str) -> str:
    """Expected and observed, one line per step, from the bundle alone."""
    from nandatown.bundle import load_bundle

    bundle = load_bundle(bundle_dir)
    trace = Trace(bundle["events"], bundle["run"].run_id)
    lines = ["| Agent | Step | Request | Expected | Observed | Hub state"
             " expected | Hub state observed | Match |",
             "|---|---|---|---|---|---|---|---|"]
    for row in _expectations(bundle["profile"]):
        seen = _observed(trace, row)
        want_state = row.get("expect_state")
        match = (seen["outcome"] == row["expect"]
                 and (not want_state
                      or _state_matches(want_state, seen["state"])))
        request = (f"{row['command']} {row['device']}"
                   if row.get("device") else "describe_home")
        if row.get("params"):
            request += f" {json.dumps(row['params'], sort_keys=True)}"
        shown = ({name: (seen["state"] or {}).get(name)
                  for name in want_state} if want_state else "")
        lines.append(
            f"| {row['agent']} | {row['step']} | {request} |"
            f" {row['expect']} | {seen['outcome']} |"
            f" {json.dumps(want_state) if want_state else ''} |"
            f" {json.dumps(shown) if want_state else ''} |"
            f" {'yes' if match else 'NO'} |")
    result = bundle["result"]
    lines += ["", "| Stage | Status | Note |", "|---|---|---|"]
    lines += [f"| {stage.name} | {stage.status} | {stage.note} |"
              for stage in result.stages]
    lines += ["", f"Verdict: {result.verdict}"
              f" (evaluator {result.evaluator_version},"
              f" run {result.run_id})"]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "table":
        print(step_table(argv[1]))
        return 0
    if len(argv) == 2 and argv[0] == "verify":
        from nandatown.bundle import verify_bundle

        problems = verify_bundle(argv[1])
        for problem in problems:
            print(f"problem: {problem}")
        if not problems:
            print("bundle verified: hashes match and the porch_home"
                  " validator reproduces the recorded result")
        return 1 if problems else 0
    print("usage: python porch_town.py table|verify <bundle directory>")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
