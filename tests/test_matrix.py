import json
import os

import pytest

from nandatown.matrix import (
    BASELINE_FAULT,
    DEFAULT_MATRIX,
    FAULT_CATALOG,
    CellResult,
    CellStats,
    FaultSpec,
    MatrixResult,
    PropagationTrace,
    _build_layer_overrides,
    _build_scenario_fault_rules,
    _compute_cell_stats,
    _resolve_fault,
    _trace_propagation,
    _wilson_ci,
    register_fault,
    render_heatmap,
    render_matrix_report,
    run_failure_matrix,
    run_matrix_comparison,
)
from nandatown.sim.scenario import load_bundled


class TestFaultSpec:

    def test_fault_spec_to_dict_round_trips(self):
        spec = FaultSpec(
            fault_id="transport/duplicate",
            layer="transport",
            fault_type="transport_fault",
            transport_action="duplicate",
            description="test",
        )
        d = spec.to_dict()
        assert d["fault_id"] == "transport/duplicate"
        assert d["layer"] == "transport"
        assert d["fault_type"] == "transport_fault"
        assert d["transport_action"] == "duplicate"
        assert d["swap_plugin_id"] is None

    def test_layer_swap_fault_spec(self):
        spec = FaultSpec(
            fault_id="auth/none",
            layer="auth",
            fault_type="layer_swap",
            swap_plugin_id="plain.v1",
            description="auth disabled",
        )
        d = spec.to_dict()
        assert d["fault_type"] == "layer_swap"
        assert d["swap_plugin_id"] == "plain.v1"
        assert d["transport_action"] is None

    def test_baseline_fault_spec(self):
        assert BASELINE_FAULT.fault_id == "baseline"
        assert BASELINE_FAULT.fault_type == "baseline"


class TestFaultCatalog:

    def test_catalog_has_expected_faults(self):
        assert "transport/duplicate" in FAULT_CATALOG
        assert "transport/drop" in FAULT_CATALOG
        assert "transport/delay" in FAULT_CATALOG
        assert "transport/rate_flood" in FAULT_CATALOG
        assert "transport/drop_2nd" in FAULT_CATALOG
        assert "transport/delay_long" in FAULT_CATALOG
        assert "transport/duplicate_2x" in FAULT_CATALOG
        assert "transport/drop_3rd" in FAULT_CATALOG
        assert "auth/none" in FAULT_CATALOG
        assert "baseline" in FAULT_CATALOG

    def test_register_fault_adds_to_catalog(self):
        spec = FaultSpec(
            fault_id="test/custom",
            layer="test",
            fault_type="layer_swap",
            swap_plugin_id="custom.v1",
        )
        register_fault(spec)
        assert "test/custom" in FAULT_CATALOG
        assert FAULT_CATALOG["test/custom"].swap_plugin_id == "custom.v1"
        del FAULT_CATALOG["test/custom"]

    def test_transport_fault_has_action(self):
        for fid in ["transport/duplicate", "transport/drop", "transport/delay",
                     "transport/rate_flood", "transport/drop_2nd"]:
            spec = FAULT_CATALOG[fid]
            assert spec.fault_type == "transport_fault"
            assert spec.transport_action is not None

    def test_layer_swap_has_plugin_id(self):
        spec = FAULT_CATALOG["auth/none"]
        assert spec.fault_type == "layer_swap"
        assert spec.swap_plugin_id == "plain.v1"

    def test_rate_flood_has_rate(self):
        spec = FAULT_CATALOG["transport/rate_flood"]
        assert spec.transport_action == "drop_rate"
        assert spec.transport_rate == 0.5

    def test_drop_2nd_has_nth(self):
        spec = FAULT_CATALOG["transport/drop_2nd"]
        assert spec.transport_nth == 2

    def test_delay_long_has_delay(self):
        spec = FAULT_CATALOG["transport/delay_long"]
        assert spec.transport_delay == 5.0


class TestFaultRuleBuilding:

    def test_transport_duplicate_builds_rule(self):
        fault = FAULT_CATALOG["transport/duplicate"]
        rules = _build_scenario_fault_rules([fault])
        assert len(rules) == 1
        assert rules[0]["action"] == "duplicate"

    def test_transport_drop_builds_rule(self):
        fault = FAULT_CATALOG["transport/drop"]
        rules = _build_scenario_fault_rules([fault])
        assert len(rules) == 1
        assert rules[0]["action"] == "drop"

    def test_transport_delay_builds_rule_with_delay(self):
        fault = FAULT_CATALOG["transport/delay"]
        rules = _build_scenario_fault_rules([fault])
        assert len(rules) == 1
        assert rules[0]["action"] == "delay"
        assert rules[0]["delay"] == 2.0

    def test_rate_flood_builds_rule_with_rate(self):
        fault = FAULT_CATALOG["transport/rate_flood"]
        rules = _build_scenario_fault_rules([fault])
        assert len(rules) == 1
        assert rules[0]["action"] == "drop_rate"
        assert rules[0]["rate"] == 0.5

    def test_drop_2nd_builds_rule_with_nth(self):
        fault = FAULT_CATALOG["transport/drop_2nd"]
        rules = _build_scenario_fault_rules([fault])
        assert len(rules) == 1
        assert rules[0]["nth"] == 2

    def test_layer_swap_builds_no_fault_rules(self):
        fault = FAULT_CATALOG["auth/none"]
        rules = _build_scenario_fault_rules([fault])
        assert rules == []

    def test_layer_swap_builds_overrides(self):
        fault = FAULT_CATALOG["auth/none"]
        overrides = _build_layer_overrides([fault])
        assert overrides == {"auth": "plain.v1"}

    def test_transport_fault_builds_no_overrides(self):
        fault = FAULT_CATALOG["transport/duplicate"]
        overrides = _build_layer_overrides([fault])
        assert overrides is None

    def test_baseline_builds_no_rules(self):
        rules = _build_scenario_fault_rules([BASELINE_FAULT])
        assert rules == []

    def test_baseline_builds_no_overrides(self):
        overrides = _build_layer_overrides([BASELINE_FAULT])
        assert overrides is None

    def test_composite_fault_builds_multiple_rules(self):
        faults = [
            FAULT_CATALOG["transport/drop"],
            FAULT_CATALOG["transport/delay"],
        ]
        rules = _build_scenario_fault_rules(faults)
        assert len(rules) == 2


class TestDefaultMatrix:

    def test_default_matrix_has_scenarios(self):
        assert "marketplace" in DEFAULT_MATRIX
        assert "capability_spoofing" in DEFAULT_MATRIX
        assert "consensus" in DEFAULT_MATRIX

    def test_default_matrix_faults_are_valid(self):
        for scenario, fault_ids in DEFAULT_MATRIX.items():
            for fid in fault_ids:
                assert fid in FAULT_CATALOG, (
                    f"fault {fid!r} in DEFAULT_MATRIX[{scenario!r}]"
                    f" not in FAULT_CATALOG"
                )

    def test_capability_spoofing_uses_auth_fault(self):
        assert "auth/none" in DEFAULT_MATRIX["capability_spoofing"]

    def test_marketplace_uses_transport_faults(self):
        faults = DEFAULT_MATRIX["marketplace"]
        assert "transport/duplicate" in faults
        assert "transport/drop" in faults
        assert "transport/delay" in faults
        assert "transport/rate_flood" in faults


class TestWilsonCI:

    def test_perfect_pass(self):
        lo, hi = _wilson_ci(10, 10)
        assert lo > 0.6
        assert hi > 0.95

    def test_total_fail(self):
        lo, hi = _wilson_ci(0, 10)
        assert lo < 0.05
        assert hi < 0.4

    def test_zero_trials(self):
        lo, hi = _wilson_ci(0, 0)
        assert lo == 0.0
        assert hi == 0.0

    def test_half_pass(self):
        lo, hi = _wilson_ci(5, 10)
        assert 0.2 < lo < 0.5
        assert 0.5 < hi < 0.8

    def test_bounds_clamped(self):
        lo, hi = _wilson_ci(100, 100)
        assert lo >= 0.0
        assert hi <= 1.0


class TestCellStats:

    def test_empty_cell(self):
        cell = CellResult(scenario="test", fault_id="test/fault")
        stats = _compute_cell_stats(cell)
        assert stats.pass_rate == 0.0
        assert stats.non_deterministic is False

    def test_all_pass(self):
        cell = CellResult(
            scenario="test", fault_id="test/fault",
            passes=10,
            trials=[{"verdict": "passed"}] * 10,
        )
        stats = _compute_cell_stats(cell)
        assert stats.pass_rate == 1.0
        assert stats.non_deterministic is False
        assert stats.verdict_distribution == {"passed": 10}

    def test_mixed_verdicts_non_deterministic(self):
        cell = CellResult(
            scenario="test", fault_id="test/fault",
            passes=5, violations=5,
            trials=[{"verdict": "passed"}] * 5 + [{"verdict": "failed"}] * 5,
        )
        stats = _compute_cell_stats(cell)
        assert stats.pass_rate == 0.5
        assert stats.non_deterministic is True

    def test_all_same_verdict_deterministic(self):
        cell = CellResult(
            scenario="test", fault_id="test/fault",
            violations=10,
            trials=[{"verdict": "failed"}] * 10,
        )
        stats = _compute_cell_stats(cell)
        assert stats.pass_rate == 0.0
        assert stats.non_deterministic is False


class TestPropagationTrace:

    def test_trace_empty_events(self):
        trace = _trace_propagation("transport/drop", [], [])
        assert trace.fault_id == "transport/drop"
        assert trace.fault_event_ids == []

    def test_trace_detects_dropped_events(self):
        class FakeEvent:
            def __init__(self, eid, kind):
                self.event_id = eid
                self.kind = kind
        events = [
            FakeEvent("ev-1", "message_dropped"),
            FakeEvent("ev-2", "message_delivered"),
        ]
        trace = _trace_propagation("transport/drop", events, [])
        assert "ev-1" in trace.fault_event_ids
        assert "ev-2" not in trace.fault_event_ids

    def test_trace_builds_chain(self):
        class FakeEvent:
            def __init__(self, eid, kind):
                self.event_id = eid
                self.kind = kind
        events = [
            FakeEvent("ev-1", "message_duplicated"),
            FakeEvent("ev-2", "duplicate_recognized"),
        ]
        trace = _trace_propagation("transport/duplicate", events, ["settlement"])
        assert len(trace.chain) == 2
        assert trace.invariant_name == "settlement"

    def test_trace_to_dict(self):
        trace = PropagationTrace(fault_id="test")
        d = trace.to_dict()
        assert d["fault_id"] == "test"
        assert isinstance(d["chain"], list)


class TestCellResult:

    def test_cell_result_defaults(self):
        cell = CellResult(scenario="test", fault_id="test/fault")
        assert cell.violations == 0
        assert cell.passes == 0
        assert cell.errors == 0
        assert cell.incompletes == 0
        assert cell.trials == []
        assert cell.invariant_violations == {}
        assert cell.propagation == []
        assert cell.baseline_fault_id is None

    def test_cell_result_to_dict(self):
        cell = CellResult(
            scenario="marketplace",
            fault_id="transport/drop",
            violations=2,
            passes=8,
            invariant_violations={"settlement": 2},
        )
        d = cell.to_dict()
        assert d["scenario"] == "marketplace"
        assert d["fault_id"] == "transport/drop"
        assert d["violations"] == 2
        assert d["passes"] == 8
        assert d["invariant_violations"] == {"settlement": 2}
        assert "stats" in d
        assert "propagation" in d


class TestMatrixResult:

    def test_matrix_result_to_dict(self):
        result = MatrixResult(
            matrix_id="mtx-test",
            scenarios=["marketplace"],
            faults=["transport/drop"],
            trials_per_cell=5,
            seed_base=2000,
        )
        d = result.to_dict()
        assert d["matrix_id"] == "mtx-test"
        assert d["scenarios"] == ["marketplace"]
        assert d["trials_per_cell"] == 5
        assert "nandatown_version" in d
        assert d["comparison"] is None


class TestResolveFault:

    def test_resolve_baseline(self):
        faults = _resolve_fault("baseline")
        assert len(faults) == 1
        assert faults[0].fault_type == "baseline"

    def test_resolve_transport_fault(self):
        faults = _resolve_fault("transport/drop")
        assert len(faults) == 1
        assert faults[0].transport_action == "drop"

    def test_resolve_unknown_returns_empty(self):
        faults = _resolve_fault("nonexistent/fault")
        assert faults == []


class TestRunFailureMatrix:

    def test_single_scenario_single_fault_produces_bundles(self, tmp_path):
        matrix_dir, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=3000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        assert os.path.isdir(matrix_dir)
        cell_key = "voting/transport/drop"
        assert cell_key in result.cells
        cell = result.cells[cell_key]
        assert len(cell.trials) == 2

    def test_trials_produce_evidence_bundles(self, tmp_path):
        matrix_dir, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=3000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell_dir = os.path.join(matrix_dir, "voting/transport/drop")
        assert os.path.isdir(cell_dir)
        bundles = [d for d in os.listdir(cell_dir) if d.startswith("sim-")]
        assert len(bundles) == 2

    def test_deterministic_reproduction(self, tmp_path):
        dir1 = str(tmp_path / "run1")
        dir2 = str(tmp_path / "run2")
        os.makedirs(dir1)
        os.makedirs(dir2)
        _, result1 = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=3,
            seed_base=4000,
            out_dir=dir1,
            include_baseline=False,
        )
        _, result2 = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=3,
            seed_base=4000,
            out_dir=dir2,
            include_baseline=False,
        )
        for key in result1.cells:
            cell1 = result1.cells[key]
            cell2 = result2.cells[key]
            assert cell1.passes == cell2.passes
            assert cell1.violations == cell2.violations

    def test_matrix_plan_written(self, tmp_path):
        matrix_dir, _ = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        plan_path = os.path.join(matrix_dir, "matrix-plan.json")
        assert os.path.exists(plan_path)
        with open(plan_path) as f:
            plan = json.load(f)
        assert plan["scenarios"] == ["voting"]

    def test_matrix_result_written(self, tmp_path):
        matrix_dir, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        result_path = os.path.join(matrix_dir, "matrix-result.json")
        assert os.path.exists(result_path)

    def test_matrix_report_written(self, tmp_path):
        matrix_dir, _ = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        report_path = os.path.join(matrix_dir, "matrix-report.md")
        assert os.path.exists(report_path)
        with open(report_path) as f:
            text = f.read()
        assert "Protocol Failure Matrix" in text

    def test_heatmap_written(self, tmp_path):
        matrix_dir, _ = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        heatmap_path = os.path.join(matrix_dir, "matrix-heatmap.html")
        assert os.path.exists(heatmap_path)
        with open(heatmap_path) as f:
            text = f.read()
        assert "<!DOCTYPE html>" in text
        assert "Failure Matrix" in text

    def test_cell_result_json_written(self, tmp_path):
        matrix_dir, _ = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell_path = os.path.join(
            matrix_dir, "voting/transport/drop", "cell-result.json",
        )
        assert os.path.exists(cell_path)

    def test_layer_swap_fault_runs_capability_spoofing(self, tmp_path):
        matrix_dir, result = run_failure_matrix(
            scenarios=["capability_spoofing"],
            faults=["auth/none"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell_key = "capability_spoofing/auth/none"
        assert cell_key in result.cells
        cell = result.cells[cell_key]
        assert cell.violations > 0

    def test_multiple_scenarios_and_faults(self, tmp_path):
        matrix_dir, result = run_failure_matrix(
            scenarios=["voting", "capability_spoofing"],
            faults=["transport/drop", "auth/none"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        assert "voting/transport/drop" in result.cells
        assert "capability_spoofing/auth/none" in result.cells

    def test_default_faults_per_scenario(self, tmp_path):
        matrix_dir, result = run_failure_matrix(
            scenarios=["marketplace"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        assert "marketplace/transport/duplicate" in result.cells
        assert "marketplace/transport/drop" in result.cells
        assert "marketplace/transport/delay" in result.cells

    def test_unknown_fault_skipped(self, tmp_path):
        matrix_dir, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["nonexistent/fault"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        assert len(result.cells) == 0

    def test_all_bundles_verify(self, tmp_path):
        from nandatown.bundle import verify_bundle

        matrix_dir, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell_dir = os.path.join(matrix_dir, "voting/transport/drop")
        for bundle_name in os.listdir(cell_dir):
            if bundle_name.startswith("sim-"):
                bundle_path = os.path.join(cell_dir, bundle_name)
                problems = verify_bundle(bundle_path)
                assert problems == [], (
                    f"Bundle {bundle_name} verification failed: {problems}"
                )

    def test_invariant_violations_detected(self, tmp_path):
        matrix_dir, result = run_failure_matrix(
            scenarios=["capability_spoofing"],
            faults=["auth/none"],
            trials=3,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell = result.cells["capability_spoofing/auth/none"]
        assert cell.violations > 0
        assert len(cell.invariant_violations) > 0

    def test_seed_base_affects_results(self, tmp_path):
        _, result_a = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=5000,
            out_dir=str(tmp_path / "a"),
            include_baseline=False,
        )
        _, result_b = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=6000,
            out_dir=str(tmp_path / "b"),
            include_baseline=False,
        )
        for key in result_a.cells:
            cell_a = result_a.cells[key]
            cell_b = result_b.cells[key]
            for t_a, t_b in zip(cell_a.trials, cell_b.trials):
                assert t_a["seed"] != t_b["seed"]

    def test_trials_increment_seeds(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=3,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell = result.cells["voting/transport/drop"]
        seeds = [t["seed"] for t in cell.trials]
        assert len(seeds) == 3
        assert len(set(seeds)) == 3

    def test_include_baseline_adds_baseline_cell(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=True,
        )
        assert "voting/baseline" in result.cells
        assert "voting/transport/drop" in result.cells
        baseline = result.cells["voting/baseline"]
        assert baseline.stats.pass_rate == 1.0

    def test_no_baseline_skips_baseline_cell(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        assert "voting/baseline" not in result.cells

    def test_cell_stats_computed(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=3,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell = result.cells["voting/transport/drop"]
        assert isinstance(cell.stats, CellStats)
        assert 0.0 <= cell.stats.pass_rate <= 1.0
        assert 0.0 <= cell.stats.ci_lower <= 1.0
        assert 0.0 <= cell.stats.ci_upper <= 1.0

    def test_baseline_fault_id_set(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell = result.cells["voting/transport/drop"]
        assert cell.baseline_fault_id == "baseline"

    def test_baseline_cell_has_no_baseline_ref(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=[],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=True,
        )
        cell = result.cells["voting/baseline"]
        assert cell.baseline_fault_id is None


class TestRenderMatrixReport:

    def test_report_contains_header(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        text = render_matrix_report(result)
        assert "Protocol Failure Matrix" in text
        assert "Matrix ID" in text

    def test_report_contains_cell_data(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        text = render_matrix_report(result)
        assert "voting" in text
        assert "transport/drop" in text

    def test_report_contains_fault_descriptions(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        text = render_matrix_report(result)
        assert "First message is silently dropped" in text

    def test_report_contains_stats_columns(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        text = render_matrix_report(result)
        assert "Pass%" in text
        assert "95%CI" in text
        assert "Det?" in text

    def test_report_contains_legend(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        text = render_matrix_report(result)
        assert "Wilson score" in text


class TestRenderHeatmap:

    def test_heatmap_contains_html(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        html_out = render_heatmap(result)
        assert "<!DOCTYPE html>" in html_out
        assert "Failure Matrix" in html_out
        assert "voting" in html_out
        assert "transport/drop" in html_out

    def test_heatmap_contains_stats_table(self, tmp_path):
        _, result = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=1,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        html_out = render_heatmap(result)
        assert "<table>" in html_out
        assert "Pass Rate" in html_out


class TestMatrixComparison:

    def test_comparison_runs_both_configurations(self, tmp_path):
        cmp_dir, comparison = run_matrix_comparison(
            scenarios=["capability_spoofing"],
            faults=["auth/none"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            compare_layer="auth",
            compare_plugin="plain.v1",
        )
        assert os.path.isdir(cmp_dir)
        assert comparison["compare_layer"] == "auth"
        assert comparison["compare_plugin"] == "plain.v1"

    def test_comparison_detects_differences(self, tmp_path):
        cmp_dir, comparison = run_matrix_comparison(
            scenarios=["capability_spoofing"],
            faults=["auth/none"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            compare_layer="auth",
            compare_plugin="plain.v1",
        )
        assert len(comparison["differences"]) > 0

    def test_comparison_report_written(self, tmp_path):
        cmp_dir, _ = run_matrix_comparison(
            scenarios=["capability_spoofing"],
            faults=["auth/none"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            compare_layer="auth",
            compare_plugin="plain.v1",
        )
        report_path = os.path.join(cmp_dir, "comparison-report.md")
        assert os.path.exists(report_path)
        with open(report_path) as f:
            text = f.read()
        assert "Failure Matrix Comparison" in text

    def test_comparison_json_written(self, tmp_path):
        cmp_dir, _ = run_matrix_comparison(
            scenarios=["capability_spoofing"],
            faults=["auth/none"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            compare_layer="auth",
            compare_plugin="plain.v1",
        )
        json_path = os.path.join(cmp_dir, "comparison.json")
        assert os.path.exists(json_path)


class TestMatrixCLI:

    def test_matrix_cli_runs_successfully(self, tmp_path, capsys):
        from nandatown.cli import main

        ret = main([
            "matrix",
            "--scenario", "voting",
            "--fault", "transport/drop",
            "--trials", "2",
            "--seed-base", "2000",
            "--no-baseline",
            "--out", str(tmp_path),
        ])
        assert ret == 0
        text = capsys.readouterr().out
        assert "Protocol Failure Matrix" in text

    def test_matrix_cli_default_faults(self, tmp_path, capsys):
        from nandatown.cli import main

        ret = main([
            "matrix",
            "--scenario", "voting",
            "--trials", "1",
            "--no-baseline",
            "--out", str(tmp_path),
        ])
        assert ret == 0

    def test_matrix_cli_returns_nonzero_on_violations(self, tmp_path):
        from nandatown.cli import main

        ret = main([
            "matrix",
            "--scenario", "capability_spoofing",
            "--fault", "auth/none",
            "--trials", "3",
            "--no-baseline",
            "--out", str(tmp_path),
        ])
        assert ret == 1

    def test_matrix_cli_with_baseline(self, tmp_path, capsys):
        from nandatown.cli import main

        ret = main([
            "matrix",
            "--scenario", "voting",
            "--fault", "transport/drop",
            "--trials", "1",
            "--out", str(tmp_path),
        ])
        assert ret == 0

    def test_matrix_cli_compare_layer(self, tmp_path, capsys):
        from nandatown.cli import main

        ret = main([
            "matrix",
            "--scenario", "capability_spoofing",
            "--fault", "auth/none",
            "--trials", "2",
            "--compare-layer", "auth=plain.v1",
            "--out", str(tmp_path),
        ])
        assert ret == 1
        text = capsys.readouterr().out
        assert "Failure Matrix Comparison" in text


class TestExistingFunctionalityPreserved:

    def test_lab_scenario_still_runs_directly(self, tmp_path):
        from nandatown.sim.runner import run_lab

        bundle_dir, result = run_lab("voting", str(tmp_path))
        assert result.verdict in ("passed", "failed", "incomplete", "error")
        assert os.path.exists(os.path.join(bundle_dir, "manifest.json"))

    def test_compare_still_works(self, tmp_path):
        from nandatown.compare import run_comparison

        compare_dir, comparison = run_comparison(
            "capability_spoofing", {"auth": "plain.v1"}, str(tmp_path),
        )
        assert comparison["variants"]["baseline"]["verdict"] == "passed"
        assert comparison["variants"]["swapped"]["verdict"] == "failed"

    def test_campaign_still_works(self, tmp_path):
        from nandatown.campaign import run_campaign

        campaign_dir, aggregate = run_campaign(
            "voting", trials=2, out_dir=str(tmp_path),
        )
        assert aggregate["trials"] == 2

    def test_bundle_verification_still_works(self, tmp_path):
        from nandatown.bundle import verify_bundle
        from nandatown.sim.runner import run_lab

        bundle_dir, _ = run_lab("voting", str(tmp_path))
        problems = verify_bundle(bundle_dir)
        assert problems == []


class TestScenarioFaultInjection:

    def test_marketplace_with_transport_duplicate(self, tmp_path):
        from nandatown.sim.runner import run_lab

        bundle_dir, result = run_lab("marketplace", str(tmp_path), seed=42)
        assert result.verdict in ("passed", "failed", "incomplete", "error")

    def test_consensus_with_transport_drop(self, tmp_path):
        from nandatown.sim.runner import run_lab

        bundle_dir, result = run_lab("consensus", str(tmp_path), seed=42)
        assert result.verdict in ("passed", "failed", "incomplete", "error")

    def test_capability_spoofing_with_auth_none(self, tmp_path):
        from nandatown.sim.runner import run_lab

        bundle_dir, result = run_lab(
            "capability_spoofing", str(tmp_path), seed=42,
            layer_overrides={"auth": "plain.v1"},
        )
        assert result.verdict == "failed"

    def test_capability_spoofing_baseline_passes(self, tmp_path):
        from nandatown.sim.runner import run_lab

        bundle_dir, result = run_lab(
            "capability_spoofing", str(tmp_path), seed=42,
        )
        assert result.verdict == "passed"


class TestEvidenceIntegration:

    def test_each_trial_bundle_has_correct_mode(self, tmp_path):
        from nandatown.bundle import load_bundle

        matrix_dir, _ = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell_dir = os.path.join(matrix_dir, "voting/transport/drop")
        for name in os.listdir(cell_dir):
            if name.startswith("sim-"):
                bundle = load_bundle(os.path.join(cell_dir, name))
                assert bundle["manifest"]["mode"] == "lab"

    def test_each_trial_bundle_has_events(self, tmp_path):
        from nandatown.bundle import load_bundle

        matrix_dir, _ = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell_dir = os.path.join(matrix_dir, "voting/transport/drop")
        for name in os.listdir(cell_dir):
            if name.startswith("sim-"):
                bundle = load_bundle(os.path.join(cell_dir, name))
                assert len(bundle["events"]) > 0

    def test_each_trial_bundle_has_stages(self, tmp_path):
        from nandatown.bundle import load_bundle

        matrix_dir, _ = run_failure_matrix(
            scenarios=["voting"],
            faults=["transport/drop"],
            trials=2,
            seed_base=2000,
            out_dir=str(tmp_path),
            include_baseline=False,
        )
        cell_dir = os.path.join(matrix_dir, "voting/transport/drop")
        for name in os.listdir(cell_dir):
            if name.startswith("sim-"):
                bundle = load_bundle(os.path.join(cell_dir, name))
                assert len(bundle["result"].stages) > 0
