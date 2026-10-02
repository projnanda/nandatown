"""Run one Lab scenario end to end and leave an evidence bundle.

Redaction happens before evaluation, so the recorded result is exactly
reproducible from the exported records by anyone, with nothing private
required.
"""

from __future__ import annotations

import os
import sys

from .. import __version__
from ..bundle import write_bundle
from ..records import RunRecord, TownEvent, fingerprint
from .agents import ROLES
from .api import TownAPI
from .engine import Engine
from .scenario import ScenarioSpec, load_bundled, load_scenario_file
from .validators import LAB_EVALUATOR_VERSION, evaluate_scenario


class LabError(Exception):
    pass


def build_engine(spec: ScenarioSpec) -> Engine:
    engine = Engine(spec)
    payments = engine.layers["payments"]
    for a in spec.agents:
        cls = ROLES.get(a.role)
        if cls is None:
            raise LabError(f"unknown role {a.role!r};"
                           f" known: {sorted(ROLES)}")
        api = TownAPI(engine, a.name)
        engine.add_agent(cls(a.name, dict(a.config), api))
        payments.open_account(a.name, a.config.get("balance_cents", 0))
    return engine


def load_plugin_files(paths: list[str]) -> None:
    """Import the user's plugin files so their @register and @validator
    decorators run. These are the user's own local files; running a
    scenario that names them is running their code by request."""
    import importlib.util

    for i, path in enumerate(paths):
        spec = importlib.util.spec_from_file_location(
            f"nandatown_user_plugin_{i}", path)
        if spec is None or spec.loader is None:
            raise LabError(f"cannot load plugin file {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


def resolve_spec(name_or_path: str) -> ScenarioSpec:
    if name_or_path.endswith((".yaml", ".yml")) or os.sep in name_or_path:
        if os.path.exists(name_or_path):
            spec = load_scenario_file(name_or_path)
            load_plugin_files(spec.plugin_files)
            return spec
        # Try as bundled scenario by stripping path prefix
        name = os.path.splitext(os.path.basename(name_or_path))[0]
        return load_bundled(name)
    return load_bundled(name_or_path)


def run_lab(name_or_path: str, out_dir: str,
            seed: int | None = None,
            plugins: list[str] | None = None,
            layer_overrides: dict[str, str] | None = None
            ) -> tuple[str, object]:
    spec = resolve_spec(name_or_path)
    if seed is not None:
        spec.seed = seed
    if plugins:
        load_plugin_files([os.path.abspath(p) for p in plugins])
        spec.plugin_files = list(spec.plugin_files) + list(plugins)
    if layer_overrides:
        for layer, plugin_id in layer_overrides.items():
            if layer not in spec.layers:
                raise LabError(f"unknown layer {layer!r}")
            spec.layers[layer] = plugin_id
    engine = build_engine(spec)
    engine.run()

    privacy = engine.layers["privacy"]
    events = [TownEvent.model_validate(privacy.redact(e.model_dump()))
              for e in engine.events]
    intents = [privacy.redact(i) for i in engine.intents]
    public_spec = ScenarioSpec.model_validate(privacy.redact(
        spec.model_dump()))

    result = evaluate_scenario(public_spec, engine.run_id, events)
    rerun = f"nandatown run {name_or_path} --seed {spec.seed}"
    for path in plugins or []:
        rerun += f" --plugin {path}"
    for layer, plugin_id in (layer_overrides or {}).items():
        rerun += f" --layer {layer}={plugin_id}"
    run_record = RunRecord(
        run_id=engine.run_id,
        profile_name=spec.name,
        profile_fingerprint=fingerprint(public_spec.model_dump()),
        created_at=0.0,
        participants=[{"name": a.name, "role": a.role} for a in spec.agents],
        releases={
            "nandatown": __version__,
            "evaluator": LAB_EVALUATOR_VERSION,
            "python": sys.version.split()[0],
        },
        config={"mode": "lab", "seed": spec.seed,
                "layers": spec.layers,
                "logical_time": engine.now,
                "rerun_command": rerun},
    )
    bundle_dir = os.path.join(out_dir, engine.run_id)
    write_bundle(bundle_dir, public_spec, run_record, intents, events,
                 result, mode="lab")
    from ..bundle import attest_bundle
    attest_bundle(bundle_dir)
    return bundle_dir, result
