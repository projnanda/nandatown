# SPDX-License-Identifier: MIT
# Copyright (c) 2026 nanda-resilience contributors
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.validators import Trace

from nandatown.experiments.resilience import EVALUATOR, agents  # noqa: F401
from nandatown.experiments.resilience.campaign import bootstrap_interval, summarize
from nandatown.experiments.resilience.evaluator import analyze, evaluate, metrics
from nandatown.experiments.resilience.experiment import run_experiment
from nandatown.experiments.resilience.model import CONDITIONS, POLICIES, Settings, build_spec, make_world
from nandatown.experiments.resilience.policies import Candidate, choose
from nandatown.experiments.resilience.report import render_html


@pytest.fixture(scope="module")
def shared_run():
    spec = build_spec(Settings())
    engine = build_engine(spec)
    engine.run()
    return spec, engine


@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("disclosure", [0.0, 0.5, 1.0])
def test_all_policy_condition_disclosure_combinations(condition, policy, disclosure):
    row = run_experiment(Settings(
        condition=condition, policy=policy, disclosure=disclosure, seed=19))
    assert row["tasks"] == 160
    assert 0 <= row["completion_rate"] <= 1
    assert 1 <= row["attempts_per_task"] <= 3
    if condition == "clean":
        assert row["completion_rate"] == 1
        assert row["recovery_after_outage"] is None


def test_shared_outage_has_measurable_effect_and_cost():
    price = run_experiment(Settings(policy="price"))
    aware = run_experiment(Settings(policy="dependency"))
    random = run_experiment(Settings(policy="random"))
    # The last stress-window cohort can succeed on retry after recovery at t=28.
    assert price["stress_completion_rate"] == 0.125
    assert aware["stress_completion_rate"] >= 0.9
    assert aware["attempts_per_task"] < price["attempts_per_task"]
    assert aware["cost_per_success_cents"] > price["cost_per_success_cents"]
    assert random["stress_completion_rate"] > price["stress_completion_rate"]
    assert aware["dependency_hhi_before_stress"] < price["dependency_hhi_before_stress"]


def test_zero_disclosure_does_not_leak_true_groups():
    hidden = run_experiment(Settings(policy="dependency", disclosure=0.0))
    oracle = run_experiment(Settings(policy="oracle", disclosure=0.0))
    assert hidden["stress_completion_rate"] < oracle["stress_completion_rate"]


def test_full_disclosure_matches_information_oracle():
    aware = run_experiment(Settings(policy="dependency"))
    oracle = run_experiment(Settings(policy="oracle"))
    for key in ("completion_rate", "attempts_per_task", "total_spend_cents",
                "dependency_hhi_before_stress", "timeline"):
        assert aware[key] == oracle[key]


def test_repeatability():
    first = run_experiment(Settings(seed=123))
    second = run_experiment(Settings(seed=123))
    assert first == second


def test_fault_schedule_is_paired_and_disclosure_masks_are_nested():
    worlds = [make_world(Settings(policy=p, condition="independent", seed=3))
              for p in POLICIES]
    assert all(w.providers == worlds[0].providers for w in worlds)
    partial = make_world(Settings(condition="independent", seed=3, disclosure=0.5))
    assert {p.name for p in partial.providers if p.disclosed} <= {
        p.name for p in worlds[0].providers if p.disclosed}
    assert [p.outages for p in partial.providers] == [p.outages for p in worlds[0].providers]
    shared = make_world(Settings(condition="shared"))
    independent = worlds[0]
    shared_downtime = sum(
        end - start for p in shared.providers
        for start, end in shared.dependency_outages[p.dependency])
    independent_downtime = sum(
        end - start for p in independent.providers for start, end in p.outages)
    assert shared_downtime == independent_downtime == 144


@pytest.mark.parametrize("settings", [
    {"disclosure": -0.1}, {"disclosure": 1.1}, {"disclosure": float("nan")},
    {"disclosure": float("inf")}, {"seed": True}, {"buyers": 0},
    {"attempts": 13}, {"policy": "unknown"}, {"rounds": 1},
])
def test_invalid_settings_rejected(settings):
    with pytest.raises(ValidationError):
        Settings(**settings)


def test_selection_avoids_failed_group_not_just_failed_identity():
    candidates = [Candidate("cheap", 10, 0, "A"), Candidate("backup", 12, 0, "B")]
    kwargs = dict(seed=1, task="task", attempt=2, inflight=Counter(),
                  reservations={}, failed_groups={"A"})
    assert choose("price", candidates, **kwargs).name == "cheap"
    assert choose("dependency", candidates, **kwargs).name == "backup"


def test_unknown_dependencies_are_not_treated_as_independent():
    candidates = [Candidate("cheap", 10, 0, None), Candidate("known", 12, 0, "B")]
    result = choose("dependency", candidates, seed=1, task="task", attempt=1,
                    inflight=Counter(), reservations={"other": "unknown"},
                    failed_groups=set())
    assert result.name == "known"


def test_policy_selection_is_invariant_to_candidate_order():
    candidates = [Candidate(str(i), 10, 0, "A") for i in range(10)]
    for policy in POLICIES:
        kwargs = dict(seed=9, task="task", attempt=1, inflight=Counter(),
                      reservations={}, failed_groups=set())
        assert choose(policy, candidates, **kwargs) == choose(
            policy, list(reversed(candidates)), **kwargs)


def test_empty_candidates_is_error():
    with pytest.raises(ValueError, match="empty"):
        choose("price", [], seed=1, task="task", attempt=1,
               inflight=Counter(), reservations={}, failed_groups=set())


@pytest.mark.parametrize("kind,field,value", [
    ("resilience.task_finished", "success", False),
    ("resilience.task_finished", "success", 1),
    ("resilience.task_finished", "attempts", 99),
    ("resilience.task_finished", "price_cents", 999),
    ("resilience.task_finished", "latency", -1),
    ("resilience.selected", "visible_dependency", "invented"),
    ("resilience.selected", "provider", "provider-99"),
    ("resilience.backend_result", "success", False),
    ("resilience.backend_admitted", "active", 1000),
    ("resilience.provider_result", "buyer", "wrong-buyer"),
    ("payment_settled", "to", "wrong-payee"),
    ("payment_settled", "cents", 1),
    ("resilience.availability", "down", False),
])
def test_tampered_evidence_is_rejected(shared_run, kind, field, value):
    spec, engine = shared_run
    events = [e.model_copy(deep=True) for e in engine.events]
    target = next(e for e in events if e.kind == kind)
    target.detail[field] = value
    result = evaluate(spec, Trace(events, engine.run_id))
    assert result[0].status == "failed", result[0].note


@pytest.mark.parametrize("kind", [
    "resilience.selected", "resilience.backend_admitted",
    "resilience.backend_result", "resilience.provider_result",
    "resilience.task_created", "resilience.task_finished",
    "resilience.attempt_finished", "payment_settled", "message_delivered",
    "card_registered",
])
def test_missing_evidence_is_not_a_pass(shared_run, kind):
    spec, engine = shared_run
    events = list(engine.events)
    target = next((e for e in events if e.kind == kind
                   and (kind != "card_registered" or e.subject.startswith("provider-"))))
    events.remove(target)
    assert evaluate(spec, Trace(events, engine.run_id))[0].status == "failed"


def test_partial_trace_is_incomplete(shared_run):
    spec, engine = shared_run
    result = evaluate(spec, Trace(engine.events[:100], engine.run_id))
    assert result[0].status == "not_enough_evidence"


def test_duplicate_and_cross_run_events_rejected(shared_run):
    spec, engine = shared_run
    assert evaluate(spec, Trace(engine.events + [engine.events[-1]],
                                engine.run_id))[0].status == "failed"
    events = [e.model_copy(deep=True) for e in engine.events]
    events[50].run_id = "other-run"
    assert evaluate(spec, Trace(events, engine.run_id))[0].status == "failed"


@pytest.mark.parametrize("kind", [
    "resilience.backend_admitted", "resilience.backend_result",
    "resilience.provider_result", "resilience.attempt_finished",
])
def test_reordered_causal_events_rejected(shared_run, kind):
    spec, engine = shared_run
    events = [e.model_copy(deep=True) for e in engine.events]
    target = next(e for e in events if e.kind == kind)
    target.at = 0.0
    events.sort(key=lambda e: e.at)
    assert evaluate(spec, Trace(events, engine.run_id))[0].status == "failed"


def test_fault_and_population_mutation_rejected(shared_run):
    spec, engine = shared_run
    altered = spec.model_copy(deep=True)
    next(a for a in altered.agents if a.role == "resilience_backend").config["capacity"] = 100
    assert evaluate(altered, Trace(engine.events, engine.run_id))[0].status == "failed"


def test_capacity_pressure_is_measured():
    row = run_experiment(Settings(dependency_capacity=1, condition="clean", attempts=1))
    assert row["attempts_per_task"] == 1
    assert row["completion_rate"] < 1


def test_provider_local_outage_does_not_poison_dependency_group():
    from nandatown.experiments.resilience.agents import BuyerAgent, Task
    class FakeAPI:
        def observe(self, *args):
            pass

        def later(self, delay, callback):
            pass

    buyer = BuyerAgent("buyer", {"attempts": 3}, FakeAPI())
    buyer.tasks = {
        "task": Task(round=0, created=0, tried={"provider"}, ref="attempt",
                     provider="provider", group="shared"),
    }
    buyer.handle_service_result({
        "sender": "provider",
        "body": {"task": "task", "ref": "attempt", "success": False,
                 "reason": "provider_unavailable"},
    })
    assert buyer.tasks["task"].failed_groups == set()
    buyer.handle_service_result({
        "sender": "provider",
        "body": {"task": "task", "ref": "attempt", "success": False,
                 "reason": "unavailable"},
    })
    assert buyer.tasks["task"].failed_groups == {"shared"}


def test_bundle_is_portable_and_verified(tmp_path: Path):
    out = tmp_path / "evidence"
    row = run_experiment(Settings(), out)
    assert row["bundle"] == "evidence"
    assert verify_bundle(str(out)) == []
    assert (out / "attestation.json").exists()
    assert not list(out.rglob("*private*"))
    with pytest.raises(FileExistsError):
        run_experiment(Settings(), out)


def test_native_nandatown_plugin_integration(tmp_path: Path, monkeypatch):
    import yaml
    spec = build_spec(Settings(policy="price"))
    recipe = tmp_path / "scenario.yaml"
    recipe.write_text(yaml.safe_dump(spec.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.setenv("NANDATOWN_HOME", str(tmp_path / "keys"))
    plugin = Path(agents.__file__).with_name("plugin.py")
    bundle, result = run_lab(str(recipe), str(tmp_path / "native"),
                             plugins=[str(plugin)],
                             layer_overrides={"coordination": "resilience.dependency.v1"})
    assert result.verdict == "passed"
    assert verify_bundle(bundle) == []


def test_metrics_are_recomputed_from_trace(shared_run):
    spec, engine = shared_run
    row = metrics(analyze(spec, engine.events))
    assert row["tasks"] == 160
    assert row["total_spend_cents"] == sum(
        e.detail["cents"] for e in engine.events if e.kind == "payment_settled")
    assert row["p95_success_latency"] > 0


def test_bootstrap_and_pairing():
    assert bootstrap_interval([1], 0) is None
    assert bootstrap_interval([2, 2, 2], 0) == [2.0, 2.0]
    rows = [
        run_experiment(Settings(seed=seed, policy=policy))
        for seed in (1, 2) for policy in ("price", "dependency")
    ]
    summary = summarize(rows)
    effect = next(r for r in summary["paired"] if r["metric"] == "stress_completion_rate")
    assert effect["paired_seeds"] == 2
    assert effect["mean_difference"] > 0


def test_report_is_self_contained_and_honest(shared_run):
    spec, engine = shared_run
    row = metrics(analyze(spec, engine.events))
    data = {
        "version": "test", "upstream_compatibility_baseline": "abc",
        "nandatown_version": "test", "source_experiment_version": "0.1.0",
        "design": {"disclosures": [1.0], "seeds": [7]},
        "runs": [row], **summarize([row]),
    }
    report = render_html(data)
    assert "shared reservation coordinator" in report
    assert "dependency_hhi" not in report
    assert "<script" not in report
    assert 'src="http' not in report
    assert "not a proven optimum" in report
