"""One incorrect declaration must affect discovery, never physical execution."""

import json

import pytest

from nandatown.bundle import verify_bundle
from nandatown.experiments.resilience.evaluator import (
    analyze, evaluate, evaluate_legacy, metrics,
)
from nandatown.experiments.resilience.experiment import run_experiment
from nandatown.experiments.resilience.model import Settings, build_spec, make_world
from nandatown.sim.runner import build_engine
from nandatown.sim.validators import Trace


@pytest.fixture(scope="module")
def incorrect_run():
    spec = build_spec(Settings(condition="shared-incorrect", seed=7))
    engine = build_engine(spec)
    engine.run()
    return spec, engine


def test_incorrect_case_changes_one_declaration_not_physical_world(incorrect_run):
    spec, engine = incorrect_run
    baseline = Settings(condition="shared", seed=7)
    truthful = make_world(baseline)
    incorrect = make_world(Settings(condition="shared-incorrect", seed=7))
    assert incorrect.providers == truthful.providers
    assert incorrect.dependency_outages == truthful.dependency_outages
    actual_agents = {agent.name: agent for agent in spec.agents}
    for agent in build_spec(baseline).agents:
        if agent.name not in {"experiment", "provider-00"}:
            assert actual_agents[agent.name] == agent
    config = actual_agents["provider-00"].config
    assert config["dependency"] == "backend-0"
    assert config["declared_dependency"] == "backend-1"
    selected = [e for e in engine.events if e.kind == "resilience.selected"
                and e.detail["provider"] == "provider-00"]
    assert selected
    assert all(e.detail["visible_dependency"] == "backend-1" for e in selected)
    requests = [e for e in engine.events if e.kind == "message_sent"
                and e.observer == "provider-00" and e.detail["kind"] == "compute"]
    assert requests
    assert all(e.detail["to"] == "backend-0" for e in requests)
    results = [e for e in engine.events if e.kind == "resilience.backend_result"
               and e.detail["provider"] == "provider-00" and 12 <= e.at < 28]
    assert results
    assert all(e.observer == "backend-0" and not e.detail["success"] for e in results)
    assert evaluate(spec, Trace(engine.events, engine.run_id))[0].status == "passed"


def test_incorrect_case_preserves_price_and_oracle_controls(incorrect_run):
    spec, engine = incorrect_run
    incorrect = metrics(analyze(spec, engine.events, engine.run_id))
    truthful = run_experiment(Settings(seed=7))
    for policy in ("price", "random", "load", "oracle"):
        before = run_experiment(Settings(policy=policy, seed=7))
        after = run_experiment(Settings(policy=policy, condition="shared-incorrect", seed=7))
        for key in ("completed", "completion_rate", "stress_completion_rate",
                    "attempts_per_task", "total_spend_cents", "cost_per_success_cents",
                    "timeline"):
            assert after[key] == before[key]
    assert incorrect["completion_rate"] == truthful["completion_rate"] == 1.0
    assert incorrect["stress_completion_rate"] == truthful["stress_completion_rate"] == 1.0
    assert incorrect["attempts_per_task"] == 1.3
    assert truthful["attempts_per_task"] == 1.1
    assert incorrect["cost_per_success_cents"] == 13.45
    assert truthful["cost_per_success_cents"] == 13.3
    assert incorrect["total_spend_cents"] == 2152
    assert truthful["total_spend_cents"] == 2128
    assert incorrect["repeated_dependency_retries"] == 24
    assert truthful["repeated_dependency_retries"] == 0
    assert incorrect["total_spend_cents"] == sum(
        e.detail["cents"] for e in engine.events if e.kind == "payment_settled")


def test_incorrect_case_cannot_leak_true_label_into_selection(incorrect_run):
    spec, engine = incorrect_run
    events = [e.model_copy(deep=True) for e in engine.events]
    selected = next(e for e in events if e.kind == "resilience.selected"
                    and e.detail["provider"] == "provider-00")
    selected.detail["visible_dependency"] = "backend-0"
    assert evaluate(spec, Trace(events, engine.run_id))[0].status == "failed"


def test_incorrect_case_bundle_reproduction(tmp_path):
    settings = Settings(condition="shared-incorrect", seed=7)
    out = tmp_path / "incorrect"
    first = run_experiment(settings, out)
    second = run_experiment(settings)
    assert {k: v for k, v in first.items() if k != "bundle"} == {
        k: v for k, v in second.items() if k != "bundle"}
    assert verify_bundle(str(out)) == []
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert "--condition shared-incorrect" in run["config"]["rerun_command"]


def test_undisclosed_incorrect_label_is_not_visible():
    rows = [run_experiment(Settings(condition=condition, disclosure=0.0))
            for condition in ("shared", "shared-incorrect")]
    for key in ("completion_rate", "attempts_per_task", "total_spend_cents", "timeline"):
        assert rows[0][key] == rows[1][key]


def test_legacy_evaluator_keeps_truthful_semantics(incorrect_run):
    spec = build_spec(Settings())
    spec.validator = "resilience.v2"
    engine = build_engine(spec)
    engine.run()
    result = evaluate_legacy(spec, Trace(engine.events, engine.run_id))[0]
    assert result.status == "passed"
    assert result.name == "resilience.v2"
    incorrect_spec, incorrect_engine = incorrect_run
    rejected = evaluate_legacy(
        incorrect_spec, Trace(incorrect_engine.events, incorrect_engine.run_id))[0]
    assert rejected.status == "failed"
    assert "require resilience.v3" in rejected.note
