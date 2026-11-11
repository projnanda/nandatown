"""The Porch integration under examples/porch.

Porch is a home gateway for agents that runs outside Town. Three kinds
of test live here:

* tests that need no Porch at all: the scenario files load, and a Porch
  that cannot be reached is never reported as a pass;
* adapter contract tests against FakePorch, a small stand-in defined in
  this file. It follows the subset of Porch's MCP, owner and hub
  interface that the scenario uses. It is a Town fixture: a pass here
  shows the adapter and validator behave as written, not that Porch is
  compatible;
* live tests against a real Porch, skipped unless PORCH_CONFIG is set
  when pytest starts. Those are the compatibility check; see
  examples/porch/README.md for how to run Porch.
"""

import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import Body, FastAPI, Header
from fastapi.responses import JSONResponse, Response

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.sim.agents import ROLES
from nandatown.sim.runner import resolve_spec, run_lab

REPO_ROOT = Path(__file__).resolve().parents[1]
PORCH_DIR = REPO_ROOT / "examples" / "porch"
SCENARIO = str(PORCH_DIR / "porch_home.yaml")
NO_DEDUP = str(PORCH_DIR / "porch_home_no_dedup.yaml")

# Captured before any test points the adapter at a fixture.
LIVE_CONFIG = os.environ.get("PORCH_CONFIG", "")
LIVE_MCP_URL = os.environ.get("PORCH_MCP_URL", "")
LIVE_HUB_URL = os.environ.get("PORCH_HUB_URL", "")
live = pytest.mark.skipif(
    not LIVE_CONFIG, reason="needs a running Porch; set PORCH_CONFIG")

LAMP = "home-1/desk-lamp-plug"
LOCK = "home-1/front-door-lock"
DRYER = "home-1/laundry-dryer"
HEATER = "home-1/space-heater-plug"


def statuses(result) -> dict[str, str]:
    return {stage.name: stage.status for stage in result.stages}


def notes(result) -> dict[str, str]:
    return {stage.name: stage.note for stage in result.stages}


def events(bundle_dir: str, kind: str):
    return [e for e in load_bundle(bundle_dir)["events"] if e.kind == kind]


def files_containing(bundle_dir: str, needle: str) -> list[str]:
    return [path.name for path in Path(bundle_dir).rglob("*")
            if path.is_file() and needle in path.read_text(errors="ignore")]


# -- no Porch needed ----------------------------------------------------


def test_scenarios_load_and_register_the_porch_roles():
    for path in (SCENARIO, NO_DEDUP):
        spec = resolve_spec(path)
        assert spec.validator == "porch_home"
        for agent in spec.agents:
            assert agent.role in ROLES
            # YAML reads a bare On or Off as a boolean; Porch needs the
            # command name.
            commands = [step["command"]
                        for step in agent.config.get("plan") or []]
            commands += [agent.config["command"]] \
                if "command" in agent.config else []
            assert all(isinstance(c, str) for c in commands), agent.name


@pytest.fixture
def unreachable_porch(tmp_path, monkeypatch):
    """A configured adapter whose Porch is not listening (port 9)."""
    config = tmp_path / "porch_config.json"
    keys = {"energy": "unused-" + secrets.token_hex(8),
            "concierge": "unused-" + secrets.token_hex(8)}
    config.write_text(json.dumps({
        "hub_url": "http://127.0.0.1:9",
        "owner_key": "unused-" + secrets.token_hex(8),
        "agents": {keys["energy"]: {"id": "energy-agent"},
                   keys["concierge"]: {"id": "concierge-agent"}},
    }))
    monkeypatch.setenv("PORCH_CONFIG", str(config))
    monkeypatch.setenv("PORCH_MCP_URL", "http://127.0.0.1:9/mcp")
    monkeypatch.delenv("PORCH_HUB_URL", raising=False)
    return keys


def test_an_unreachable_porch_is_never_a_pass(unreachable_porch, tmp_path):
    """Boundary case: the gateway is down. Every agent stops at its first
    call, nothing is retried, and no stage that needs Porch passes."""
    bundle_dir, result = run_lab(SCENARIO, str(tmp_path / "runs"))
    status = statuses(result)
    assert result.verdict == "failed"
    assert status["gateway_reachable"] == "failed"
    assert "Porch did not answer" in notes(result)["gateway_reachable"]
    for name in ("home_described", "decisions_match_policy",
                 "hub_state_confirmed", "owner_approval",
                 "unknown_agent_rejected", "impersonation_blocked",
                 "duplicate_applied_once", "audit_complete"):
        assert status[name] == "not_enough_evidence", name
    # Discovery is Town's own index and still works.
    assert status["discovery"] == "passed"
    assert events(bundle_dir, "porch_decision") == []
    aborted = {e.observer for e in events(bundle_dir, "plan_aborted")}
    assert aborted == {"energy-agent", "concierge-agent", "stranger"}
    # One call per agent: the describe. No action request was sent.
    sent = [e for e in events(bundle_dir, "message_sent")
            if e.detail["kind"] == "home_action_request"
            and e.observer != "impersonator"]
    assert sent == []
    for key in unreachable_porch.values():
        assert files_containing(bundle_dir, key) == []
    assert verify_bundle(bundle_dir) == []


def test_a_missing_porch_config_is_never_a_pass(tmp_path, monkeypatch):
    monkeypatch.delenv("PORCH_CONFIG", raising=False)
    _, result = run_lab(SCENARIO, str(tmp_path / "runs"))
    assert result.verdict == "failed"
    assert statuses(result)["gateway_reachable"] == "failed"
    assert "PORCH_CONFIG" in notes(result)["gateway_reachable"]


# -- FakePorch: a Town fixture, not Porch -------------------------------

FAKE_DEVICES = {
    LOCK: ("entry", "matter", {"DoorLock": {"LockState": "Locked"}}),
    "home-1/kitchen-leak-sensor": (
        "sensor", "matter", {"BooleanState": {"StateValue": False}}),
    "home-1/main-water-valve": (
        "water_shutoff", "matter",
        {"ValveConfigurationAndControl": {"CurrentState": "Open"}}),
    LAMP: ("lighting", "matter", {"OnOff": {"OnOff": True}}),
    DRYER: ("appliance", "lg_thinq",
            {"PorchLaundryExtension": {"RelativeHourToStart": 0}}),
    "home-1/robot-vacuum": ("appliance", "lg_thinq", {}),
    "home-1/air-purifier": ("appliance", "lg_thinq", {}),
    HEATER: ("heating", "google_home", {"OnOff": {"OnOff": False}}),
}
FAKE_EFFECTS = {
    "Off": ("OnOff", "OnOff", False),
    "On": ("OnOff", "OnOff", True),
    "LockDoor": ("DoorLock", "LockState", "Locked"),
    "UnlockDoor": ("DoorLock", "LockState", "Unlocked"),
}


class FakePorch:
    """The rules the scenario exercises, as Porch's README states them:
    scopes per agent, a 12 hour delay limit, no heating in an empty home,
    unlocking held for the owner, an approval usable once."""

    def __init__(self):
        self.owner_key = "fixture-owner-" + secrets.token_hex(8)
        self.agents = {
            "fixture-" + secrets.token_hex(8): {
                "id": "energy-agent", "scopes": ["lighting", "appliance"]},
            "fixture-" + secrets.token_hex(8): {
                "id": "concierge-agent",
                "scopes": ["lighting", "heating", "entry"]},
        }
        self.state = {device: {cluster: dict(attributes)
                               for cluster, attributes in clusters.items()}
                      for device, (_, _, clusters) in FAKE_DEVICES.items()}
        self.occupied = True
        self.audit: list[dict] = []
        self.approvals: dict[str, dict] = {}
        self.app = self._build()

    def secrets(self) -> list[str]:
        return [self.owner_key, *self.agents]

    def _log(self, **entry) -> str:
        entry["id"] = f"a-{len(self.audit) + 1:03d}"
        self.audit.append(entry)
        return entry["id"]

    def _decide(self, agent, device, command, params, approval_id):
        capability = FAKE_DEVICES[device][0]
        if capability not in agent["scopes"]:
            return "deny", "Permission: outside this agent's scope."
        if capability == "appliance" and command == "SetDelayStart" \
                and int(params.get("hours", 0)) > 12:
            return "deny", "Household limit: at most 12 hours."
        if capability == "heating" and command == "On" \
                and not self.occupied:
            return "deny", "Safety rule: nobody is home."
        if capability == "entry" and command == "UnlockDoor":
            approval = self.approvals.get(approval_id or "")
            if approval is None:
                return "hold", "Needs the owner's approval."
            if (approval["agent"], approval["device"],
                    approval["command"]) != (agent["id"], device, command):
                return "deny", "Approval was given for a different action."
            if approval["status"] == "pending":
                return "waiting", "The owner has not answered yet."
            if approval["status"] != "approved":
                return "deny", "Approval declined or already used."
            approval["status"] = "used"
            return "approved", "The owner approved this exact action."
        return "allow", "Allowed within scope."

    def _request_action(self, agent, arguments):
        device, command = arguments["device_id"], arguments["command"]
        params = arguments.get("params") or {}
        if device not in FAKE_DEVICES:
            return None
        decision, why = self._decide(agent, device, command, params,
                                     arguments.get("approval_id"))
        entry = {"agent": agent["id"], "device": device,
                 "command": command, "agent_reason": arguments["reason"],
                 "decision": decision}
        if decision == "deny":
            return {"decision": "denied", "reason": why, "executed": False,
                    "audit_id": self._log(**entry)}
        if decision in ("hold", "waiting"):
            approval_id = arguments.get("approval_id")
            if decision == "hold":
                approval_id = secrets.token_hex(3)
                self.approvals[approval_id] = {
                    "id": approval_id, "agent": agent["id"],
                    "device": device, "command": command,
                    "status": "pending"}
            return {"decision": "held_for_owner_approval", "reason": why,
                    "executed": False, "approval_id": approval_id,
                    "audit_id": self._log(**entry, approval_id=approval_id)}
        if command == "SetDelayStart":
            cluster, name, value = ("PorchLaundryExtension",
                                    "RelativeHourToStart", params["hours"])
        else:
            cluster, name, value = FAKE_EFFECTS[command]
        self.state[device].setdefault(cluster, {})[name] = value
        verification = {"executed": True, "verified": True}
        return {"decision": "allowed", "reason": why, **verification,
                "latency_ms": 1, "control_path": FAKE_DEVICES[device][1],
                "audit_id": self._log(**entry, verification=verification)}

    def _build(self) -> FastAPI:
        app = FastAPI()
        denied = JSONResponse({"error": "owner key required"},
                              status_code=401)

        @app.post("/mcp")
        def mcp(body: dict = Body(...), authorization: str = Header("")):
            if "id" not in body:
                return Response(status_code=202)
            if body.get("method") == "initialize":
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": body["id"],
                     "result": {"protocolVersion": "2025-06-18",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "FakePorch",
                                               "version": "fixture"}}},
                    headers={"mcp-session-id": secrets.token_hex(8)})
            params = body.get("params") or {}
            agent = self.agents.get(authorization.removeprefix("Bearer "))
            result = None
            if agent is not None and params.get("name") == "describe_home":
                result = {"hardware": "fixture", "devices": [
                    {"device_id": device, "platform": platform}
                    for device, (_, platform, _) in FAKE_DEVICES.items()]}
            elif agent is not None \
                    and params.get("name") == "request_action":
                result = self._request_action(
                    agent, params.get("arguments") or {})
            if result is None:
                text = ("Unknown agent key." if agent is None
                        else "No such tool or device.")
                payload = {"content": [{"type": "text", "text": text}],
                           "isError": True}
            else:
                payload = {"content": [{"type": "text",
                                        "text": json.dumps(result)}],
                           "isError": False, "structuredContent": result}
            return {"jsonrpc": "2.0", "id": body["id"], "result": payload}

        @app.post("/owner/occupancy")
        def occupancy(body: dict = Body(...), x_owner_key: str = Header("")):
            if x_owner_key != self.owner_key:
                return denied
            self.occupied = bool(body["occupied"])
            return {"occupied": self.occupied}

        @app.get("/owner/approvals")
        def approvals(x_owner_key: str = Header("")):
            if x_owner_key != self.owner_key:
                return denied
            return [a for a in self.approvals.values()
                    if a["status"] == "pending"]

        @app.post("/owner/approvals/{approval_id}/{verdict}")
        def answer(approval_id: str, verdict: str,
                   x_owner_key: str = Header("")):
            if x_owner_key != self.owner_key:
                return denied
            approval = self.approvals.get(approval_id)
            if approval is None or approval["status"] != "pending":
                return JSONResponse({"error": "no such pending approval"},
                                    status_code=404)
            approval["status"] = ("approved" if verdict == "approve"
                                  else "denied")
            self._log(agent=approval["agent"], device=approval["device"],
                      command=approval["command"],
                      decision="approved" if verdict == "approve"
                      else "declined")
            return approval

        @app.get("/owner/audit")
        def audit(x_owner_key: str = Header("")):
            if x_owner_key != self.owner_key:
                return denied
            return self.audit

        @app.get("/devices/{device_id:path}/state")
        def device_state(device_id: str):
            if device_id not in self.state:
                return JSONResponse({"error": "no such device"},
                                    status_code=404)
            return {cluster: {"attributes": attributes}
                    for cluster, attributes in self.state[device_id].items()}

        return app


@pytest.fixture
def fake_porch(tmp_path, monkeypatch):
    import uvicorn

    porch = FakePorch()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(
        porch.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"{url}/devices/{LAMP}/state", timeout=1,
                      trust_env=False)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    config = tmp_path / "porch_config.json"
    config.write_text(json.dumps({
        "hub_url": url, "owner_key": porch.owner_key,
        "agents": {key: {"id": agent["id"]}
                   for key, agent in porch.agents.items()}}))
    monkeypatch.setenv("PORCH_CONFIG", str(config))
    monkeypatch.setenv("PORCH_MCP_URL", f"{url}/mcp")
    monkeypatch.delenv("PORCH_HUB_URL", raising=False)
    yield porch
    server.should_exit = True
    thread.join(timeout=5)


def assert_full_workflow(bundle_dir, result, credentials):
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    status = statuses(result)
    for name in ("gateway_reachable", "discovery", "home_described",
                 "decisions_match_policy", "hub_state_confirmed",
                 "owner_approval", "unknown_agent_rejected",
                 "impersonation_blocked", "duplicate_applied_once",
                 "audit_complete"):
        assert status[name] == "passed", detail
    decisions = events(bundle_dir, "porch_decision")
    assert [e.detail["decision"] for e in decisions] == [
        "allowed", "allowed", "denied", "denied",          # energy-agent
        "denied", "held_for_owner_approval", "allowed",    # concierge-agent
        "denied", "allowed"]
    assert len(events(bundle_dir, "porch_duplicate_absorbed")) == 1
    assert {e.detail["agent"] for e in events(bundle_dir, "porch_rejected")
            } == {"stranger"}
    for credential in credentials:
        assert files_containing(bundle_dir, credential) == []
    assert verify_bundle(bundle_dir) == []


def test_fixture_full_workflow(fake_porch, tmp_path):
    bundle_dir, result = run_lab(SCENARIO, str(tmp_path / "runs"))
    assert_full_workflow(bundle_dir, result, fake_porch.secrets())
    assert fake_porch.state[LAMP]["OnOff"]["OnOff"] is False
    assert fake_porch.state[LOCK]["DoorLock"]["LockState"] == "Locked"
    assert fake_porch.state[HEATER]["OnOff"]["OnOff"] is False
    # Nine agent requests plus the owner's approval.
    assert len(fake_porch.audit) == 10
    assert not any(e["agent"] == "stranger" for e in fake_porch.audit)


def test_fixture_duplicate_guard_off_fails_on_purpose(fake_porch, tmp_path):
    """Negative control: without the adapter's guard the duplicated
    request reaches the gateway twice, and only that stage fails."""
    _, result = run_lab(NO_DEDUP, str(tmp_path / "runs"))
    assert result.verdict == "failed"
    failed = [name for name, value in statuses(result).items()
              if value == "failed"]
    assert failed == ["duplicate_applied_once"]
    assert "holds 2 entries" in notes(result)["duplicate_applied_once"]
    assert len([e for e in fake_porch.audit if e["device"] == LAMP]) == 2


def test_fixture_weak_town_auth_lets_a_forged_request_through(
        fake_porch, tmp_path):
    """The adapter trusts Town's auth layer for who sent a message. With
    auth swapped for plain.v1 the forged request runs under the victim's
    Porch key, and the run says so."""
    _, result = run_lab(SCENARIO, str(tmp_path / "runs"),
                        layer_overrides={"auth": "plain.v1"})
    assert result.verdict == "failed"
    status = statuses(result)
    assert status["impersonation_blocked"] == "failed"
    assert status["hub_state_confirmed"] == "failed"
    assert fake_porch.state[LAMP]["OnOff"]["OnOff"] is True


def test_fixture_owner_declines_a_request_it_did_not_list(
        fake_porch, tmp_path):
    """The owner agent approves only what its config lists. Without the
    listing it declines, the agent's retry is denied, and the door stays
    locked; the run fails at owner_approval, not silently."""
    spec = yaml.safe_load(Path(SCENARIO).read_text())
    spec["plugin_files"] = [str(PORCH_DIR / "porch_town.py")]
    owner = next(a for a in spec["agents"] if a["name"] == "owner")
    owner["config"]["approve"] = []
    scenario = tmp_path / "porch_home_owner_declines.yaml"
    scenario.write_text(yaml.safe_dump(spec, sort_keys=False))
    bundle_dir, result = run_lab(str(scenario), str(tmp_path / "runs"))
    assert result.verdict == "failed"
    assert statuses(result)["owner_approval"] == "failed"
    answers = events(bundle_dir, "owner_answered")
    assert [e.detail["status"] for e in answers] == ["denied"]
    unlocked = [e for e in fake_porch.audit
                if e["command"] == "UnlockDoor"
                and (e.get("verification") or {}).get("executed")]
    assert unlocked == []
    assert fake_porch.state[LOCK]["DoorLock"]["LockState"] == "Locked"


# -- live: a real Porch --------------------------------------------------


@pytest.fixture
def live_porch(monkeypatch):
    monkeypatch.setenv("PORCH_CONFIG", LIVE_CONFIG)
    for name, value in (("PORCH_MCP_URL", LIVE_MCP_URL),
                        ("PORCH_HUB_URL", LIVE_HUB_URL)):
        if value:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    with open(LIVE_CONFIG, encoding="utf-8") as f:
        config = json.load(f)
    return [config["owner_key"], *config["agents"]]


@live
def test_live_full_workflow(live_porch, tmp_path):
    bundle_dir, result = run_lab(SCENARIO, str(tmp_path / "runs"))
    assert_full_workflow(bundle_dir, result, live_porch)


@live
def test_live_duplicate_guard_off_fails_on_purpose(live_porch, tmp_path):
    _, result = run_lab(NO_DEDUP, str(tmp_path / "runs"))
    assert result.verdict == "failed"
    failed = [name for name, value in statuses(result).items()
              if value == "failed"]
    assert failed == ["duplicate_applied_once"]
