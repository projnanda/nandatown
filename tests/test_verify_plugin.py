"""`nandatown verify --plugin`: replaying a bundle judged by a plugin validator."""

import pytest

from nandatown.cli import main
from nandatown.new import SCENARIO_TEMPLATE
from nandatown.sim.runner import run_lab
from nandatown.sim.validators import VALIDATORS

VALIDATOR = "verify_plugin_demo"
PLUGIN = '''
from nandatown.records import StageResult
from nandatown.sim.validators import validator


@validator("verify_plugin_demo")
def verify_plugin_demo(spec, trace):
    return [StageResult(name="verify_plugin_demo", status="passed",
                        note="judged by a plugin file")]
'''


@pytest.fixture
def plugin_bundle(tmp_path):
    """A Lab bundle judged by a plugin validator, seen from a fresh process
    that has not imported the plugin (its validator is not registered)."""
    plugin = tmp_path / "demo_plugin.py"
    plugin.write_text(PLUGIN)
    commented = "# plugin_files:\n#   - demo_plugin.py\n# validator: demo"
    template = SCENARIO_TEMPLATE.format(name="demo")
    assert commented in template
    scenario = tmp_path / "demo.yaml"
    scenario.write_text(template.replace(
        commented, f"plugin_files:\n  - demo_plugin.py\nvalidator: {VALIDATOR}"))
    bundle_dir, result = run_lab(str(scenario), str(tmp_path / "runs"))
    assert any(stage.name == VALIDATOR for stage in result.stages)
    VALIDATORS.pop(VALIDATOR)
    yield bundle_dir, plugin
    VALIDATORS.pop(VALIDATOR, None)


def test_verify_without_the_plugin_names_the_missing_validator(plugin_bundle, capsys):
    bundle_dir, _ = plugin_bundle
    assert main(["verify", bundle_dir]) == 1
    out = capsys.readouterr().out
    assert "evaluator replay mismatch" in out
    assert VALIDATOR in out and "--plugin" in out


def test_verify_with_the_plugin_replays_it(plugin_bundle, capsys):
    bundle_dir, plugin = plugin_bundle
    assert main(["verify", bundle_dir, "--plugin", str(plugin)]) == 0
    assert "bundle verified" in capsys.readouterr().out


def test_verify_refuses_a_missing_plugin_file(plugin_bundle, tmp_path, capsys):
    bundle_dir, _ = plugin_bundle
    assert main(["verify", bundle_dir, "--plugin", str(tmp_path / "nope.py")]) == 2
    assert "plugin file not found" in capsys.readouterr().out
