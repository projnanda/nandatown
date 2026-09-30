"""Partition fault: the Lab transport drops every envelope sent between
groups during [start, heal) and records when the cut starts and heals."""

import json
from pathlib import Path

import pytest

from nandatown.layers.transport import MemoryTransport
from nandatown.sim.runner import LabError, run_lab

PARTITION_YAML = """
name: partition-transport
validator: consensus
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

GROUP = {"proposer": 0, "acceptor-1": 0, "acceptor-2": 1, "acceptor-3": 1}


def run_partition(tmp_path):
    path = tmp_path / "partition.yaml"
    path.write_text(PARTITION_YAML)
    bundle_dir, _ = run_lab(str(path), str(tmp_path / "runs"))
    lines = (Path(bundle_dir) / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_partition_window_is_recorded_by_the_town(tmp_path):
    events = run_partition(tmp_path)
    started = [e for e in events if e["kind"] == "partition_started"]
    healed = [e for e in events if e["kind"] == "partition_healed"]
    assert len(started) == 1
    assert started[0]["at"] == 0.0 and started[0]["observer"] == "town"
    assert len(healed) == 1
    assert healed[0]["at"] == 10.0 and healed[0]["observer"] == "town"


def test_partition_drops_only_cross_group_sends_inside_the_window(tmp_path):
    events = run_partition(tmp_path)
    drops = [e for e in events if e["kind"] == "message_dropped"]
    assert drops, "the partition dropped nothing"
    for e in drops:
        assert e["detail"]["fault"] == "partition"
        assert GROUP[e["detail"]["from"]] != GROUP[e["detail"]["to"]]
        assert 0.0 <= e["at"] < 10.0
    early_sends = [e for e in events if e["kind"] == "message_sent"
                   and e["observer"] in ("acceptor-2", "acceptor-3")
                   and e["at"] < 10.0]
    assert early_sends == [], "a cut-off acceptor sent something during the cut"
    assert len(drops) == 14  # 2 prepares at the start + 6 retries x 2


def test_consensus_completes_after_the_heal(tmp_path):
    events = run_partition(tmp_path)
    commits = [e for e in events if e["kind"] == "consensus_committed"]
    assert len(commits) == 1 and commits[0]["at"] > 10.0
    committed = {e["subject"] for e in events if e["kind"] == "value_committed"}
    assert committed == {"acceptor-1", "acceptor-2", "acceptor-3"}


DEFECT_YAML = PARTITION_YAML.replace(
    "config: {value: v42, retry_after: 1.5}",
    "config: {value: v42, retry_after: 1.5, defect: shrink_quorum_on_timeout}")


def run_text(tmp_path, text):
    path = tmp_path / "scenario.yaml"
    path.write_text(text)
    bundle_dir, _ = run_lab(str(path), str(tmp_path / "runs"))
    lines = (Path(bundle_dir) / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_quorum_shrinking_proposer_commits_on_one_ack_during_the_cut(tmp_path):
    assert DEFECT_YAML != PARTITION_YAML  # the defect really was added
    events = run_text(tmp_path, DEFECT_YAML)
    commits = [e for e in events if e["kind"] == "consensus_committed"]
    assert len(commits) == 1
    assert commits[0]["at"] == 2.0
    assert commits[0]["detail"] == {"acks": ["acceptor-1"], "quorum": 1}
    committed = {e["subject"] for e in events if e["kind"] == "value_committed"}
    assert committed == {"acceptor-1"}  # the other commits are lost in the cut


def test_unknown_proposer_defect_fails_loudly(tmp_path):
    typo = PARTITION_YAML.replace(
        "config: {value: v42, retry_after: 1.5}",
        "config: {value: v42, retry_after: 1.5, defect: shrink_quorm}")
    with pytest.raises(LabError, match="unknown proposer defect"):
        run_text(tmp_path, typo)


JUDGED_YAML = PARTITION_YAML.replace("validator: consensus\n",
                                     "validator: consensus_partition\n")


def judge_text(tmp_path, text):
    path = tmp_path / "scenario.yaml"
    path.write_text(text)
    _, result = run_lab(str(path), str(tmp_path / "runs"))
    return {s.name: s.status for s in result.stages}


def test_a_leaky_partition_fools_the_old_checks_but_not_the_new_ones(
        tmp_path, monkeypatch):
    assert JUDGED_YAML != PARTITION_YAML
    monkeypatch.setattr(MemoryTransport, "_partitioned",
                        lambda self, sender, to: False)
    stages = judge_text(tmp_path, JUDGED_YAML)
    assert stages["quorum_commit"] == "passed"
    assert stages["agreement"] == "passed"
    assert stages["partition_enforced"] == "failed"
    assert stages["progress_after_heal"] == "failed"
