"""Referral integration: scenarios, the MCP client and offline verification.

The live tests start a real Referral MCP server. They run when
``NANDATOWN_REFERRAL_CMD`` names one (for example
``/path/to/referral/.venv/bin/referral serve --history none``) or a
``referral`` command is on PATH, and are skipped otherwise. The fail-closed
run, the MCP client test and the scenario-shape test need no Referral.
"""

from __future__ import annotations

import glob
import json
import os
import shlex
import shutil
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files, run_lab

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations" / "referral"
SCENARIOS = INTEGRATION / "scenarios"
sys.path.insert(0, str(INTEGRATION))

import mcp_stdio  # noqa: E402

load_plugin_files([str(INTEGRATION / "plugin.py")])


def _referral_command() -> list[str] | None:
    env = os.environ.get("NANDATOWN_REFERRAL_CMD")
    if env:
        return shlex.split(env)
    if shutil.which("referral"):
        return ["referral", "serve", "--history", "none"]
    return None


live = pytest.mark.skipif(
    _referral_command() is None,
    reason="no Referral server: set NANDATOWN_REFERRAL_CMD or install referral")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NANDATOWN_HOME", str(tmp_path / "home"))
    command = _referral_command()
    if command:
        monkeypatch.setenv("NANDATOWN_REFERRAL_CMD", shlex.join(command))


def _run(name: str, tmp_path: Path):
    bundle, result = run_lab(str(SCENARIOS / f"{name}.yaml"),
                             str(tmp_path / "runs"))
    stages = {s.name: s.status for s in result.stages}
    return bundle, result, stages


def _events(bundle: str) -> list[dict]:
    with open(os.path.join(bundle, "events.jsonl")) as f:
        return [json.loads(line) for line in f]


# -- no Referral needed ---------------------------------------------------------


def test_the_two_runs_differ_only_in_the_trust_layer():
    main = yaml.safe_load((SCENARIOS / "referral_fabricated_citation.yaml").read_text())
    control = yaml.safe_load(
        (SCENARIOS / "referral_fabricated_citation_reputation_control.yaml").read_text())
    for spec in (main, control):
        spec.pop("name"), spec.pop("description")
    assert main.pop("layers") == {"trust": "referral.v1"}
    assert control.pop("layers") == {"trust": "reputation.v1"}
    assert main == control


def test_unreachable_referral_fails_closed(tmp_path):
    bundle, result, stages = _run("referral_unreachable_fail_closed", tmp_path)
    assert result.verdict == "passed", stages
    kinds = [e["kind"] for e in _events(bundle)]
    assert "referral_unavailable" in kinds
    assert "escrow_released" not in kinds and "escrow_held" not in kinds


def test_mcp_client_round_trip_and_tool_error(tmp_path):
    server = tmp_path / "fake_server.py"
    server.write_text(textwrap.dedent('''
        import json, sys
        for line in sys.stdin:
            msg = json.loads(line)
            if "id" not in msg:
                continue
            m = msg["method"]
            if m == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {},
                          "serverInfo": {"name": "fake", "version": "1"}}
            elif m == "tools/list":
                result = {"tools": [{"name": "echo"}, {"name": "boom"}]}
            elif msg["params"]["name"] == "echo":
                result = {"content": [{"type": "text",
                          "text": json.dumps(msg["params"]["arguments"])}]}
            else:
                result = {"isError": True,
                          "content": [{"type": "text", "text": "bad"}]}
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                              "result": result}), flush=True)
    '''))
    client = mcp_stdio.StdioMcpClient([sys.executable, str(server)])
    try:
        assert client.start() == ["boom", "echo"]
        assert client.server_info["name"] == "fake"
        assert client.call("echo", a=1) == {"a": 1}
        with pytest.raises(mcp_stdio.McpToolError):
            client.call("boom")
    finally:
        client.close()


def test_mcp_client_reports_a_server_that_exits(tmp_path):
    client = mcp_stdio.StdioMcpClient(
        [sys.executable, "-c", "import sys; sys.exit(3)"], startup_timeout=5)
    with pytest.raises(mcp_stdio.McpError):
        client.start()
    client.close()


@pytest.mark.parametrize("name, verdict", [
    ("referral_fabricated_citation", "passed"),
    ("referral_fabricated_citation_reputation_control", "failed"),
    ("referral_unlocked_forecast", "passed"),
    ("referral_unreachable_fail_closed", "passed"),
])
def test_recorded_evidence_verifies_offline(name, verdict):
    """The committed bundles replay under this checkout's evaluator."""
    bundle = INTEGRATION / "evidence" / name
    assert verify_bundle(str(bundle)) == []
    result = json.loads((bundle / "result.json").read_text())
    assert result["verdict"] == verdict


# -- live Referral ------------------------------------------------------------------


@live
def test_fabricated_citations_are_caught_and_rematched(tmp_path):
    bundle, result, stages = _run("referral_fabricated_citation", tmp_path)
    assert result.verdict == "passed", stages
    events = _events(bundle)
    verdicts = [e for e in events if e["kind"] == "referral_verdict"]
    assert [(v["detail"]["worker"], v["detail"]["status"]) for v in verdicts] == [
        ("worker-cheap", "failed"), ("worker-honest", "passed")]
    paid = [e["detail"]["to"] for e in events if e["kind"] == "escrow_released"]
    assert paid == ["worker-honest"]
    ranking = [e for e in events if e["kind"] == "registry_ranking"][-1]
    assert ranking["detail"]["ranking"] == ["worker-honest", "worker-cheap"]
    assert verify_bundle(bundle) == []


@live
def test_reputation_control_fails_on_purpose(tmp_path):
    bundle, result, stages = _run(
        "referral_fabricated_citation_reputation_control", tmp_path)
    assert result.verdict == "failed"
    assert stages["fabrication_rejected"] == "failed"
    assert stages["reputation_matches_checks"] == "failed"
    assert stages["referral_connected"] == "passed"
    events = _events(bundle)
    paid = [e["detail"]["to"] for e in events if e["kind"] == "escrow_released"]
    assert paid == ["worker-cheap"]
    credit = [e["detail"]["delta"] for e in events
              if e["kind"] == "reputation_updated" and e["subject"] == "worker-cheap"]
    assert credit == [1]


@live
def test_delivery_before_forecast_is_refused(tmp_path):
    bundle, result, stages = _run("referral_unlocked_forecast", tmp_path)
    assert result.verdict == "passed", stages
    refused = [e for e in _events(bundle) if e["kind"] == "referral_call_refused"]
    assert len(refused) == 1
    assert "forecast" in refused[0]["detail"]["error"]


@live
def test_no_referral_key_reaches_the_bundle(tmp_path, monkeypatch):
    keys: list[str] = []
    original = mcp_stdio.StdioMcpClient.call

    def spying(self, tool, **arguments):
        answer = original(self, tool, **arguments)
        if tool == "register_agent" and "key" in answer:
            keys.append(answer["key"])
        return answer

    monkeypatch.setattr(mcp_stdio.StdioMcpClient, "call", spying)
    bundle, result, _ = _run("referral_fabricated_citation", tmp_path)
    assert len(keys) == 3
    for path in glob.glob(os.path.join(bundle, "*")):
        text = Path(path).read_text()
        assert not any(key in text for key in keys), path


@live
def test_runs_are_deterministic(tmp_path):
    def trace(sub):
        bundle, _, _ = _run("referral_fabricated_citation", tmp_path / sub)
        return [(e["kind"], e["subject"],
                 json.dumps(e["detail"], sort_keys=True).replace(e["run_id"], "RUN"))
                for e in _events(bundle) if e["kind"] not in ("run_created", "run_finished")]

    assert trace("a") == trace("b")
