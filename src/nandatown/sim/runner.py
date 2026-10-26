"""Run one Lab scenario end to end and leave an evidence bundle.

Records are first reduced to the JSON data the bundle files will hold,
then redacted, then evaluated, so the recorded result is exactly
reproducible from the exported records by anyone, with nothing private
required. The run record is built from the redacted scenario too.
"""

from __future__ import annotations

import math
import os
import sys
from typing import Any

from .. import __version__
from ..bundle import write_bundle
from ..layers.privacy import RedactionCollision
from ..records import RunRecord, TownEvent, fingerprint
from .agents import ROLES
from .api import TownAPI
from .engine import Engine
from .scenario import ScenarioSpec, load_bundled, load_scenario_file
from .validators import LAB_EVALUATOR_VERSION, evaluate_scenario


class LabError(Exception):
    pass


class UnsupportedValue(ValueError):
    """A record holds a value that JSON evidence cannot record exactly.

    The run is refused before any file is written. The message names the
    record and the value's type, never the value.
    """


def _json_data(value: Any, where: str) -> Any:
    """The value as the JSON data its bundle file will hold.

    A tuple becomes a list, as JSON writes it. A set, bytes, a non-text
    key, NaN or infinity would be written in a different form from the
    one redaction and evaluation saw, or not at all, so it is refused.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise UnsupportedValue(
            f"{where} holds a non-finite number, which JSON evidence"
            " cannot record")
    if isinstance(value, (list, tuple)):
        return [_json_data(v, where) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise UnsupportedValue(
                    f"{where} has a key of type {type(k).__name__};"
                    " JSON evidence keys must be text")
            out[k] = _json_data(v, where)
        return out
    raise UnsupportedValue(
        f"{where} holds a value of type {type(value).__name__}, which"
        " JSON evidence cannot record")


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
        spec = load_scenario_file(name_or_path)
        load_plugin_files(spec.plugin_files)
        return spec
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
    events = [TownEvent.model_validate(privacy.redact(
                  _json_data(e.model_dump(), f"event {e.event_id}")))
              for e in engine.events]
    intents = [privacy.redact(_json_data(i, f"intent {i['intent_id']}"))
               for i in engine.intents]
    public_spec = ScenarioSpec.model_validate(privacy.redact(
        _json_data(spec.model_dump(), "the scenario")))
    # Participants are named in every record; two that become one name
    # would make the evidence misattribute what each did.
    if len({a.name for a in public_spec.agents}) \
            < len({a.name for a in spec.agents}):
        raise RedactionCollision(
            "two participants would share a name after redaction")

    result = evaluate_scenario(public_spec, engine.run_id, events)
    rerun = f"nandatown run {name_or_path} --seed {spec.seed}"
    for path in plugins or []:
        rerun += f" --plugin {path}"
    for layer, plugin_id in (layer_overrides or {}).items():
        rerun += f" --layer {layer}={plugin_id}"
    # Everything the run record says about the scenario comes from the
    # redacted copy, the same one written as the profile.
    run_record = RunRecord(
        run_id=engine.run_id,
        profile_name=public_spec.name,
        profile_fingerprint=fingerprint(public_spec.model_dump()),
        created_at=0.0,
        participants=[{"name": a.name, "role": a.role}
                      for a in public_spec.agents],
        releases={
            "nandatown": __version__,
            "evaluator": LAB_EVALUATOR_VERSION,
            "python": sys.version.split()[0],
        },
        config={"mode": "lab", "seed": public_spec.seed,
                "layers": public_spec.layers,
                "logical_time": engine.now,
                "rerun_command": privacy.redact(
                    {"rerun_command": rerun})["rerun_command"]},
    )
    bundle_dir = os.path.join(out_dir, engine.run_id)
    write_bundle(bundle_dir, public_spec, run_record, intents, events,
                 result, mode="lab")
    from ..bundle import attest_bundle
    attest_bundle(bundle_dir)
    return bundle_dir, result
