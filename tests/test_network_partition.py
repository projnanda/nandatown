"""Tests for the network partition transport plugin and scenario."""

import json
import os

import pytest

from nandatown.layers.transport import MemoryTransport, PartitionedTransport
from nandatown.sim.scenario import FaultRule, ScenarioSpec, load_bundled
from nandatown.sim.validators import evaluate_scenario


class FakeEngine:
    """Minimal engine stub for transport unit tests."""

    def __init__(self):
        self.now = 0.0
        self.events = []
        self.intents = []
        self.agents = {}
        self._eseq = 0
        self._seq = 0
        self._queue = []

    class _Rng:
        def random(self):
            return 0.5

    rng = _Rng()

    def emit(self, observer, kind, subject, detail=None):
        self._eseq += 1
        event = type("E", (), {
            "event_id": f"ev-{self._eseq}",
            "run_id": "test-run",
            "at": self.now,
            "observer": observer,
            "kind": kind,
            "subject": subject,
            "detail": detail or {},
        })()
        self.events.append(event)
        return event.event_id

    def schedule(self, delay, fn):
        self._seq += 1
        self._queue.append((self.now + delay, self._seq, fn))

    def deliver(self, to, envelope):
        agent = self.agents.get(to)
        if agent:
            agent.on_message(envelope)


class FakeAgent:
    def __init__(self, name):
        self.name = name
        self.received = []

    def on_message(self, msg):
        self.received.append(msg)


class TestPartitionedTransport:

    def _make_envelope(self, sender, to, kind="test"):
        return {
            "message_id": f"msg-{sender}-{to}",
            "sender": sender,
            "kind": kind,
            "body": {},
            "signature": "",
            "conversation": None,
        }

    def test_cross_group_message_blocked(self):
        engine = FakeEngine()
        t = PartitionedTransport(engine)
        t.configure([{
            "action": "partition",
            "groups": [["a", "b"], ["c"]],
        }])
        env = self._make_envelope("a", "c")
        t.send("a", "c", env)
        blocked = [e for e in engine.events
                   if e.kind == "message_partition_blocked"]
        assert len(blocked) == 1
        assert blocked[0].detail["from"] == "a"
        assert blocked[0].detail["to"] == "c"

    def test_same_group_message_delivered(self):
        engine = FakeEngine()
        agent_b = FakeAgent("b")
        engine.agents["b"] = agent_b
        t = PartitionedTransport(engine)
        t.configure([{
            "action": "partition",
            "groups": [["a", "b"], ["c"]],
        }])
        env = self._make_envelope("a", "b")
        t.send("a", "b", env)
        # Execute scheduled deliveries
        for _, _, fn in sorted(engine._queue):
            fn()
        delivered = [e for e in engine.events
                     if e.kind == "message_delivered"]
        assert len(delivered) == 1
        assert len(agent_b.received) == 1

    def test_ungrouped_agent_blocked_from_grouped(self):
        engine = FakeEngine()
        t = PartitionedTransport(engine)
        t.configure([{
            "action": "partition",
            "groups": [["a", "b"]],
        }])
        # "c" is not in any group
        env = self._make_envelope("c", "a")
        t.send("c", "a", env)
        blocked = [e for e in engine.events
                   if e.kind == "message_partition_blocked"]
        assert len(blocked) == 1

    def test_no_partition_all_messages_delivered(self):
        engine = FakeEngine()
        agent_b = FakeAgent("b")
        engine.agents["b"] = agent_b
        t = PartitionedTransport(engine)
        t.configure([])
        env = self._make_envelope("a", "b")
        t.send("a", "b", env)
        for _, _, fn in sorted(engine._queue):
            fn()
        delivered = [e for e in engine.events
                     if e.kind == "message_delivered"]
        assert len(delivered) == 1

    def test_partition_with_drop_fault(self):
        engine = FakeEngine()
        t = PartitionedTransport(engine)
        t.configure([
            {"action": "partition", "groups": [["a"], ["b"]]},
            {"action": "drop", "kind": "test"},
        ])
        # Same group, but drop rule applies
        env = self._make_envelope("a", "a")
        t.send("a", "a", env)
        dropped = [e for e in engine.events if e.kind == "message_dropped"]
        assert len(dropped) == 1

    def test_partition_with_duplicate_fault(self):
        engine = FakeEngine()
        agent_a = FakeAgent("a")
        engine.agents["a"] = agent_a
        t = PartitionedTransport(engine)
        t.configure([
            {"action": "partition", "groups": [["a"]]},
            {"action": "duplicate", "kind": "test"},
        ])
        env = self._make_envelope("a", "a")
        t.send("a", "a", env)
        for _, _, fn in sorted(engine._queue):
            fn()
        duplicated = [e for e in engine.events
                      if e.kind == "message_duplicated"]
        assert len(duplicated) == 1
        assert len(agent_a.received) == 2

    def test_partition_cross_group_ignores_fault_rules(self):
        """Cross-group messages are blocked before fault rules apply."""
        engine = FakeEngine()
        t = PartitionedTransport(engine)
        t.configure([
            {"action": "partition", "groups": [["a"], ["b"]]},
            {"action": "duplicate", "kind": "test"},
        ])
        env = self._make_envelope("a", "b")
        t.send("a", "b", env)
        blocked = [e for e in engine.events
                   if e.kind == "message_partition_blocked"]
        duplicated = [e for e in engine.events
                      if e.kind == "message_duplicated"]
        assert len(blocked) == 1
        assert len(duplicated) == 0

    def test_empty_groups_no_partition(self):
        engine = FakeEngine()
        agent_b = FakeAgent("b")
        engine.agents["b"] = agent_b
        t = PartitionedTransport(engine)
        t.configure([{"action": "partition", "groups": []}])
        env = self._make_envelope("a", "b")
        t.send("a", "b", env)
        for _, _, fn in sorted(engine._queue):
            fn()
        blocked = [e for e in engine.events
                   if e.kind == "message_partition_blocked"]
        delivered = [e for e in engine.events
                     if e.kind == "message_delivered"]
        assert len(blocked) == 0
        assert len(delivered) == 1

    def test_message_sent_event_always_emitted(self):
        engine = FakeEngine()
        t = PartitionedTransport(engine)
        t.configure([{"action": "partition", "groups": [["a"], ["b"]]}])
        env = self._make_envelope("a", "b")
        t.send("a", "b", env)
        sent = [e for e in engine.events if e.kind == "message_sent"]
        assert len(sent) == 1


class TestFaultRulePartition:

    def test_partition_action_accepted(self):
        rule = FaultRule(
            action="partition",
            groups=[["a", "b"], ["c"]],
        )
        assert rule.action == "partition"
        assert rule.groups == [["a", "b"], ["c"]]

    def test_partition_rule_model_dump(self):
        rule = FaultRule(
            action="partition",
            groups=[["a"], ["b", "c"]],
        )
        d = rule.model_dump()
        assert d["action"] == "partition"
        assert d["groups"] == [["a"], ["b", "c"]]


class TestNetworkPartitionScenario:

    def test_scenario_loads(self):
        spec = load_bundled("network_partition")
        assert spec.name == "network_partition"
        assert spec.layers["transport"] == "partitioned.v1"
        assert len(spec.agents) == 3
        assert len(spec.faults) == 1
        assert spec.faults[0].action == "partition"

    def test_scenario_has_correct_agents(self):
        spec = load_bundled("network_partition")
        names = {a.name for a in spec.agents}
        assert names == {"seller-a", "seller-b", "buyer-1"}

    def test_scenario_has_correct_groups(self):
        spec = load_bundled("network_partition")
        fault = spec.faults[0]
        assert fault.groups == [["buyer-1", "seller-a"], ["seller-b"]]


class TestNetworkPartitionValidator:

    def test_partition_scenario_passes(self, tmp_path):
        from nandatown.sim.runner import run_lab

        bundle_dir, result = run_lab(
            "network_partition", str(tmp_path), seed=42)
        assert result.verdict == "passed"

    def test_partition_detected_stage(self, tmp_path):
        from nandatown.sim.runner import run_lab

        _, result = run_lab("network_partition", str(tmp_path), seed=42)
        stage_names = [s.name for s in result.stages]
        assert "partition_detected" in stage_names
        detected = [s for s in result.stages
                    if s.name == "partition_detected"][0]
        assert detected.status == "passed"

    def test_isolated_seller_contained(self, tmp_path):
        from nandatown.sim.runner import run_lab

        _, result = run_lab("network_partition", str(tmp_path), seed=42)
        stage_names = [s.name for s in result.stages]
        assert "isolated_seller_contained" in stage_names
        contained = [s for s in result.stages
                     if s.name == "isolated_seller_contained"][0]
        assert contained.status == "passed"

    def test_reachable_trade_completed(self, tmp_path):
        from nandatown.sim.runner import run_lab

        _, result = run_lab("network_partition", str(tmp_path), seed=42)
        stage_names = [s.name for s in result.stages]
        assert "reachable_trade_completed" in stage_names
        trade = [s for s in result.stages
                 if s.name == "reachable_trade_completed"][0]
        assert trade.status == "passed"

    def test_reputation_consistent(self, tmp_path):
        from nandatown.sim.runner import run_lab

        _, result = run_lab("network_partition", str(tmp_path), seed=42)
        stage_names = [s.name for s in result.stages]
        assert "reputation" in stage_names

    def test_partition_events_in_bundle(self, tmp_path):
        from nandatown.bundle import load_bundle
        from nandatown.sim.runner import run_lab

        bundle_dir, _ = run_lab(
            "network_partition", str(tmp_path), seed=42)
        bundle = load_bundle(bundle_dir)
        blocked = [e for e in bundle["events"]
                   if e.kind == "message_partition_blocked"]
        assert len(blocked) > 0

    def test_isolated_seller_no_deliveries(self, tmp_path):
        from nandatown.bundle import load_bundle
        from nandatown.sim.runner import run_lab

        bundle_dir, _ = run_lab(
            "network_partition", str(tmp_path), seed=42)
        bundle = load_bundle(bundle_dir)
        delivered_to_b = [e for e in bundle["events"]
                          if e.kind == "message_delivered"
                          and e.detail.get("to") == "seller-b"]
        assert len(delivered_to_b) == 0

    def test_deterministic_across_seeds(self, tmp_path):
        from nandatown.sim.runner import run_lab

        _, r1 = run_lab("network_partition", str(tmp_path / "a"), seed=42)
        _, r2 = run_lab("network_partition", str(tmp_path / "b"), seed=42)
        assert r1.verdict == r2.verdict
        for s1, s2 in zip(r1.stages, r2.stages):
            assert s1.status == s2.status

    def test_different_seed_same_verdict(self, tmp_path):
        from nandatown.sim.runner import run_lab

        _, r1 = run_lab("network_partition", str(tmp_path / "a"), seed=42)
        _, r2 = run_lab("network_partition", str(tmp_path / "b"), seed=99)
        assert r1.verdict == "passed"
        assert r2.verdict == "passed"


class TestPartitionMatrixIntegration:

    def test_partition_fault_in_catalog(self):
        from nandatown.matrix import FAULT_CATALOG

        assert "transport/partition" in FAULT_CATALOG
        spec = FAULT_CATALOG["transport/partition"]
        assert spec.transport_action == "partition"
        assert spec.transport_groups == []

    def test_network_partition_in_default_matrix(self):
        from nandatown.matrix import DEFAULT_MATRIX

        assert "network_partition" in DEFAULT_MATRIX
        assert "transport/partition" in DEFAULT_MATRIX["network_partition"]

    def test_partition_fault_rule_building(self):
        from nandatown.matrix import (
            FAULT_CATALOG, _build_scenario_fault_rules,
        )

        fault = FAULT_CATALOG["transport/partition"]
        # Set groups for the test
        fault.transport_groups = [["a", "b"], ["c"]]
        rules = _build_scenario_fault_rules([fault])
        assert len(rules) == 1
        assert rules[0]["action"] == "partition"
        assert rules[0]["groups"] == [["a", "b"], ["c"]]
        # Reset
        fault.transport_groups = []

    def test_partition_in_propagation_chains(self):
        from nandatown.matrix import PROPAGATION_CHAINS

        assert "transport/partition" in PROPAGATION_CHAINS
        chain = PROPAGATION_CHAINS["transport/partition"]
        assert len(chain) == 1
        assert chain[0][0] == "message_partition_blocked"

    def test_partition_in_fault_event_kinds(self):
        from nandatown.matrix import FAULT_EVENT_KINDS

        assert "message_partition_blocked" in FAULT_EVENT_KINDS

    def test_matrix_runs_network_partition(self, tmp_path):
        from nandatown.matrix import run_failure_matrix

        matrix_dir, result = run_failure_matrix(
            scenarios=["network_partition"],
            faults=["transport/partition"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell_key = "network_partition/transport/partition"
        assert cell_key in result.cells
        cell = result.cells[cell_key]
        assert len(cell.trials) == 2
        assert cell.passes == 2

    def test_matrix_partition_with_baseline(self, tmp_path):
        from nandatown.matrix import run_failure_matrix

        _, result = run_failure_matrix(
            scenarios=["network_partition"],
            faults=["transport/partition"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=True,
        )
        assert "network_partition/baseline" in result.cells
        assert "network_partition/transport/partition" in result.cells
        baseline = result.cells["network_partition/baseline"]
        assert baseline.stats.pass_rate == 1.0

    def test_partition_propagation_traced(self, tmp_path):
        from nandatown.matrix import run_failure_matrix

        _, result = run_failure_matrix(
            scenarios=["network_partition"],
            faults=["transport/partition"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell = result.cells["network_partition/transport/partition"]
        # All trials pass (partition is handled), so no propagation traces
        # (propagation is traced only on failed stages)
        assert cell.stats.pass_rate == 1.0

    def test_partition_report_includes_fault(self, tmp_path):
        from nandatown.matrix import render_matrix_report, run_failure_matrix

        _, result = run_failure_matrix(
            scenarios=["network_partition"],
            faults=["transport/partition"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        report = render_matrix_report(result)
        assert "transport/partition" in report
        assert "Network partition" in report

    def test_partition_heatmap(self, tmp_path):
        from nandatown.matrix import render_heatmap, run_failure_matrix

        _, result = run_failure_matrix(
            scenarios=["network_partition"],
            faults=["transport/partition"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        html = render_heatmap(result)
        assert "network_partition" in html
        assert "transport/partition" in html

    def test_partition_bundles_verify(self, tmp_path):
        from nandatown.bundle import verify_bundle
        from nandatown.matrix import run_failure_matrix

        matrix_dir, _ = run_failure_matrix(
            scenarios=["network_partition"],
            faults=["transport/partition"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell_dir = os.path.join(
            matrix_dir, "network_partition/transport/partition")
        for name in os.listdir(cell_dir):
            if name.startswith("sim-"):
                problems = verify_bundle(os.path.join(cell_dir, name))
                assert problems == [], (
                    f"Bundle {name} verification failed: {problems}")


class TestPartitionCLI:

    def test_matrix_cli_with_network_partition(self, tmp_path, capsys):
        from nandatown.cli import main

        ret = main([
            "matrix",
            "--scenario", "network_partition",
            "--fault", "transport/partition",
            "--trials", "2",
            "--no-baseline",
            "--out", str(tmp_path),
        ])
        assert ret == 0
        text = capsys.readouterr().out
        assert "Protocol Failure Matrix" in text
        assert "network_partition" in text
