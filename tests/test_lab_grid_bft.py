"""Byzantine broadcast over a frequency-regulation action.

n=4 grid nodes, tolerance f=(n-1)//3=1. Below the boundary, honest
nodes must agree (and, with an honest proposer, agree on what it
proposed) even when the proposer or a voter equivocates. Past the
boundary (two Byzantine nodes at once), the agreement stage is
expected to fail: that failure is the evidence the boundary was
actually crossed, not a town malfunction.
"""

import pytest

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.scenario import load_bundled
from nandatown.sim.validators import Trace, grid_bft


def run(name):
    spec = load_bundled(name)
    engine = build_engine(spec)
    engine.run()
    return spec, Trace(engine.events, engine.run_id)


def stage(spec, trace, name):
    return next(s for s in grid_bft(spec, trace) if s.name == name)


@pytest.mark.parametrize("name", [
    "grid_bft_byzantine_voter",
    "grid_bft_byzantine_controller",
])
def test_agreement_holds_within_declared_tolerance(name):
    spec, trace = run(name)
    assert stage(spec, trace, "participation").status == "passed"
    assert stage(spec, trace, "byzantine_behavior_injected").status == "passed"
    assert stage(spec, trace, "agreement").status == "passed"


def test_validity_holds_with_an_honest_equivocating_voter():
    spec, trace = run("grid_bft_byzantine_voter")
    assert stage(spec, trace, "validity").status == "passed"


def test_no_validity_stage_when_the_proposer_itself_is_byzantine():
    spec, trace = run("grid_bft_byzantine_controller")
    names = {s.name for s in grid_bft(spec, trace)}
    assert "validity" not in names


def test_honest_nodes_converge_on_one_value_despite_a_lying_voter():
    spec, trace = run("grid_bft_byzantine_voter")
    committed = trace.find("value_committed")
    honest = {"frequency-controller", "substation-2", "substation-3"}
    values = {e.detail["value"] for e in committed if e.observer in honest}
    assert values == {"shed_load_5mw"}


def test_honest_nodes_converge_despite_an_equivocating_proposer():
    spec, trace = run("grid_bft_byzantine_controller")
    committed = trace.find("value_committed")
    honest = {"substation-1", "substation-2", "substation-3"}
    values = {e.detail["value"] for e in committed if e.observer in honest}
    assert len(values) == 1


def test_agreement_fails_once_byzantine_nodes_exceed_the_boundary():
    spec, trace = run("grid_bft_double_fault")
    agreement = stage(spec, trace, "agreement")
    assert agreement.status == "failed"
    committed = trace.find("value_committed")
    honest = {"substation-2", "substation-3"}
    values = {e.detail["value"] for e in committed if e.observer in honest}
    assert values == {"shed_load_5mw", "inject_reserve_3mw"}


def test_no_byzantine_node_configured_is_reported_as_such():
    spec, trace = run("grid_bft_byzantine_voter")
    spec = spec.model_copy(deep=True)
    for agent in spec.agents:
        agent.config.pop("equivocation", None)
    injected = stage(spec, trace, "byzantine_behavior_injected")
    assert injected.status == "not_tested"
    assert "no Byzantine node is configured" in injected.note


def test_empty_trace_does_not_certify_participation():
    spec, _ = run("grid_bft_byzantine_voter")
    assert stage(spec, Trace([]), "participation").status == "not_enough_evidence"


@pytest.mark.parametrize("name,verdict", [
    ("grid_bft_byzantine_voter", "passed"),
    ("grid_bft_byzantine_controller", "passed"),
    ("grid_bft_double_fault", "failed"),
])
def test_exported_bundle_replays_and_verifies(name, verdict, tmp_path):
    bundle, result = run_lab(name, str(tmp_path))
    assert result.verdict == verdict
    assert verify_bundle(bundle) == []
