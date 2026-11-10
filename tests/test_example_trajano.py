"""The Trajano integration example, checked without Trajano.

The committed bundles must verify and keep their verdicts, damaged copies of
their evidence must not pass, a run with no project must be incomplete rather
than passed, and the MCP client is exercised against a tiny local server.
The live runs against Trajano itself are in the example's README.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.sim.runner import load_plugin_files, run_lab
from nandatown.sim.validators import evaluate_scenario

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "examples", "integrations", "trajano")
PLUGIN = os.path.join(EXAMPLE, "trajano_plugin.py")
EVIDENCE = os.path.join(EXAMPLE, "evidence")
CASES = ["deal", "lost_accept", "over_mandate"]


@pytest.fixture(scope="module", autouse=True)
def plugin():
    load_plugin_files([PLUGIN])


def _bundle(case: str) -> dict:
    return load_bundle(os.path.join(EVIDENCE, case))


def _replay(bundle: dict, events) -> str:
    return evaluate_scenario(bundle["profile"], bundle["run"].run_id,
                             events).verdict


def _edit(events, kind: str, change) -> list:
    """A copy of the events with `change(detail)` applied to each of `kind`."""
    out = copy.deepcopy(events)
    hits = [e for e in out if e.kind == kind]
    assert hits, kind
    for event in hits:
        change(event.detail)
    return out


@pytest.mark.parametrize("case", CASES)
def test_recorded_evidence_verifies_and_passed(case):
    bundle = os.path.join(EVIDENCE, case)
    assert verify_bundle(bundle) == []
    assert _bundle(case)["result"].verdict == "passed"


def test_lost_audit_leaves_the_deal_incomplete():
    bundle = _bundle("deal")
    events = [e for e in bundle["events"] if e.kind != "trajano_audit"]
    assert _replay(bundle, events) == "incomplete"


def test_a_second_order_in_the_database_fails_the_deal():
    bundle = _bundle("deal")

    def duplicate(detail):
        orders = detail["audit"]["orders"]
        orders.append(dict(orders[0], order_id=orders[0]["order_id"] + 1))
    events = _edit(bundle["events"], "trajano_audit", duplicate)
    assert _replay(bundle, events) == "failed"


def test_another_project_commit_fails_the_connection():
    bundle = _bundle("deal")
    events = _edit(bundle["events"], "trajano_connected",
                   lambda d: d.update(project_commit="0" * 40))
    assert _replay(bundle, events) == "failed"


def test_an_accept_reply_sent_elsewhere_does_not_count():
    bundle = _bundle("deal")
    events = copy.deepcopy(bundle["events"])
    for event in events:
        if (event.kind == "message_sent"
                and event.detail.get("kind") == "accept_offer_result"):
            event.detail["to"] = "someone-else"
    assert _replay(bundle, events) != "passed"


def test_a_retry_that_got_a_new_order_fails_the_replay_check():
    bundle = _bundle("lost_accept")
    events = copy.deepcopy(bundle["events"])
    accepts = [e for e in events if e.kind == "trajano_tool_result"
               and e.detail["tool"] == "accept_offer"]
    assert len(accepts) == 2
    accepts[1].detail["result"]["order_id"] += 1
    assert _replay(bundle, events) == "failed"


def test_without_the_lost_reply_the_retry_is_unexplained():
    bundle = _bundle("lost_accept")
    events = [e for e in bundle["events"] if e.kind != "message_dropped"]
    assert _replay(bundle, events) != "passed"


def test_an_accepted_over_mandate_offer_fails():
    bundle = _bundle("over_mandate")
    events = copy.deepcopy(bundle["events"])
    for event in events:
        if (event.kind == "trajano_tool_result"
                and event.detail["tool"] == "accept_offer"):
            event.detail["result"].update(error=None, order_id=1)
    assert _replay(bundle, events) == "failed"


def test_missing_project_is_incomplete_not_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAJANO_PROJECT", str(tmp_path / "no-trajano"))
    _, result = run_lab(os.path.join(EXAMPLE, "trajano_deal.yaml"),
                        str(tmp_path / "runs"))
    assert result.verdict == "incomplete"
    judged = [s for s in result.stages
              if s.name not in ("ledger_conserved", "privacy")]
    assert judged and all(s.status == "not_enough_evidence" for s in judged)


def test_run_refuses_without_the_project(tmp_path):
    done = subprocess.run(
        [sys.executable, os.path.join(EXAMPLE, "run.py"), "--project",
         str(tmp_path), "--case", "deal", "--out", str(tmp_path / "runs")],
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 2
    assert "setup incomplete" in done.stderr


def test_importing_the_plugin_does_not_load_mcp():
    code = ("import sys; from nandatown.sim.runner import load_plugin_files; "
            f"load_plugin_files([{PLUGIN!r}]); "
            "print('mcp' in sys.modules or 'trajano_mcp' in sys.modules)")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=60, check=True)
    assert done.stdout.strip() == "False"


# -- the MCP client against a stand-in server -------------------------------

@pytest.fixture()
def fake_trajano():
    pytest.importorskip("mcp")
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("fake-trajano", stateless_http=True, json_response=True,
                     streamable_http_path="/")

    @server.tool()
    def get_order(order_id: int) -> dict:
        return {"error": None, "order": {"id": order_id, "status": "DELIVERED"}}

    @server.tool()
    def accept_offer(offer_id: int) -> dict:
        return {"error": {"code": "MANDATE_INVALID", "message": "over cap",
                          "retryable": False}}

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    web = uvicorn.Server(uvicorn.Config(server.streamable_http_app(),
                                        host="127.0.0.1", port=port,
                                        log_level="warning"))
    thread = threading.Thread(target=web.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not web.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/"
    web.should_exit = True
    thread.join(10)


def _client():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "trajano_mcp_under_test", os.path.join(EXAMPLE, "trajano_mcp.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_client_lists_tools_and_returns_results_and_refusals(fake_trajano):
    client = _client()
    assert client.list_tools(fake_trajano, "key") == ["accept_offer",
                                                      "get_order"]
    order = client.call(fake_trajano, "key", "get_order", {"order_id": 7})
    assert order["order"] == {"id": 7, "status": "DELIVERED"}
    refused = client.call(fake_trajano, "key", "accept_offer", {"offer_id": 1})
    assert refused["error"]["code"] == "MANDATE_INVALID"


def test_client_raises_one_error_type_when_unreachable():
    pytest.importorskip("mcp")
    client = _client()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(client.TrajanoError):
        client.call(f"http://127.0.0.1:{port}/", "key", "get_order",
                    {"order_id": 1}, timeout=5)


def test_client_raises_on_bad_arguments(fake_trajano):
    client = _client()
    with pytest.raises(client.TrajanoError):
        client.call(fake_trajano, "key", "get_order", {"order_id": "x"})
