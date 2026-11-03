"""Regressions for the opt-in upstream integration, beyond the imported cases."""

import json
import subprocess
import sys
from pathlib import Path

from nandatown.experiments.resilience import (
    SOURCE_EXPERIMENT_VERSION, UPSTREAM_COMPATIBILITY_BASELINE,
)


def invoke(*args, cwd):
    result = subprocess.run(
        [sys.executable, *args], cwd=cwd, text=True, capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_registration_is_explicit_and_preserves_defaults(tmp_path):
    invoke("-c", """
import sys
from nandatown.layers import DEFAULT_PLUGINS, plugins
from nandatown.sim.agents import ROLES
from nandatown.sim.validators import VALIDATORS

before = plugins(), dict(ROLES), dict(VALIDATORS), dict(DEFAULT_PLUGINS)
import nandatown.experiments
import nandatown.experiments.resilience
assert before == (plugins(), dict(ROLES), dict(VALIDATORS), dict(DEFAULT_PLUGINS))
assert "resilience.v2" not in VALIDATORS
from nandatown.experiments.resilience import plugin
assert {p["plugin_id"] for p in plugins()["coordination"]} - {
    p["plugin_id"] for p in before[0]["coordination"]
} == {f"resilience.{p}.v1" for p in ("price", "random", "load", "dependency", "oracle")}
assert set(ROLES) - before[1].keys() == {
    "resilience_clock", "resilience_backend", "resilience_provider", "resilience_buyer"
}
assert set(VALIDATORS) - before[2].keys() == {"resilience.v2"}
assert DEFAULT_PLUGINS == before[3]
for layer, entries in before[0].items():
    assert all(entry in plugins()[layer] for entry in entries)
assert "nanda_resilience" not in sys.modules
""", cwd=tmp_path)


def test_module_run_and_fresh_process_verification(tmp_path):
    out = tmp_path / "bundle"
    row = json.loads(invoke(
        "-m", "nandatown.experiments.resilience", "run",
        "--buyers", "1", "--out", str(out), cwd=tmp_path,
    ))
    verified = json.loads(invoke(
        "-m", "nandatown.experiments.resilience", "verify", str(out), cwd=tmp_path,
    ))
    assert verified["verified"] is True
    assert verified["metrics"]["completed"] == row["completed"]
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    releases = run["releases"]
    assert releases["nandatown_compatibility_baseline"] == UPSTREAM_COMPATIBILITY_BASELINE
    assert releases["source_experiment_version"] == SOURCE_EXPERIMENT_VERSION
    assert "nandatown_commit" not in releases
    assert run["config"]["rerun_command"].startswith("nandatown-resilience run ")
    assert not list(tmp_path.glob(".resilience-keys-*"))
    assert not (out / "profile.json").read_text(encoding="utf-8").count("nanda_resilience")


def test_scenario_prints_installed_plugin_path(tmp_path):
    import nandatown.experiments.resilience as resilience

    output = invoke(
        "-m", "nandatown.experiments.resilience", "scenario",
        "--out", str(tmp_path / "scenario.yaml"), cwd=tmp_path,
    )
    plugin = Path(resilience.__file__).with_name("plugin.py")
    assert f'--plugin "{plugin}"' in output
    assert plugin.is_file()


def test_campaign_artifacts_and_provenance(tmp_path):
    from nandatown.experiments.resilience.campaign import run_campaign

    out = tmp_path / "campaign"
    summary = run_campaign(out, seeds=[0], disclosures=[1.0])
    assert len(summary["runs"]) == 15
    assert len(list((out / "bundles").iterdir())) == 15
    assert summary["source_experiment_version"] == SOURCE_EXPERIMENT_VERSION
    assert summary["upstream_compatibility_baseline"] == UPSTREAM_COMPATIBILITY_BASELINE
    assert "upstream_commit" not in summary
    assert summary["nandatown_version"]
    assert json.loads((out / "summary.json").read_text(encoding="utf-8")) == summary
    assert len((out / "runs.csv").read_text(encoding="utf-8").splitlines()) == 16
    report = (out / "report.html").read_text(encoding="utf-8")
    assert "not the executing revision" in report
    assert "Source experiment version" in report
    assert "counterexample matters" in report
