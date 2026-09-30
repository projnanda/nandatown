"""Fault declaration contract from legacy PR #8 (@mariagorskikh).

Legacy PRs #10 and #11 remain future latency and topology profiles. These
tests harden the numeric declarations the current transport consumes and
the partition declaration: a timed cut between named groups of agents that
must list every agent once, heal before max_time, and be the only fault in
its scenario.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nandatown.sim.scenario import (FaultRule, ScenarioSpec, bundled_scenarios,
                                    load_bundled, load_scenario_text)


@pytest.mark.parametrize(("field", "action", "value"), [
    ("nth", "drop_rate", 0),
    ("nth", "drop_rate", -1),
    ("nth", "drop_rate", True),
    ("nth", "drop_rate", 1.0),
    ("nth", "drop_rate", 1.5),
    ("nth", "drop_rate", "1"),
    ("delay", "drop", -0.1),
    ("delay", "drop", True),
    ("delay", "drop", "0.5"),
    ("delay", "drop", float("nan")),
    ("delay", "drop", float("inf")),
    ("delay", "drop", float("-inf")),
    ("rate", "delay", -0.1),
    ("rate", "delay", 1.1),
    ("rate", "delay", True),
    ("rate", "delay", "0.5"),
    ("rate", "delay", float("nan")),
    ("rate", "delay", float("inf")),
    ("rate", "delay", float("-inf")),
])
def test_fault_numbers_reject_malformed_values_even_when_unused(
        field, action, value):
    with pytest.raises(ValidationError):
        FaultRule.model_validate({"action": action, field: value})


def test_integer_yaml_values_remain_valid_for_float_fault_fields():
    spec = load_scenario_text("""
name: integer-fault-values
agents: []
faults:
  - {action: delay, kind: ping, nth: 1, delay: 2, rate: 1}
""")

    rule = spec.faults[0]
    assert rule.nth == 1 and type(rule.nth) is int
    assert rule.delay == 2.0 and type(rule.delay) is float
    assert rule.rate == 1.0 and type(rule.rate) is float


def test_shipped_scenario_schema_matches_generated_numeric_contract():
    path = Path(__file__).parents[1] / "schemas" / "scenario.schema.json"
    shipped = json.loads(path.read_text())
    generated = ScenarioSpec.model_json_schema()
    generated["$id"] = "https://nandatown.local/schemas/scenario.schema.json"

    assert shipped == generated
    fault = shipped["$defs"]["FaultRule"]["properties"]
    assert fault["nth"]["minimum"] == 1
    assert fault["delay"]["minimum"] == 0
    assert fault["rate"]["minimum"] == 0
    assert fault["rate"]["maximum"] == 1


PARTITION_SCENARIO = """
name: partition-declaration
agents:
  - {name: proposer, role: proposer, config: {value: v42, retry_after: 1.5}}
  - {name: acceptor-1, role: acceptor, config: {}}
  - {name: acceptor-2, role: acceptor, config: {}}
  - {name: acceptor-3, role: acceptor, config: {}}
faults:
  - action: partition
    groups: [[proposer, acceptor-1], [acceptor-2, acceptor-3]]
    start: 0
    heal: 10
max_time: 60
"""


def test_partition_declaration_loads_with_float_window():
    spec = load_scenario_text(PARTITION_SCENARIO)

    rule = spec.faults[0]
    assert rule.action == "partition"
    assert rule.groups == (("proposer", "acceptor-1"), ("acceptor-2", "acceptor-3"))
    assert rule.start == 0.0 and type(rule.start) is float
    assert rule.heal == 10.0 and type(rule.heal) is float


OMIT = object()


def partition_rule(**overrides):
    rule = {"action": "partition",
            "groups": [["proposer", "acceptor-1"], ["acceptor-2", "acceptor-3"]],
            "start": 0.0, "heal": 10.0}
    rule.update(overrides)
    return {key: value for key, value in rule.items() if value is not OMIT}


@pytest.mark.parametrize("rule", [
    pytest.param(partition_rule(groups=OMIT), id="partition-without-groups"),
    pytest.param(partition_rule(start=OMIT), id="partition-without-start"),
    pytest.param(partition_rule(heal=OMIT), id="partition-without-heal"),
    pytest.param(partition_rule(groups=None), id="partition-null-groups"),
    pytest.param(partition_rule(kind="prepare"), id="partition-with-kind"),
    # nth: 1 is the default, and every recorded partition carries it,
    # so only a non-default value can be rejected.
    pytest.param(partition_rule(nth=2), id="partition-with-nth"),
    pytest.param(partition_rule(delay=0.5), id="partition-with-delay"),
    pytest.param(partition_rule(rate=0.5), id="partition-with-rate"),
    pytest.param(partition_rule(start=10.0), id="start-equals-heal"),
    pytest.param(partition_rule(start=12.0), id="start-after-heal"),
    pytest.param(partition_rule(groups=[["proposer", "acceptor-1",
                                         "acceptor-2", "acceptor-3"]]),
                 id="single-group"),
    pytest.param(partition_rule(groups=[["proposer", "acceptor-1"], []]),
                 id="empty-group"),
    pytest.param(partition_rule(groups=[["proposer", "acceptor-1"],
                                        ["acceptor-1", "acceptor-2"]]),
                 id="agent-in-two-groups"),
    pytest.param(partition_rule(groups=[["proposer", "proposer"], ["acceptor-1"]]),
                 id="agent-twice-in-one-group"),
    pytest.param({"action": "drop", "kind": "prepare_ack", "groups": [["a"], ["b"]]},
                 id="drop-with-groups"),
    pytest.param({"action": "delay", "delay": 1.0, "start": 0.0},
                 id="delay-with-start"),
    pytest.param({"action": "duplicate", "heal": 5.0}, id="duplicate-with-heal"),
    pytest.param({"action": "drop_rate", "rate": 0.1, "groups": [["a"], ["b"]]},
                 id="drop-rate-with-groups"),
])
def test_partition_declarations_fail_closed(rule):
    with pytest.raises(ValidationError):
        FaultRule.model_validate(rule)


CONSENSUS_AGENTS = [
    {"name": "proposer", "role": "proposer",
     "config": {"value": "v42", "retry_after": 1.5}},
    {"name": "acceptor-1", "role": "acceptor", "config": {}},
    {"name": "acceptor-2", "role": "acceptor", "config": {}},
    {"name": "acceptor-3", "role": "acceptor", "config": {}},
]


def partition_scenario(faults=None, max_time=60):
    return {"name": "partition-scenario", "agents": CONSENSUS_AGENTS,
            "faults": faults if faults is not None else [partition_rule()],
            "max_time": max_time}


def test_partition_scenario_helper_builds_a_valid_scenario():
    spec = ScenarioSpec.model_validate(partition_scenario())
    assert spec.faults[0].action == "partition"


@pytest.mark.parametrize("scenario", [
    pytest.param(partition_scenario([partition_rule(
        groups=[["proposer", "acceptor-1"],
                ["acceptor-2", "acceptor-3", "acceptor-9"]])]),
        id="unknown-agent-in-groups"),
    pytest.param(partition_scenario([partition_rule(
        groups=[["proposer", "acceptor-1"], ["acceptor-2"]])]),
        id="agent-missing-from-groups"),
    pytest.param(partition_scenario([partition_rule(heal=60.0)]),
                 id="heal-at-max-time"),
    pytest.param(partition_scenario([partition_rule(heal=75.0)]),
                 id="heal-after-max-time"),
    pytest.param(partition_scenario([partition_rule(),
                                     partition_rule(start=20.0, heal=30.0)]),
                 id="two-partitions"),
    pytest.param(partition_scenario([partition_rule(),
                                     {"action": "drop", "kind": "prepare_ack", "nth": 1}]),
                 id="partition-plus-drop"),
])
def test_partition_scenarios_fail_closed(scenario):
    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate(scenario)


@pytest.mark.parametrize("name", sorted(bundled_scenarios()))
def test_bundled_scenarios_survive_their_own_round_trip(name):
    """Town revalidates its own records: runner.py rebuilds the public spec
    from a dump after every run, and load_bundle reparses profile.json on
    report and verify. A valid scenario must survive its own dump."""
    spec = load_bundled(name)
    assert ScenarioSpec.model_validate(spec.model_dump()) == spec
    assert ScenarioSpec.model_validate_json(spec.model_dump_json()) == spec


def test_partition_scenario_survives_its_own_round_trip():
    spec = load_scenario_text(PARTITION_SCENARIO)
    assert ScenarioSpec.model_validate(spec.model_dump()) == spec
    assert ScenarioSpec.model_validate_json(spec.model_dump_json()) == spec
