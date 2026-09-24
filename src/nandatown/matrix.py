"""Protocol Failure Matrix: systematic fault injection across scenarios.

Runs every (scenario × fault) combination for N trials, records verdicts
and per-stage results, and produces a research-quality matrix table.

Fault injection reuses the existing mechanisms:
- Transport faults: FaultRule declarations (drop, duplicate, delay)
- Layer swaps: layer_overrides (e.g., auth=plain.v1)

Evaluation reuses the existing scenario evaluators.
Evidence reuses the existing bundle infrastructure.
"""

from __future__ import annotations

import html
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import __version__


@dataclass
class FaultSpec:
    fault_id: str
    layer: str
    fault_type: str
    transport_action: str | None = None
    transport_kind: str | None = None
    transport_nth: int = 1
    transport_delay: float | None = None
    transport_rate: float | None = None
    transport_groups: list[list[str]] | None = None
    swap_plugin_id: str | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = {
            "fault_id": self.fault_id,
            "layer": self.layer,
            "fault_type": self.fault_type,
            "transport_action": self.transport_action,
            "transport_kind": self.transport_kind,
            "transport_nth": self.transport_nth,
            "transport_delay": self.transport_delay,
            "transport_rate": self.transport_rate,
            "swap_plugin_id": self.swap_plugin_id,
            "description": self.description,
        }
        if self.transport_groups is not None:
            result["transport_groups"] = self.transport_groups
        return result


FAULT_CATALOG: dict[str, FaultSpec] = {}


def register_fault(spec: FaultSpec) -> FaultSpec:
    FAULT_CATALOG[spec.fault_id] = spec
    return spec


BASELINE_FAULT = FaultSpec(
    fault_id="baseline",
    layer="none",
    fault_type="baseline",
    description="No fault injected (healthy control)",
)
FAULT_CATALOG["baseline"] = BASELINE_FAULT


register_fault(FaultSpec(
    fault_id="transport/duplicate",
    layer="transport",
    fault_type="transport_fault",
    transport_action="duplicate",
    description="First message is delivered twice",
))

register_fault(FaultSpec(
    fault_id="transport/drop",
    layer="transport",
    fault_type="transport_fault",
    transport_action="drop",
    description="First message is silently dropped",
))

register_fault(FaultSpec(
    fault_id="transport/delay",
    layer="transport",
    fault_type="transport_fault",
    transport_action="delay",
    transport_delay=2.0,
    description="First message is delayed by 2.0s",
))

register_fault(FaultSpec(
    fault_id="transport/rate_flood",
    layer="transport",
    fault_type="transport_fault",
    transport_action="drop_rate",
    transport_rate=0.5,
    description="50% of messages randomly dropped",
))

register_fault(FaultSpec(
    fault_id="transport/drop_2nd",
    layer="transport",
    fault_type="transport_fault",
    transport_action="drop",
    transport_nth=2,
    description="Second message is silently dropped",
))

register_fault(FaultSpec(
    fault_id="transport/delay_long",
    layer="transport",
    fault_type="transport_fault",
    transport_action="delay",
    transport_delay=5.0,
    description="First message delayed by 5.0s (beyond most timeouts)",
))

register_fault(FaultSpec(
    fault_id="auth/none",
    layer="auth",
    fault_type="layer_swap",
    swap_plugin_id="plain.v1",
    description="Authentication disabled: any claimed sender accepted",
))

register_fault(FaultSpec(
    fault_id="transport/duplicate_2x",
    layer="transport",
    fault_type="transport_fault",
    transport_action="duplicate",
    transport_nth=2,
    description="Second message is delivered twice",
))

register_fault(FaultSpec(
    fault_id="transport/drop_3rd",
    layer="transport",
    fault_type="transport_fault",
    transport_action="drop",
    transport_nth=3,
    description="Third message is silently dropped",
))


register_fault(FaultSpec(
    fault_id="transport/partition",
    layer="transport",
    fault_type="transport_fault",
    transport_action="partition",
    transport_groups=[],
    description="Network partition: agents in different groups cannot communicate",
))


DEFAULT_MATRIX: dict[str, list[str]] = {
    "marketplace": [
        "transport/duplicate",
        "transport/drop",
        "transport/delay",
        "transport/rate_flood",
    ],
    "capability_spoofing": [
        "auth/none",
    ],
    "consensus": [
        "transport/drop",
        "transport/delay",
        "transport/drop_2nd",
        "transport/rate_flood",
    ],
    "auction": [
        "transport/drop",
        "transport/delay",
        "transport/rate_flood",
    ],
    "supply_chain": [
        "transport/duplicate",
        "transport/drop",
        "transport/delay",
        "transport/delay_long",
    ],
    "voting": [
        "transport/drop",
        "transport/rate_flood",
    ],
    "network_partition": [
        "transport/partition",
    ],
}


def _build_scenario_fault_rules(faults: list[FaultSpec]) -> list[dict[str, Any]]:
    rules = []
    for fault in faults:
        if fault.fault_type != "transport_fault":
            continue
        rule: dict[str, Any] = {"action": fault.transport_action}
        if fault.transport_kind is not None:
            rule["kind"] = fault.transport_kind
        if fault.transport_nth != 1:
            rule["nth"] = fault.transport_nth
        if fault.transport_delay is not None:
            rule["delay"] = fault.transport_delay
        if fault.transport_rate is not None:
            rule["rate"] = fault.transport_rate
        if fault.transport_groups is not None:
            rule["groups"] = fault.transport_groups
        rules.append(rule)
    return rules


def _build_layer_overrides(faults: list[FaultSpec]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for fault in faults:
        if fault.fault_type == "layer_swap" and fault.swap_plugin_id:
            overrides[fault.layer] = fault.swap_plugin_id
    return overrides or None


@dataclass
class PropagationTrace:
    fault_id: str
    fault_event_ids: list[str] = field(default_factory=list)
    intermediate_event_ids: list[str] = field(default_factory=list)
    invariant_event_ids: list[str] = field(default_factory=list)
    invariant_name: str = ""
    chain: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fault_id": self.fault_id,
            "fault_event_ids": self.fault_event_ids,
            "intermediate_event_ids": self.intermediate_event_ids,
            "invariant_event_ids": self.invariant_event_ids,
            "invariant_name": self.invariant_name,
            "chain": self.chain,
        }


FAULT_EVENT_KINDS = {
    "message_dropped", "message_duplicated", "message_delayed",
    "card_unverified", "signature_invalid", "message_partition_blocked",
}

PROPAGATION_CHAINS: dict[str, list[tuple[str, str]]] = {
    "transport/duplicate": [
        ("message_duplicated", "transport duplicated a message"),
        ("duplicate_recognized", "agent recognized the duplicate"),
    ],
    "transport/drop": [
        ("message_dropped", "transport dropped a message"),
        ("proposal_retry", "proposer retried after timeout"),
    ],
    "transport/delay": [
        ("message_delayed", "transport delayed a message"),
    ],
    "transport/rate_flood": [
        ("message_dropped", "transport randomly dropped messages"),
    ],
    "auth/none": [
        ("card_unverified", "forged card was registered"),
    ],
    "transport/partition": [
        ("message_partition_blocked", "cross-group message blocked by partition"),
    ],
}


def _trace_propagation(
    fault_id: str,
    events: list[Any],
    failed_stages: list[str],
) -> PropagationTrace:
    trace = PropagationTrace(fault_id=fault_id)

    for event in events:
        kind = event.kind if hasattr(event, "kind") else event.get("kind", "")
        eid = event.event_id if hasattr(event, "event_id") else event.get("event_id", "")
        if kind in FAULT_EVENT_KINDS:
            trace.fault_event_ids.append(eid)

    chain_template = PROPAGATION_CHAINS.get(fault_id, [])
    for event_kind, description in chain_template:
        matches = [
            e for e in events
            if (e.kind if hasattr(e, "kind") else e.get("kind", "")) == event_kind
        ]
        if matches:
            trace.chain.append(description)
            for m in matches:
                eid = m.event_id if hasattr(m, "event_id") else m.get("event_id", "")
                if eid not in trace.fault_event_ids:
                    trace.intermediate_event_ids.append(eid)

    if failed_stages:
        trace.invariant_name = failed_stages[0]

    return trace


@dataclass
class CellStats:
    pass_rate: float = 0.0
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    non_deterministic: bool = False
    verdict_distribution: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass_rate": round(self.pass_rate, 4),
            "ci_lower": round(self.ci_lower, 4),
            "ci_upper": round(self.ci_upper, 4),
            "non_deterministic": self.non_deterministic,
            "verdict_distribution": dict(self.verdict_distribution),
        }


def _wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


def _compute_cell_stats(cell: CellResult) -> CellStats:
    stats = CellStats()
    total = len(cell.trials)
    if total == 0:
        return stats

    stats.pass_rate = cell.passes / total
    stats.ci_lower, stats.ci_upper = _wilson_ci(cell.passes, total)

    verdicts: dict[str, int] = {}
    for trial in cell.trials:
        v = trial.get("verdict", "unknown")
        verdicts[v] = verdicts.get(v, 0) + 1
    stats.verdict_distribution = verdicts

    unique_verdicts = {t.get("verdict") for t in cell.trials}
    stats.non_deterministic = len(unique_verdicts) > 1

    return stats


@dataclass
class CellResult:
    scenario: str
    fault_id: str
    trials: list[dict[str, Any]] = field(default_factory=list)
    violations: int = 0
    passes: int = 0
    errors: int = 0
    incompletes: int = 0
    invariant_violations: dict[str, int] = field(default_factory=dict)
    propagation: list[PropagationTrace] = field(default_factory=list)
    stats: CellStats = field(default_factory=CellStats)
    baseline_fault_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "fault_id": self.fault_id,
            "trials": len(self.trials),
            "violations": self.violations,
            "passes": self.passes,
            "errors": self.errors,
            "incompletes": self.incompletes,
            "invariant_violations": dict(self.invariant_violations),
            "propagation": [p.to_dict() for p in self.propagation],
            "stats": self.stats.to_dict(),
            "baseline_fault_id": self.baseline_fault_id,
        }


@dataclass
class MatrixResult:
    matrix_id: str
    scenarios: list[str]
    faults: list[str]
    trials_per_cell: int
    seed_base: int
    cells: dict[str, CellResult] = field(default_factory=dict)
    started_at: float = 0.0
    completed_at: float = 0.0
    comparison: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "matrix_id": self.matrix_id,
            "scenarios": self.scenarios,
            "faults": self.faults,
            "trials_per_cell": self.trials_per_cell,
            "seed_base": self.seed_base,
            "cells": {k: v.to_dict() for k, v in self.cells.items()},
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "comparison": self.comparison,
            "nandatown_version": __version__,
        }


def _run_cell_trials(
    scenario: str,
    faults: list[FaultSpec],
    trials: int,
    seed_base: int,
    cell_index: int,
    out_dir: str,
) -> CellResult:
    from .bundle import load_bundle
    from .sim.runner import run_lab

    primary_fault = faults[0] if faults else BASELINE_FAULT
    cell = CellResult(scenario=scenario, fault_id=primary_fault.fault_id)

    is_baseline = any(f.fault_type == "baseline" for f in faults)
    if not is_baseline:
        cell.baseline_fault_id = "baseline"

    fault_rules = _build_scenario_fault_rules(faults)
    layer_overrides = _build_layer_overrides(faults)

    for trial in range(trials):
        seed = seed_base + cell_index * 1000 + trial
        trial_record: dict[str, Any] = {"trial": trial + 1, "seed": seed}
        try:
            bundle_dir, result = run_lab(
                scenario,
                out_dir,
                seed=seed,
                layer_overrides=layer_overrides,
            )
            trial_record["run_id"] = result.run_id
            trial_record["verdict"] = result.verdict
            trial_record["bundle"] = os.path.basename(bundle_dir)
            trial_record["stages"] = {
                s.name: s.status for s in result.stages
            }

            failed_stages = [
                s.name for s in result.stages if s.status == "failed"
            ]

            if result.verdict == "failed":
                cell.violations += 1
                for s in result.stages:
                    if s.status == "failed":
                        cell.invariant_violations[s.name] = (
                            cell.invariant_violations.get(s.name, 0) + 1
                        )
            elif result.verdict == "passed":
                cell.passes += 1
            elif result.verdict == "error":
                cell.errors += 1
            else:
                cell.incompletes += 1

            if not is_baseline and failed_stages:
                try:
                    bundle = load_bundle(bundle_dir)
                    events = bundle.get("events", [])
                    prop = _trace_propagation(
                        primary_fault.fault_id, events, failed_stages,
                    )
                    if prop.fault_event_ids or prop.chain:
                        cell.propagation.append(prop)
                        trial_record["propagation"] = prop.to_dict()
                except Exception:
                    pass

        except Exception as exc:
            trial_record["verdict"] = "error"
            trial_record["error"] = f"{type(exc).__name__}: {exc}"
            cell.errors += 1

        cell.trials.append(trial_record)

    cell.stats = _compute_cell_stats(cell)
    return cell


def _resolve_fault(fault_id: str) -> list[FaultSpec]:
    if fault_id == "baseline":
        return [BASELINE_FAULT]
    spec = FAULT_CATALOG.get(fault_id)
    if spec is None:
        return []
    return [spec]


def run_failure_matrix(
    scenarios: list[str] | None = None,
    faults: list[str] | None = None,
    trials: int = 10,
    seed_base: int = 2000,
    out_dir: str = "runs",
    include_baseline: bool = True,
    layer_overrides: dict[str, str] | None = None,
) -> tuple[str, MatrixResult]:
    matrix_id = "mtx-" + uuid.uuid4().hex[:10]
    matrix_dir = os.path.join(out_dir, matrix_id)
    os.makedirs(matrix_dir, exist_ok=True)

    if scenarios is None:
        scenarios = sorted(DEFAULT_MATRIX.keys())
    if faults is None:
        fault_ids = sorted({f for fl in DEFAULT_MATRIX.values() for f in fl})
    else:
        fault_ids = faults

    effective_faults = list(fault_ids)
    if include_baseline and "baseline" not in effective_faults:
        effective_faults.insert(0, "baseline")

    result = MatrixResult(
        matrix_id=matrix_id,
        scenarios=scenarios,
        faults=effective_faults,
        trials_per_cell=trials,
        seed_base=seed_base,
        started_at=time.time(),
    )

    plan = {
        "matrix_id": matrix_id,
        "scenarios": scenarios,
        "faults": effective_faults,
        "trials_per_cell": trials,
        "seed_base": seed_base,
        "include_baseline": include_baseline,
        "layer_overrides": layer_overrides,
        "nandatown_version": __version__,
        "declared_at": result.started_at,
        "policy": "every trial is reported: pass, fail, incomplete, error",
    }
    with open(os.path.join(matrix_dir, "matrix-plan.json"), "w") as f:
        json.dump(plan, f, indent=2)

    cell_index = 0
    for scenario in scenarios:
        applicable_faults = DEFAULT_MATRIX.get(scenario, fault_ids)
        for fault_id in effective_faults:
            if fault_id != "baseline" and fault_id not in applicable_faults:
                continue
            fault_specs = _resolve_fault(fault_id)
            if not fault_specs:
                continue

            if layer_overrides:
                combined = list(fault_specs)
                for layer, pid in layer_overrides.items():
                    combined.append(FaultSpec(
                        fault_id=f"override/{layer}",
                        layer=layer,
                        fault_type="layer_swap",
                        swap_plugin_id=pid,
                    ))
                fault_specs = combined

            cell_key = f"{scenario}/{fault_id}"
            cell_dir = os.path.join(matrix_dir, cell_key)
            os.makedirs(cell_dir, exist_ok=True)

            cell = _run_cell_trials(
                scenario, fault_specs, trials, seed_base, cell_index, cell_dir,
            )
            result.cells[cell_key] = cell

            with open(os.path.join(cell_dir, "cell-result.json"), "w") as f:
                json.dump(cell.to_dict(), f, indent=2)

            cell_index += 1

    result.completed_at = time.time()

    with open(os.path.join(matrix_dir, "matrix-result.json"), "w") as f:
        json.dump(result.to_dict(), f, indent=2)
    with open(os.path.join(matrix_dir, "matrix-report.md"), "w") as f:
        f.write(render_matrix_report(result))
    with open(os.path.join(matrix_dir, "matrix-heatmap.html"), "w") as f:
        f.write(render_heatmap(result))

    return matrix_dir, result


def run_matrix_comparison(
    scenarios: list[str] | None = None,
    faults: list[str] | None = None,
    trials: int = 10,
    seed_base: int = 2000,
    out_dir: str = "runs",
    compare_layer: str = "auth",
    compare_plugin: str = "plain.v1",
) -> tuple[str, dict[str, Any]]:
    cmp_id = "cmp-mtx-" + uuid.uuid4().hex[:10]
    cmp_dir = os.path.join(out_dir, cmp_id)
    os.makedirs(cmp_dir, exist_ok=True)

    _, baseline_result = run_failure_matrix(
        scenarios=scenarios,
        faults=faults,
        trials=trials,
        seed_base=seed_base,
        out_dir=os.path.join(cmp_dir, "baseline"),
        include_baseline=True,
    )

    _, swapped_result = run_failure_matrix(
        scenarios=scenarios,
        faults=faults,
        trials=trials,
        seed_base=seed_base,
        out_dir=os.path.join(cmp_dir, "swapped"),
        include_baseline=True,
        layer_overrides={compare_layer: compare_plugin},
    )

    differences: list[dict[str, Any]] = []
    for key in set(baseline_result.cells.keys()) | set(swapped_result.cells.keys()):
        b_cell = baseline_result.cells.get(key)
        s_cell = swapped_result.cells.get(key)
        if b_cell and s_cell:
            if b_cell.stats.pass_rate != s_cell.stats.pass_rate:
                differences.append({
                    "cell": key,
                    "baseline_pass_rate": round(b_cell.stats.pass_rate, 4),
                    "swapped_pass_rate": round(s_cell.stats.pass_rate, 4),
                    "baseline_violations": b_cell.violations,
                    "swapped_violations": s_cell.violations,
                })

    comparison = {
        "comparison_id": cmp_id,
        "compare_layer": compare_layer,
        "compare_plugin": compare_plugin,
        "baseline_matrix": baseline_result.matrix_id,
        "swapped_matrix": swapped_result.matrix_id,
        "differences": differences,
        "compared_at": time.time(),
    }

    with open(os.path.join(cmp_dir, "comparison.json"), "w") as f:
        json.dump(comparison, f, indent=2)
    with open(os.path.join(cmp_dir, "comparison-report.md"), "w") as f:
        f.write(_render_comparison_report(comparison, baseline_result, swapped_result))

    return cmp_dir, comparison


def _render_comparison_report(
    comparison: dict[str, Any],
    baseline: MatrixResult,
    swapped: MatrixResult,
) -> str:
    lines: list[str] = []
    add = lines.append
    add("NANDA Town Failure Matrix Comparison")
    add("=" * 50)
    add(f"Layer swap:   {comparison['compare_layer']}={comparison['compare_plugin']}")
    add(f"Baseline:     {baseline.matrix_id}")
    add(f"Swapped:      {swapped.matrix_id}")
    add("")

    if not comparison["differences"]:
        add("No differences detected between baseline and swapped configurations.")
    else:
        add(f"{'Cell':<40} {'Base Pass%':>10} {'Swap Pass%':>10} {'Delta':>8}")
        add("-" * 70)
        for diff in comparison["differences"]:
            delta = diff["swapped_pass_rate"] - diff["baseline_pass_rate"]
            add(
                f"{diff['cell']:<40} "
                f"{diff['baseline_pass_rate']:>10.1%} "
                f"{diff['swapped_pass_rate']:>10.1%} "
                f"{delta:>+8.1%}"
            )
    add("")
    add("Each column is backed by its own verifiable evidence bundles.")
    return "\n".join(lines) + "\n"


def render_matrix_report(result: MatrixResult) -> str:
    lines: list[str] = []
    add = lines.append
    add("NANDA Town Protocol Failure Matrix")
    add("=" * 60)
    add(f"Matrix ID:   {result.matrix_id}")
    add(f"Trials/cell: {result.trials_per_cell}")
    add(f"Seed base:   {result.seed_base}")
    add(f"Scenarios:   {', '.join(result.scenarios)}")
    add(f"Faults:      {', '.join(result.faults)}")
    add("")

    header = (
        f"{'Scenario':<25} {'Fault':<25} {'Trials':>6} "
        f"{'Pass%':>6} {'95%CI':>14} "
        f"{'P':>3} {'F':>3} {'E':>3} {'I':>3} "
        f"{'Det?':>4} {'Invariant Hits'}"
    )
    add(header)
    add("-" * len(header))

    for scenario in result.scenarios:
        for fault_id in result.faults:
            cell_key = f"{scenario}/{fault_id}"
            cell = result.cells.get(cell_key)
            if cell is None:
                continue
            inv_parts = []
            for inv, count in sorted(cell.invariant_violations.items()):
                inv_parts.append(f"{inv}:{count}")
            inv_str = ", ".join(inv_parts) if inv_parts else "-"
            total = len(cell.trials)
            s = cell.stats
            det_marker = "yes" if s.non_deterministic else "no"
            ci_str = f"[{s.ci_lower:.0%},{s.ci_upper:.0%}]"
            add(
                f"{scenario:<25} {fault_id:<25} {total:>6} "
                f"{s.pass_rate:>5.0%} {ci_str:>14} "
                f"{cell.passes:>3} {cell.violations:>3} "
                f"{cell.errors:>3} {cell.incompletes:>3} "
                f"{det_marker:>4} {inv_str}"
            )
        add("")

    add("Legend: P=pass, F=fail, E=error, I=incomplete, Det=non-deterministic")
    add("")

    has_propagation = any(
        cell.propagation for cell in result.cells.values()
    )
    if has_propagation:
        add("Propagation Traces (fault → cascade → invariant breach):")
        add("-" * 50)
        for scenario in result.scenarios:
            for fault_id in result.faults:
                cell_key = f"{scenario}/{fault_id}"
                cell = result.cells.get(cell_key)
                if not cell or not cell.propagation:
                    continue
                add(f"  {cell_key}:")
                for prop in cell.propagation[:3]:
                    for step in prop.chain:
                        add(f"    → {step}")
                    if prop.invariant_name:
                        add(f"    → INVARIANT BREACH: {prop.invariant_name}")
                if len(cell.propagation) > 3:
                    add(f"    ... and {len(cell.propagation) - 3} more traces")
        add("")

    add("Fault descriptions:")
    for fault_id in result.faults:
        fault = FAULT_CATALOG.get(fault_id)
        if fault:
            add(f"  {fault_id:<25} {fault.description}")
    add("")
    add("Each cell is backed by verifiable evidence bundles in this"
        " directory.")
    add("The unit of evidence is the distribution, not any single trial.")
    add("Confidence intervals use the Wilson score method (z=1.96).")
    return "\n".join(lines) + "\n"


def load_matrix_result(matrix_dir: str) -> MatrixResult:
    result_path = os.path.join(matrix_dir, "matrix-result.json")
    if not os.path.exists(result_path):
        raise FileNotFoundError(f"no matrix-result.json in {matrix_dir}")
    with open(result_path) as f:
        data = json.load(f)

    cells: dict[str, CellResult] = {}
    for cell_key, cell_data in data.get("cells", {}).items():
        stats = CellStats(
            pass_rate=cell_data.get("stats", {}).get("pass_rate", 0.0),
            ci_lower=cell_data.get("stats", {}).get("ci_lower", 0.0),
            ci_upper=cell_data.get("stats", {}).get("ci_upper", 0.0),
            non_deterministic=cell_data.get("stats", {}).get(
                "non_deterministic", False),
            verdict_distribution=cell_data.get("stats", {}).get(
                "verdict_distribution", {}),
        )
        cells[cell_key] = CellResult(
            scenario=cell_data["scenario"],
            fault_id=cell_data["fault_id"],
            trials=cell_data.get("trials_list", cell_data.get("trials", [])),
            violations=cell_data.get("violations", 0),
            passes=cell_data.get("passes", 0),
            errors=cell_data.get("errors", 0),
            incompletes=cell_data.get("incompletes", 0),
            invariant_violations=cell_data.get("invariant_violations", {}),
            stats=stats,
            baseline_fault_id=cell_data.get("baseline_fault_id"),
        )

    return MatrixResult(
        matrix_id=data["matrix_id"],
        scenarios=data["scenarios"],
        faults=data["faults"],
        trials_per_cell=data["trials_per_cell"],
        seed_base=data["seed_base"],
        cells=cells,
        started_at=data.get("started_at", 0.0),
        completed_at=data.get("completed_at", 0.0),
    )


@dataclass
class DiffEntry:
    cell_key: str
    old_pass_rate: float
    new_pass_rate: float
    old_violations: int
    new_violations: int
    delta_pass_rate: float
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_key": self.cell_key,
            "old_pass_rate": round(self.old_pass_rate, 4),
            "new_pass_rate": round(self.new_pass_rate, 4),
            "old_violations": self.old_violations,
            "new_violations": self.new_violations,
            "delta_pass_rate": round(self.delta_pass_rate, 4),
            "status": self.status,
        }


@dataclass
class MatrixDiff:
    old_matrix_id: str
    new_matrix_id: str
    old_dir: str
    new_dir: str
    entries: list[DiffEntry] = field(default_factory=list)
    regressions: int = 0
    improvements: int = 0
    unchanged: int = 0
    new_cells: int = 0
    removed_cells: int = 0
    compared_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_matrix_id": self.old_matrix_id,
            "new_matrix_id": self.new_matrix_id,
            "old_dir": self.old_dir,
            "new_dir": self.new_dir,
            "entries": [e.to_dict() for e in self.entries],
            "regressions": self.regressions,
            "improvements": self.improvements,
            "unchanged": self.unchanged,
            "new_cells": self.new_cells,
            "removed_cells": self.removed_cells,
            "compared_at": self.compared_at,
        }


def diff_matrices(old_dir: str, new_dir: str) -> tuple[str, MatrixDiff]:
    old_result = load_matrix_result(old_dir)
    new_result = load_matrix_result(new_dir)

    diff = MatrixDiff(
        old_matrix_id=old_result.matrix_id,
        new_matrix_id=new_result.matrix_id,
        old_dir=old_dir,
        new_dir=new_dir,
        compared_at=time.time(),
    )

    all_keys = set(old_result.cells.keys()) | set(new_result.cells.keys())

    for key in sorted(all_keys):
        old_cell = old_result.cells.get(key)
        new_cell = new_result.cells.get(key)

        if old_cell is None:
            entry = DiffEntry(
                cell_key=key,
                old_pass_rate=0.0,
                new_pass_rate=new_cell.stats.pass_rate if new_cell else 0.0,
                old_violations=0,
                new_violations=new_cell.violations if new_cell else 0,
                delta_pass_rate=new_cell.stats.pass_rate if new_cell else 0.0,
                status="new",
            )
            diff.new_cells += 1
        elif new_cell is None:
            entry = DiffEntry(
                cell_key=key,
                old_pass_rate=old_cell.stats.pass_rate,
                new_pass_rate=0.0,
                old_violations=old_cell.violations,
                new_violations=0,
                delta_pass_rate=-old_cell.stats.pass_rate,
                status="removed",
            )
            diff.removed_cells += 1
        else:
            delta = new_cell.stats.pass_rate - old_cell.stats.pass_rate
            if delta < -0.001:
                status = "REGRESSION"
                diff.regressions += 1
            elif delta > 0.001:
                status = "improvement"
                diff.improvements += 1
            else:
                status = "unchanged"
                diff.unchanged += 1
            entry = DiffEntry(
                cell_key=key,
                old_pass_rate=old_cell.stats.pass_rate,
                new_pass_rate=new_cell.stats.pass_rate,
                old_violations=old_cell.violations,
                new_violations=new_cell.violations,
                delta_pass_rate=delta,
                status=status,
            )

        diff.entries.append(entry)

    diff_dir = os.path.join(
        os.path.dirname(new_dir),
        f"diff-{old_result.matrix_id[:12]}-{new_result.matrix_id[:12]}",
    )
    os.makedirs(diff_dir, exist_ok=True)

    with open(os.path.join(diff_dir, "diff-result.json"), "w") as f:
        json.dump(diff.to_dict(), f, indent=2)
    with open(os.path.join(diff_dir, "diff-report.md"), "w") as f:
        f.write(render_diff_report(diff))

    return diff_dir, diff


def render_diff_report(diff: MatrixDiff) -> str:
    lines: list[str] = []
    add = lines.append
    add("NANDA Town Failure Matrix Diff")
    add("=" * 60)
    add(f"Old:  {diff.old_matrix_id}  ({diff.old_dir})")
    add(f"New:  {diff.new_matrix_id}  ({diff.new_dir})")
    add("")
    add(f"Regressions:  {diff.regressions}")
    add(f"Improvements: {diff.improvements}")
    add(f"Unchanged:    {diff.unchanged}")
    add(f"New cells:    {diff.new_cells}")
    add(f"Removed:      {diff.removed_cells}")
    add("")

    has_change = diff.regressions or diff.improvements or diff.new_cells or diff.removed_cells
    if not has_change:
        add("No changes detected between the two matrix runs.")
        return "\n".join(lines) + "\n"

    header = (
        f"{'Cell':<40} {'Old Pass%':>10} {'New Pass%':>10} "
        f"{'Delta':>8} {'Old F':>6} {'New F':>6} {'Status'}"
    )
    add(header)
    add("-" * len(header))

    shown = [e for e in diff.entries if e.status != "unchanged"]
    if not shown:
        shown = diff.entries

    for entry in shown:
        marker = ""
        if entry.status == "REGRESSION":
            marker = " <<< REGRESSION"
        elif entry.status == "improvement":
            marker = " ^^^ improvement"
        elif entry.status == "new":
            marker = " (new)"
        elif entry.status == "removed":
            marker = " (removed)"
        add(
            f"{entry.cell_key:<40} "
            f"{entry.old_pass_rate:>10.1%} "
            f"{entry.new_pass_rate:>10.1%} "
            f"{entry.delta_pass_rate:>+8.1%} "
            f"{entry.old_violations:>6} "
            f"{entry.new_violations:>6} "
            f"{entry.status}{marker}"
        )

    add("")
    add("Regressions (<<<) indicate pass rate decreased between old and new.")
    add("Improvements (^^^) indicate pass rate increased.")
    return "\n".join(lines) + "\n"


@dataclass
class VerifyResult:
    matrix_id: str
    matrix_dir: str
    total_cells: int = 0
    verified_cells: int = 0
    mismatched_cells: int = 0
    skipped_cells: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    verified_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "matrix_id": self.matrix_id,
            "matrix_dir": self.matrix_dir,
            "total_cells": self.total_cells,
            "verified_cells": self.verified_cells,
            "mismatched_cells": self.mismatched_cells,
            "skipped_cells": self.skipped_cells,
            "mismatches": self.mismatches,
            "verified_at": self.verified_at,
            "reproducible": self.mismatched_cells == 0,
        }


def verify_matrix(matrix_dir: str) -> tuple[str, VerifyResult]:
    from .sim.runner import run_lab

    result = load_matrix_result(matrix_dir)

    verify = VerifyResult(
        matrix_id=result.matrix_id,
        matrix_dir=matrix_dir,
        verified_at=time.time(),
    )

    plan_path = os.path.join(matrix_dir, "matrix-plan.json")
    if not os.path.exists(plan_path):
        raise FileNotFoundError(f"no matrix-plan.json in {matrix_dir}")
    with open(plan_path) as f:
        plan = json.load(f)

    plan_scenarios = plan.get("scenarios", [])
    plan_faults = plan.get("faults", [])
    plan_trials = plan.get("trials_per_cell", 0)
    plan_seed_base = plan.get("seed_base", 0)

    for cell_key, cell in result.cells.items():
        scenario, fault_id = cell_key.split("/", 1)
        verify.total_cells += 1

        cell_dir = os.path.join(matrix_dir, cell_key)
        if not os.path.isdir(cell_dir):
            verify.skipped_cells += 1
            continue

        bundle_dirs = sorted(
            d for d in os.listdir(cell_dir) if d.startswith("sim-")
        )
        if len(bundle_dirs) == 0:
            verify.skipped_cells += 1
            continue

        recorded_verdicts = []
        for bundle_name in bundle_dirs:
            result_path = os.path.join(cell_dir, bundle_name, "result.json")
            if not os.path.exists(result_path):
                recorded_verdicts.append("unknown")
                continue
            with open(result_path) as f:
                bundle_result = json.load(f)
            recorded_verdicts.append(bundle_result.get("verdict", "unknown"))

        if len(recorded_verdicts) != plan_trials:
            verify.skipped_cells += 1
            continue

        cell_index = 0
        found = False
        for si, s in enumerate(plan_scenarios):
            for fi, fid in enumerate(plan_faults):
                if f"{s}/{fid}" == cell_key:
                    cell_index = si * len(plan_faults) + fi
                    found = True
                    break
            if found:
                break

        fault_specs = _resolve_fault(fault_id)
        layer_overrides = None
        for fs in fault_specs:
            if fs.fault_type == "layer_swap" and fs.swap_plugin_id:
                if layer_overrides is None:
                    layer_overrides = {}
                layer_overrides[fs.layer] = fs.swap_plugin_id

        replay_dir = os.path.join(cell_dir, "_replay")
        os.makedirs(replay_dir, exist_ok=True)

        replay_verdicts = []
        for trial_idx in range(plan_trials):
            seed = plan_seed_base + cell_index * 1000 + trial_idx
            try:
                _, replay_result = run_lab(
                    scenario,
                    replay_dir,
                    seed=seed,
                    layer_overrides=layer_overrides,
                )
                replay_verdicts.append(replay_result.verdict)
            except Exception:
                replay_verdicts.append("error")

        match = recorded_verdicts == replay_verdicts
        if match:
            verify.verified_cells += 1
        else:
            verify.mismatched_cells += 1
            verify.mismatches.append({
                "cell_key": cell_key,
                "recorded_verdicts": recorded_verdicts,
                "replay_verdicts": replay_verdicts,
            })

    verify_dir = os.path.join(matrix_dir, "verify")
    os.makedirs(verify_dir, exist_ok=True)

    with open(os.path.join(verify_dir, "verify-result.json"), "w") as f:
        json.dump(verify.to_dict(), f, indent=2)
    with open(os.path.join(verify_dir, "verify-report.md"), "w") as f:
        f.write(render_verify_report(verify))

    return verify_dir, verify


def render_verify_report(verify: VerifyResult) -> str:
    lines: list[str] = []
    add = lines.append
    add("NANDA Town Matrix Reproduction Verification")
    add("=" * 55)
    add(f"Matrix:   {verify.matrix_id}")
    add(f"Source:   {verify.matrix_dir}")
    add("")
    add(f"Total cells:      {verify.total_cells}")
    add(f"Verified:         {verify.verified_cells}")
    add(f"Mismatched:       {verify.mismatched_cells}")
    add(f"Skipped:          {verify.skipped_cells}")
    add("")

    if verify.mismatched_cells == 0 and verify.verified_cells > 0:
        add("RESULT: REPRODUCIBLE")
        add("All verified cells produced identical verdicts when replayed"
            " with the same seeds.")
    elif verify.mismatched_cells > 0:
        add("RESULT: NOT REPRODUCIBLE")
        add("Some cells produced different verdicts on replay.")
        add("")
        add("Mismatches:")
        for m in verify.mismatches:
            add(f"  {m['cell_key']}:")
            add(f"    recorded: {m['recorded_verdicts']}")
            add(f"    replay:   {m['replay_verdicts']}")
    else:
        add("RESULT: INCONCLUSIVE")
        add("No cells could be verified (missing data).")

    add("")
    add("Verification replays each trial with the same seed and compares"
        " verdicts.")
    add("Run IDs and bundle paths differ; only logical verdicts must match.")
    return "\n".join(lines) + "\n"


def render_heatmap(result: MatrixResult) -> str:
    scenarios = result.scenarios
    faults = result.faults

    cells_html = ""
    for si, scenario in enumerate(scenarios):
        for fi, fault_id in enumerate(faults):
            cell_key = f"{scenario}/{fault_id}"
            cell = result.cells.get(cell_key)
            if cell is None:
                continue
            rate = cell.stats.pass_rate
            r = int(255 * (1 - rate))
            g = int(255 * rate)
            color = f"rgb({r},{g},60)"
            det = "!" if cell.stats.non_deterministic else ""
            tooltip = (
                f"{scenario}/{fault_id}: "
                f"{cell.passes}P/{cell.violations}F/"
                f"{cell.errors}E/{cell.incompletes}I "
                f"({rate:.0%}){det}"
            )
            cells_html += (
                f'<div class="cell" style="background:{color}" '
                f'title="{html.escape(tooltip)}">'
                f'<span class="rate">{rate:.0%}</span>'
                f'<span class="det">{det}</span>'
                f'</div>\n'
            )

    scenario_labels = "".join(
        f'<div class="row-label">{html.escape(s)}</div>\n'
        for s in scenarios
    )
    fault_labels = "".join(
        f'<div class="col-label">{html.escape(f)}</div>\n'
        for f in faults
    )

    stats_rows = ""
    for s in scenarios:
        for f in faults:
            cell_key = f"{s}/{f}"
            cell = result.cells.get(cell_key)
            if cell is None:
                continue
            stats_rows += (
                f'<tr><td>{html.escape(f"{s}/{f}")}</td>'
                f'<td>{cell.stats.pass_rate:.0%}</td>'
                f'<td>[{cell.stats.ci_lower:.0%},{cell.stats.ci_upper:.0%}]</td>'
                f'<td>{cell.violations}</td>'
                f'<td>{"yes" if cell.stats.non_deterministic else "no"}</td></tr>\n'
            )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Failure Matrix: {html.escape(result.matrix_id)}</title>
<style>
body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
h1 {{ color: #00d4ff; }}
.grid {{ display: grid; grid-template-columns: 200px repeat({len(faults)}, 1fr); gap: 2px; }}
.row-label {{ padding: 8px; display: flex; align-items: center; font-size: 13px; }}
.col-label {{ padding: 8px; text-align: center; font-size: 11px; writing-mode: vertical-lr; transform: rotate(180deg); }}
.cell {{ padding: 8px; text-align: center; border-radius: 4px; cursor: pointer; position: relative; }}
.cell:hover {{ outline: 2px solid white; }}
.rate {{ font-weight: bold; font-size: 14px; }}
.det {{ color: #ff0; font-size: 10px; margin-left: 2px; }}
.legend {{ margin-top: 20px; font-size: 12px; }}
.bar {{ display: inline-block; width: 40px; height: 12px; margin-right: 8px; vertical-align: middle; }}
.stats {{ margin-top: 20px; }}
.stats table {{ border-collapse: collapse; }}
.stats td, .stats th {{ padding: 4px 12px; border: 1px solid #333; font-size: 12px; }}
.stats th {{ background: #16213e; }}
</style>
</head>
<body>
<h1>Protocol Failure Matrix</h1>
<p>Matrix: {html.escape(result.matrix_id)} | Trials/cell: {result.trials_per_cell} | Seed: {result.seed_base}</p>
<div class="grid">
<div></div>
{fault_labels}
{scenario_labels}
{cells_html}
</div>
<div class="legend">
<p><span class="bar" style="background:rgb(0,255,60)"></span>100% pass
<span class="bar" style="background:rgb(128,127,60)"></span>50%
<span class="bar" style="background:rgb(255,0,60)"></span>0% fail
<span style="color:#ff0">!</span> = non-deterministic (same inputs, different outcomes)</p>
</div>
<div class="stats">
<h2>Statistics</h2>
<table>
<tr><th>Cell</th><th>Pass Rate</th><th>95% CI</th><th>Violations</th><th>Non-det</th></tr>
{stats_rows}</table>
</div>
</body>
</html>"""
