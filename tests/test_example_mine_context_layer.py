"""The mine-context-layer example must never pass without real evidence.

The project is hosted separately and is not installed in CI, so these tests
use only the Town side: no project means no pass, the recorded bundles still
verify, and damaging the evidence a stage relies on stops that stage passing.
"""

import copy
import json
import os
import subprocess
import sys

import pytest

from nandatown.bundle import verify_bundle
from nandatown.records import TownEvent
from nandatown.sim.runner import load_plugin_files, run_lab
from nandatown.sim.scenario import load_scenario_file
from nandatown.sim.validators import evaluate_scenario

# Where the example, its scenarios and its saved evidence live
HERE = os.path.join(os.path.dirname(__file__), "..", "examples", "integrations",
                    "mine-context-layer")
EVIDENCE = os.path.join(HERE, "evidence")
PLUGIN = os.path.join(HERE, "mcl_plugin.py")
CASES = {"workflow": "mcl_workflow.yaml", "cmms_down": "mcl_cmms_down.yaml",
         "agents": "mcl_agents.yaml"}
# Cases that run without a model, so their evidence is fully deterministic
SCRIPTED = ("workflow", "cmms_down")


def _load(case):
    # The recorded events of one saved run
    with open(os.path.join(EVIDENCE, case, "events.jsonl")) as f:
        return [json.loads(line) for line in f]


def _verdict(case, events):
    # Judge a list of events with the scenario's own validator
    load_plugin_files([PLUGIN])
    spec = load_scenario_file(os.path.join(HERE, CASES[case]))
    parsed = [TownEvent.model_validate(e) for e in events]
    result = evaluate_scenario(spec, "run", parsed)
    return result.verdict, {s.name: s.status for s in result.stages}


def _tool_result(events, tool, **args):
    # The adapter's record of one real MCP call, picked by tool and arguments
    for e in events:
        if (e["kind"] == "layer_tool_result" and e["subject"] == tool
                and all(e["detail"]["args"].get(k) == v for k, v in args.items())):
            return e
    raise AssertionError((tool, args))


def _only(events, kind):
    [e] = [e for e in events if e["kind"] == kind]
    return e


@pytest.mark.parametrize("name", CASES.values())
def test_missing_project_is_incomplete_not_passed(tmp_path, monkeypatch, name):
    monkeypatch.setenv("MCL_PROJECT", str(tmp_path / "absent"))
    _, result = run_lab(os.path.join(HERE, name), str(tmp_path))
    judged = [s for s in result.stages
              if s.name not in ("ledger_conserved", "privacy")]
    assert result.verdict != "passed"
    assert judged and all(s.status == "not_enough_evidence" for s in judged)


def test_runner_refuses_a_missing_project(tmp_path):
    done = subprocess.run(
        [sys.executable, os.path.join(HERE, "run.py"), "--project",
         str(tmp_path / "absent"), "--case", "workflow", "--out", str(tmp_path)],
        capture_output=True, text=True)
    assert done.returncode == 2
    assert "setup incomplete" in done.stderr


@pytest.mark.parametrize("case", CASES)
def test_recorded_evidence_verifies(case):
    load_plugin_files([PLUGIN])
    assert verify_bundle(os.path.join(EVIDENCE, case)) == []


@pytest.mark.parametrize("case", CASES)
def test_recorded_evidence_passes_every_stage(case):
    verdict, stages = _verdict(case, _load(case))
    assert verdict == "passed", stages


def test_a_wrong_loader_fails_the_action_stage():
    events = copy.deepcopy(_load("workflow"))
    act = _tool_result(events, "submit_proposal", kind="action")
    act["detail"]["args"]["recommendation"]["equipment_id"] = "LH-02"
    _, stages = _verdict("workflow", events)
    assert stages["action_matches_key"] == "failed"


def test_an_allowed_publish_by_a_function_agent_fails_the_boundary():
    events = copy.deepcopy(_load("workflow"))
    probe = _tool_result(events, "publish_event", type="equipment_status")
    probe["detail"]["result"] = {"event_id": "EV-0099", "resolved_ids": ["LH-07"]}
    _, stages = _verdict("workflow", events)
    assert stages["permission_boundary"] != "passed"


def test_a_served_intruder_fails_the_boundary():
    events = [e for e in copy.deepcopy(_load("workflow"))
              if not (e["kind"] == "layer_refused" and e["subject"] == "visitor")]
    _, stages = _verdict("workflow", events)
    assert stages["permission_boundary"] != "passed"


def test_a_record_written_by_a_script_role_does_not_count():
    events = copy.deepcopy(_load("workflow"))
    act = _tool_result(events, "submit_proposal", kind="action")
    act["observer"] = "planning"
    _, stages = _verdict("workflow", events)
    assert stages["action_matches_key"] != "passed"


def test_a_decision_by_someone_else_fails_the_human_stage():
    events = copy.deepcopy(_load("workflow"))
    _only(events, "layer_decision_recorded")["detail"]["by"] = "planning"
    _, stages = _verdict("workflow", events)
    assert stages["human_decision"] != "passed"


def test_a_modified_project_fails_the_connection_stage():
    events = copy.deepcopy(_load("workflow"))
    _only(events, "layer_connected")["detail"]["project_dirty"] = True
    _, stages = _verdict("workflow", events)
    assert stages["connection"] == "failed"


def test_a_confident_estimate_without_cmms_fails_the_degraded_stage():
    events = copy.deepcopy(_load("cmms_down"))
    assess = _tool_result(events, "submit_proposal", kind="assessment")
    assess["detail"]["args"]["confidence"] = 0.9
    _, stages = _verdict("cmms_down", events)
    assert stages["degraded_source"] == "failed"


def test_a_real_agent_using_other_tools_fails_the_agents_stage():
    events = copy.deepcopy(_load("agents"))
    run = next(e for e in events if e["kind"] == "layer_agent_run")
    run["detail"]["run"]["tool_calls"].append("Bash")
    _, stages = _verdict("agents", events)
    assert stages["real_agents"] == "failed"
