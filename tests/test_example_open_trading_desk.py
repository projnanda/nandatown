"""The open-trading-desk example must never pass without real evidence.

The project is hosted separately and is not installed in CI, so these tests
use only the Town side: no project means no pass, the recorded bundles still
verify, and damaging the evidence a stage relies on stops that stage passing.
"""

import copy
import glob
import importlib.util
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time

import pytest

from nandatown.bundle import verify_bundle
from nandatown.records import TownEvent
from nandatown.sim.agents import ROLES
from nandatown.sim.runner import load_plugin_files, run_lab
from nandatown.sim.scenario import load_scenario_file
from nandatown.sim.validators import evaluate_scenario

HERE = os.path.join(os.path.dirname(__file__), "..", "examples", "integrations",
                    "open-trading-desk")
EVIDENCE = os.path.join(HERE, "evidence")
CASES = {"clean": "otd_workflow.yaml", "lost_reply": "otd_lost_reply.yaml"}
PLUGIN = os.path.join(HERE, "otd_plugin.py")


def _load(case):
    with open(os.path.join(EVIDENCE, case, "events.jsonl")) as f:
        return [json.loads(line) for line in f]


def _verdict(case, events, **pm_config):
    load_plugin_files([PLUGIN])
    spec = load_scenario_file(os.path.join(HERE, CASES[case]))
    if pm_config:
        spec = spec.model_copy(update={"agents": [
            a.model_copy(update={"config": {**a.config, **pm_config}})
            if a.role == "otd_pm" else a for a in spec.agents]})
    parsed = [TownEvent.model_validate(e) for e in events]
    result = evaluate_scenario(spec, "run", parsed)
    return result.verdict, {s.name: s.status for s in result.stages}


def _first(events, kind, **match):
    for event in events:
        detail = event["detail"]
        if event["kind"] == kind and all(detail.get(k) == v
                                         for k, v in match.items()):
            return event
    raise AssertionError((kind, match))


def _result_of(events, tool, n=0):
    return [e for e in events if e["kind"] == "otd_tool_result"
            and e["subject"] == tool][n]


@pytest.mark.parametrize("name", CASES.values())
def test_missing_project_is_incomplete_not_passed(tmp_path, monkeypatch, name):
    monkeypatch.setenv("OTD_PROJECT", str(tmp_path / "absent"))
    _, result = run_lab(os.path.join(HERE, name), str(tmp_path))
    judged = [s for s in result.stages
              if s.name not in ("ledger_conserved", "privacy")]
    assert result.verdict != "passed"
    assert judged and all(s.status == "not_enough_evidence" for s in judged)


def test_runner_refuses_a_missing_project(tmp_path):
    done = subprocess.run(
        [sys.executable, os.path.join(HERE, "run.py"), "--project",
         str(tmp_path / "absent"), "--case", "clean", "--out", str(tmp_path)],
        capture_output=True, text=True)
    assert done.returncode == 2
    assert "setup incomplete" in done.stderr


@pytest.mark.parametrize("case", CASES)
def test_recorded_evidence_verifies(case):
    load_plugin_files([PLUGIN])
    assert verify_bundle(os.path.join(EVIDENCE, case)) == []
    assert _verdict(case, _load(case))[0] == "passed"


@pytest.mark.parametrize("case", CASES)
def test_evidence_holds_handles_never_tokens(case):
    held = []
    for path in glob.glob(os.path.join(EVIDENCE, case, "*.jsonl")):
        with open(path) as f:
            held += re.findall(r'"delegation_token": ?"([^"]*)"', f.read())
    assert held and all(re.fullmatch(r"delegation-\d+", h) for h in held)


def _drop_kind(kind):
    return lambda ev: [e for e in ev if e["kind"] != kind]


def _edit_result(ev, tool, change, pick=lambda args: True):
    """Change an MCP result and its reply together, so they stay consistent."""
    for e in ev:
        if e["kind"] == "otd_tool_result" and e["subject"] == tool \
                and pick(e["detail"]["args"]):
            change(e["detail"]["result"])
            for m in ev:
                body = m["detail"].get("body") if m["kind"] == "message_sent" else None
                if body and "result" in body \
                        and body["request_id"] == e["detail"]["request_id"]:
                    body["result"] = copy.deepcopy(e["detail"]["result"])
    return ev


def _approve_nothing(ev):
    def change(result):
        for order in result["orders"]:
            order["approved"] = 0
    return _edit_result(ev, "close_day", change)


def _break_chain(ev):
    return _edit_result(
        ev, "close_day", lambda r: r.update(chain_verified=False))


def _inflate_score(ev):
    return _edit_result(
        ev, "close_day", lambda r: r["scores"][0].update(hits=999))


def _approve_oversized(ev):
    _edit_result(ev, "place_order", lambda r: r.update(approved=True),
                 lambda args: args["position_pct"] == 0.25)
    for e in ev:
        if e["kind"] == "pm_refusal_seen" and e["detail"]["case"] == "oversized":
            e["detail"]["reply"]["approved"] = True
    return ev


def _reply_disagrees_with_mcp(ev):
    _result_of(ev, "close_day")["detail"]["result"]["equity"] = 1.0
    return ev


def _claim_early(ev):
    for e in ev:
        if e["kind"] == "pm_order_state":
            e["detail"]["state"] = "filled"
            e["detail"]["basis"] = "order_result"
            break
    return ev


def _sent(ev, kind, n=0):
    return [e for e in ev if e["kind"] == "message_sent"
            and e["detail"]["kind"] == kind][n]


def _readdress(ev, message, to):
    message["detail"]["to"] = to
    for e in ev:
        if e["kind"] == "message_delivered" and e["subject"] == message["subject"]:
            e["detail"]["to"] = to
    return ev


def _order_goes_to_owner(ev):
    """A whole, consistent exchange that the PM took no part in."""
    request = _sent(ev, "order_request")
    rid = request["detail"]["body"]["request_id"]
    reply = next(e for e in ev if e["kind"] == "message_sent"
                 and e["detail"]["kind"] == "order_result"
                 and e["detail"]["body"]["request_id"] == rid)
    request["observer"] = "owner"
    return _readdress(ev, reply, "owner")


def _written_by_owner(kind):
    def damage(ev):
        for e in ev:
            if e["kind"] == kind:
                e["observer"] = "owner"
        return ev
    return damage


def _revocation_from_pm(ev):
    _sent(ev, "delegation_revoked")["observer"] = "pm"
    return ev


def _revocation_to_desk(ev):
    return _readdress(ev, _sent(ev, "delegation_revoked"), "desk")


def _views(ev):
    return [e for e in ev if e["kind"] == "otd_tool_result"
            and e["subject"] == "request_view"]


def _remove_exchanges(ev, request_ids):
    gone = {e["subject"] for e in ev if e["kind"] == "message_sent"
            and e["detail"]["body"].get("request_id") in request_ids}
    return [e for e in ev
            if not (e["kind"] in ("message_sent", "message_delivered")
                    and e["subject"] in gone)
            and not (e["kind"] == "otd_tool_result"
                     and e["detail"]["request_id"] in request_ids)]


def _set_scores(ev, entries, per_provider):
    def change(result):
        result["chain_message"] = re.sub(
            r"\d+ entries", f"{entries} entries", result["chain_message"])
        for score in result["scores"]:
            score.update(per_provider)
    return _edit_result(ev, "close_day", change)


def _one_round_only(ev):
    """Only the decision round happened; every total is made to agree."""
    gone = {e["detail"]["request_id"] for e in _views(ev)
            if e["detail"]["args"]["as_of_minute"] != 65
            or e["detail"]["args"]["ticker"] != "NVDA"}
    assert len(gone) == 42
    for e in ev:
        if e["kind"] == "pm_views_requested":
            e["detail"]["count"] = 3
    return _set_scores(_remove_exchanges(ev, gone), 3, {
        "n_views": 1, "hits": 1, "misses": 0, "expired": 0, "hit_rate": 1.0,
        "no_shows": 0})


def _omit_one_view(ev):
    last = _views(ev)[-1]
    provider = last["detail"]["args"]["provider_id"]

    def change(result):
        result["chain_message"] = re.sub(
            r"\d+ entries", "44 entries", result["chain_message"])
        score = next(s for s in result["scores"]
                     if s["provider_id"] == provider)
        score["n_views"] -= 1
        score["expired" if score["expired"] else "misses"] -= 1
    for e in ev:
        if e["kind"] == "pm_views_requested":
            e["detail"]["count"] = 44
    return _edit_result(_remove_exchanges(ev, {last["detail"]["request_id"]}),
                        "close_day", change)


def _ask_one_view_twice(ev):
    """Totals still add up, but one combination is asked twice, one never."""
    view = _views(ev)[-1]
    rid = view["detail"]["request_id"]
    view["detail"]["args"]["as_of_minute"] = 245
    for e in ev:
        if e["kind"] == "message_sent" \
                and e["detail"]["body"].get("request_id") == rid \
                and "args" in e["detail"]["body"]:
            e["detail"]["body"]["args"]["as_of_minute"] = 245
    return ev


def _is_last_view(args):
    return (args["as_of_minute"], args["ticker"]) == (305, "TSLA") \
        and args["provider_id"] == "claude-reasoner-01"


def _view_from_impostor(ev):
    return _edit_result(ev, "request_view",
                        lambda r: r["view"].update(provider_id="impostor"),
                        _is_last_view)


def _view_of_other_ticker(ev):
    return _edit_result(ev, "request_view",
                        lambda r: r["view"].update(ticker="FAKE"), _is_last_view)


def _impossible_scores(ev):
    def change(result):
        score = result["scores"][0]
        shows = score["n_views"]
        score.update(hits=-1, misses=shows + 1, expired=0, hit_rate=-1 / shows)
    return _edit_result(ev, "close_day", change)


def _unknown_view_status(ev):
    def change(result):
        score = next(s for s in result["scores"]
                     if s["provider_id"] == "claude-reasoner-01")
        score["n_views"] -= 1
        score["no_shows"] += 1
        score["expired"] -= 1
    _edit_result(ev, "request_view", lambda r: r.update(status="garbage"),
                 _is_last_view)
    return _edit_result(ev, "close_day", change)


def _score_listed_twice(ev):
    return _edit_result(
        ev, "close_day",
        lambda r: r["scores"].append(copy.deepcopy(r["scores"][0])))


def _no_delegation_issued(ev):
    return _remove_exchanges(ev, {"owner-issue_delegation"})


def _delivery_of(ev, message):
    return next(e for e in ev if e["kind"] == "message_delivered"
                and e["subject"] == message["subject"])


def _revocation_arrives_before_sent(ev):
    told = _sent(ev, "delegation_revoked")
    arrival = _delivery_of(ev, told)
    ev.remove(arrival)
    ev.insert(ev.index(told), arrival)
    return ev


def _revocation_lands_elsewhere(ev):
    _delivery_of(ev, _sent(ev, "delegation_revoked"))["detail"]["to"] = "desk"
    return ev


def _order_reply_lands_elsewhere(ev):
    _delivery_of(ev, _sent(ev, "order_result"))["detail"]["to"] = "owner"
    return ev


def _decision_round_reply_arrives_late(ev):
    """The decision was made before this provider's reply had arrived."""
    view = next(e for e in _views(ev)
                if e["detail"]["args"]["provider_id"] == "claude-reasoner-01")
    reply = next(e for e in ev if e["kind"] == "message_sent"
                 and e["detail"]["kind"] == "tool_result"
                 and e["detail"]["body"]["request_id"]
                 == view["detail"]["request_id"])
    arrival = _delivery_of(ev, reply)
    ev.remove(arrival)
    ev.insert(ev.index(_first(ev, "pm_decision")) + 1, arrival)
    return ev


def _after_the_close(ev, call, also=()):
    """The summary already counts an exchange that had not happened yet."""
    rid = call["detail"]["request_id"]
    subjects = {e["subject"] for e in ev if e["kind"] == "message_sent"
                and e["detail"]["body"].get("request_id") == rid}
    moved = [e for e in ev if e is call or any(e is a for a in also)
             or (e["kind"] in ("message_sent", "message_delivered")
                 and e["subject"] in subjects)]
    kept = [e for e in ev if not any(e is m for m in moved)]
    close = _delivery_of(kept, _sent(kept, "tool_result", -1))
    at = kept.index(close) + 1
    return kept[:at] + moved + kept[at:]


def _view_after_the_close(ev):
    return _after_the_close(ev, _views(ev)[-1])


def _order_after_the_close(ev):
    return _after_the_close(ev, _result_of(ev, "place_order", -1),
                            also=[_first(ev, "pm_refusal_seen", case="revoked")])


def _other_commit(ev):
    _first(ev, "desk_connected")["detail"]["project_commit"] = "0" * 40
    return ev


def _modified_files(ev):
    _first(ev, "desk_connected")["detail"]["project_dirty"] = True
    return ev


def _halted(ev):
    ev.append({**_first(ev, "pm_decision"), "kind": "pm_halted",
               "event_id": "halt", "subject": "workflow",
               "detail": {"error": "KeyError"}})
    return ev


@pytest.mark.parametrize("case,stage,damage", [
    ("clean", "decision_and_fill", _order_goes_to_owner),
    ("clean", "pm_knowledge", _order_goes_to_owner),
    ("lost_reply", "fault_and_retry", _order_goes_to_owner),
    ("clean", "decision_and_fill", _written_by_owner("pm_decision")),
    ("clean", "pm_knowledge", _written_by_owner("pm_order_state")),
    ("clean", "risk_refusals", _written_by_owner("pm_refusal_seen")),
    ("clean", "connection", _written_by_owner("desk_connected")),
    ("clean", "risk_refusals", _revocation_from_pm),
    ("clean", "risk_refusals", _revocation_to_desk),
    ("clean", "scoring_and_chain", _one_round_only),
    ("clean", "scoring_and_chain", _omit_one_view),
    ("clean", "scoring_and_chain", _ask_one_view_twice),
    ("clean", "scoring_and_chain", _view_from_impostor),
    ("clean", "scoring_and_chain", _view_of_other_ticker),
    ("clean", "scoring_and_chain", _impossible_scores),
    ("clean", "scoring_and_chain", _unknown_view_status),
    ("clean", "scoring_and_chain", _score_listed_twice),
    ("clean", "risk_refusals", _no_delegation_issued),
    ("clean", "risk_refusals", _revocation_arrives_before_sent),
    ("clean", "risk_refusals", _revocation_lands_elsewhere),
    ("clean", "decision_and_fill", _order_reply_lands_elsewhere),
    ("clean", "discovery_and_views", _decision_round_reply_arrives_late),
    ("clean", "scoring_and_chain", _view_after_the_close),
    ("clean", "risk_refusals", _order_after_the_close),
    ("lost_reply", "risk_refusals", _order_after_the_close),
    ("clean", "connection", _other_commit),
    ("clean", "connection", _modified_files),
    ("clean", "pm_knowledge", _halted),
    ("clean", "connection", _drop_kind("desk_connected")),
    ("clean", "discovery_and_views", _drop_kind("pm_providers")),
    ("clean", "decision_and_fill", _approve_nothing),
    ("clean", "decision_and_fill", _drop_kind("pm_decision")),
    ("clean", "risk_refusals", _approve_oversized),
    ("clean", "risk_refusals", _drop_kind("pm_refusal_seen")),
    ("clean", "scoring_and_chain", _break_chain),
    ("clean", "scoring_and_chain", _inflate_score),
    ("clean", "scoring_and_chain", _reply_disagrees_with_mcp),
    ("clean", "pm_knowledge", _drop_kind("pm_order_state")),
    ("lost_reply", "fault_and_retry", _drop_kind("message_dropped")),
    ("lost_reply", "fault_and_retry", _drop_kind("pm_order_retry")),
    ("lost_reply", "pm_knowledge", _claim_early),
])
def test_damaged_evidence_does_not_pass(case, stage, damage):
    verdict, stages = _verdict(case, damage(copy.deepcopy(_load(case))))
    assert stages[stage] != "passed"
    assert verdict != "passed"


@pytest.mark.parametrize("stage,config", [
    ("discovery_and_views", {"expected_providers": ["a", "b", "c"]}),
    ("scoring_and_chain", {"tickers": ["NVDA", "AAPL"]}),
    ("scoring_and_chain", {"checkpoints": [65, 125]}),
])
def test_evidence_must_cover_the_day_the_scenario_names(stage, config):
    verdict, stages = _verdict("clean", _load("clean"), **config)
    assert stages[stage] != "passed"
    assert verdict != "passed"


def test_provider_with_only_expired_views_is_a_valid_score():
    def change(result):
        score = result["scores"][0]
        shows = score["n_views"]
        score.update(hits=0, misses=0, expired=shows, hit_rate=0.0)
    ev = _edit_result(_load("clean"), "close_day", change)
    assert _verdict("clean", ev)[0] == "passed"


def test_a_no_show_does_not_spoil_the_decision():
    """The provider that did not say long misses the decision round."""
    ev = _load("clean")
    absent = "claude-reasoner-01"
    view = next(e for e in _views(ev)
                if e["detail"]["args"]["provider_id"] == absent)
    no_show = {"status": "no_show", "reason": "deadline", "log_hash": "x"}
    view["detail"]["result"] = no_show
    for e in ev:
        if e["kind"] == "message_sent" and e["detail"]["body"].get(
                "request_id") == view["detail"]["request_id"] \
                and "result" in e["detail"]["body"]:
            e["detail"]["body"]["result"] = copy.deepcopy(no_show)

    def change(result):
        score = next(s for s in result["scores"]
                     if s["provider_id"] == absent)
        score["n_views"] -= 1
        score["no_shows"] += 1
        score["expired"] -= 1
    assert _verdict("clean", _edit_result(ev, "close_day", change))[0] == "passed"


def test_malformed_view_is_still_not_passed():
    ev = _edit_result(_load("clean"), "request_view",
                      lambda r: r.pop("view"),
                      lambda a: a["as_of_minute"] == 65)
    assert _verdict("clean", ev)[0] != "passed"


def _plugin():
    """The plugin's namespace. Town loads it without registering a module."""
    load_plugin_files([PLUGIN])
    return ROLES["otd_desk"].on_start.__globals__


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t",
                    "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


def test_commit_reports_a_changed_checkout(tmp_path):
    commit = _plugin()["_commit"]
    assert commit(str(tmp_path)) == ("unknown", True)
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "first")
    head, dirty = commit(str(tmp_path))
    assert re.fullmatch(r"[0-9a-f]{40}", head) and dirty is False
    (tmp_path / "a.py").write_text("x = 2\n")
    assert commit(str(tmp_path)) == (head, True)
    _git(tmp_path, "checkout", "-q", "--", "a.py")
    (tmp_path / "new.py").write_text("y = 1\n")
    assert commit(str(tmp_path)) == (head, True)

SERVER = """
import asyncio, os
from mcp.server.fastmcp import FastMCP
m = FastMCP("fake", log_level="ERROR")
@m.tool()
def ping() -> str: return "pong"
@m.tool()
async def slow() -> str:
    await asyncio.sleep(5); return "late"
@m.tool()
def die() -> str: os._exit(3)
m.run()
"""


@pytest.fixture
def make_bridge(tmp_path):
    """Build real bridges to a fake server, and always clean them up."""
    pytest.importorskip("mcp")
    spec = importlib.util.spec_from_file_location(
        "mcp_bridge", os.path.join(HERE, "mcp_bridge.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["mcp_bridge"] = module
    spec.loader.exec_module(module)

    async def invoke(session, tool, **arguments):
        result = await session.call_tool(tool, arguments)
        return result.content[0].text
    path = tmp_path / "server.py"
    path.write_text(SERVER)
    made = []

    def make(**kwargs):
        made.append(module.McpBridge(sys.executable, [str(path)], invoke,
                                     env=dict(os.environ), **kwargs))
        return made[-1]
    yield module, make
    for bridge in made:
        bridge.close(timeout=1.0)
    module._OPEN.clear()
    del sys.modules["mcp_bridge"]


def test_bridge_calls_and_closes(make_bridge):
    module, make = make_bridge
    bridge = make(call_timeout=1.0)
    assert sorted(bridge.start()) == ["die", "ping", "slow"]
    assert bridge.call("ping") == "pong"
    with pytest.raises(module.BridgeError, match="timed out"):
        bridge.call("slow")
    assert bridge.call("ping") == "pong"
    assert bridge.close() is True
    assert bridge.close() is True


def test_bridge_reports_a_dead_server(make_bridge):
    module, make = make_bridge
    bridge = make(call_timeout=5.0)
    bridge.start()
    with pytest.raises(module.BridgeError):
        bridge.call("die")
    assert bridge.close() is True
    with pytest.raises(module.BridgeError):
        bridge.call("ping")


def test_closing_during_a_call_frees_the_caller(make_bridge):
    module, make = make_bridge
    bridge = make(call_timeout=4.0)
    bridge.start()
    seen = {}

    def caller():
        started = time.monotonic()
        try:
            bridge.call("slow")
        except module.BridgeError as exc:
            seen["error"] = str(exc)
        seen["waited"] = time.monotonic() - started
    thread = threading.Thread(target=caller)
    thread.start()
    time.sleep(0.3)
    assert bridge.close(timeout=0.2) is True
    thread.join(3.0)
    assert not thread.is_alive()
    assert seen["error"] == "slow: session closed"
    assert seen["waited"] < 3.5


def test_close_does_not_raise_when_the_session_is_closing(make_bridge, monkeypatch):
    module, make = make_bridge
    bridge = make()
    bridge.start()

    def closing(item):
        raise module.BridgeError("session is not running")
    monkeypatch.setattr(bridge, "_put", closing)
    assert bridge.close(timeout=0.1) is False
    monkeypatch.undo()
    assert bridge.close() is True


def test_close_reports_a_session_that_failed_after_startup(make_bridge, monkeypatch):
    _, make = make_bridge
    bridge = make()

    async def broken(session, queue):
        raise RuntimeError("after startup")
    monkeypatch.setattr(bridge, "_serve", broken)
    bridge.start()
    bridge._thread.join(5.0)
    assert bridge.close() is False


def _runner():
    spec = importlib.util.spec_from_file_location(
        "otd_run", os.path.join(HERE, "run.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_deadline_is_not_mistaken_for_a_tool_timeout(make_bridge):
    _, make = make_bridge
    runner = _runner()
    bridge = make(call_timeout=4.0)
    bridge.start()
    previous = signal.signal(signal.SIGALRM, runner._expire)
    signal.setitimer(signal.ITIMER_REAL, 0.3)
    try:
        with pytest.raises(runner.RunDeadline):
            bridge.call("slow")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _skeleton(root):
    server = root / "src" / "opentradingdesk" / "server.py"
    server.parent.mkdir(parents=True)
    server.write_text("")
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(python.stat().st_mode | stat.S_IEXEC)


def test_runner_exits_nonzero_at_the_deadline(tmp_path, monkeypatch, capsys):
    runner = _runner()
    _skeleton(tmp_path)
    monkeypatch.setenv("OTD_PROJECT", "")  # run() sets it; undone afterwards
    monkeypatch.setattr(runner, "run_lab", lambda *a: runner._expire())
    assert runner.run(str(tmp_path), "clean", str(tmp_path / "out")) == 1
    assert "run exceeded" in capsys.readouterr().err


def test_error_text_never_carries_a_token(make_bridge):
    module, _ = make_bridge
    first, second = "S" * 40, "T" * 40

    class Bridge:
        def call(self, tool, **args):
            raise module.BridgeError(
                f"{'x' * 170} {args['delegation_token']} {first} {second}")

    class Api:
        def __init__(self):
            self.observed, self.replied = [], []

        def observe(self, kind, subject, detail):
            self.observed.append(detail)

        def reply(self, msg, kind, body):
            self.replied.append(body)
    load_plugin_files([PLUGIN])
    api = Api()
    desk = ROLES["otd_desk"]("desk", {}, api)
    desk.bridge = Bridge()
    desk.tokens = {"delegation-1": first, "delegation-2": second}
    desk._answer({"body": {"request_id": "r", "tool": "place_order", "args": {
        "delegation_token": "delegation-1"}}}, "order_result")
    text = json.dumps([api.observed, api.replied])
    assert "SSSS" not in text and "TTTT" not in text
    assert "delegation-" in text
    assert len(api.replied[0]["error"]) <= 200


_FAKE_SERVER = """
import json
from mcp.server.fastmcp import FastMCP
m = FastMCP("fake", log_level="ERROR")
CARDS = [{"provider_id": n} for n in "abc"]
VIEW = %s
@m.tool()
def find_providers(ticker: str) -> str: return json.dumps(CARDS)
@m.tool()
def issue_delegation(owner: str, max_position_pct: float, long_only: bool,
                     flat_by_minute: int, daily_loss_stop_pct: float,
                     ttl_minutes: int) -> str: return json.dumps("fake-token")
@m.tool()
def request_view(provider_id: str, ticker: str, horizon_minutes: int,
                 deadline_seconds: int, as_of_minute: int) -> str:
    return json.dumps(VIEW)
m.run()
"""

_FAKE_AGENT = """
import json
async def call(session, tool, **args):
    out = await session.call_tool(tool, args)
    return json.loads(out.content[0].text)
def decide(ticker, views):
    %s
"""


@pytest.mark.parametrize("view,decide", [
    ('{"unexpected": 1}', "return None"),
    ('{"status": "no_show"}', "raise ValueError('bad')"),
])
def test_a_workflow_error_still_leaves_a_failed_bundle(
        tmp_path, monkeypatch, view, decide):
    pytest.importorskip("mcp")
    project = tmp_path / "project"
    _skeleton(project)
    package = project / "src" / "opentradingdesk"
    (package / "__init__.py").write_text("")
    (package / "server.py").write_text(_FAKE_SERVER % view)
    (package / "pm_agent.py").write_text(_FAKE_AGENT % decide)
    monkeypatch.setenv("OTD_PROJECT", str(project))
    monkeypatch.setattr(sys, "path", list(sys.path))
    try:
        bundle, result = run_lab(os.path.join(HERE, CASES["clean"]),
                                 str(tmp_path / "out"))
    finally:
        sys.modules["mcp_bridge"].close_all()
        for name in [n for n in sys.modules
                     if n == "mcp_bridge" or n.startswith("opentradingdesk")]:
            del sys.modules[name]
    assert result.verdict != "passed"
    assert verify_bundle(bundle) == []
    with open(os.path.join(bundle, "events.jsonl")) as f:
        kinds = [json.loads(line)["kind"] for line in f]
    assert "pm_halted" in kinds
