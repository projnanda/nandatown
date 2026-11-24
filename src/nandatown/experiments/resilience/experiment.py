# SPDX-License-Identifier: MIT
# Copyright (c) 2026 nanda-resilience contributors
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import nandatown
from nandatown.bundle import attest_bundle, verify_bundle, write_bundle
from nandatown.identity_portable import Keystore
from nandatown.records import RunRecord, fingerprint
from nandatown.sim.runner import build_engine
from nandatown.sim.validators import LAB_EVALUATOR_VERSION, evaluate_scenario

from . import (
    EVALUATOR, SOURCE_EXPERIMENT_VERSION, UPSTREAM_COMPATIBILITY_BASELINE,
    __version__, agents,  # noqa: F401
)
from .evaluator import analyze, metrics
from .model import Settings, build_spec


def run_experiment(settings: Settings, out: Path | None = None) -> dict:
    spec = build_spec(settings)
    engine = build_engine(spec)
    engine.run()
    result = evaluate_scenario(spec, engine.run_id, engine.events)
    if result.verdict != "passed":
        raise ValueError(result.model_dump_json(indent=2))
    analysis = analyze(spec, engine.events, engine.run_id)
    row = metrics(analysis)
    logical_events = [
        {"at": event.at, "observer": event.observer,
         "kind": event.kind, "subject": event.subject, "detail": event.detail}
        for event in engine.events if event.kind.startswith("resilience.")
    ]
    row["logical_trace_fingerprint"] = fingerprint(logical_events)
    row["evaluator"] = EVALUATOR
    row["bundle"] = None
    if out is not None:
        out.mkdir(parents=True, exist_ok=False)
        rerun = (
            f"nandatown-resilience run --condition {settings.condition}"
            f" --policy {settings.policy} --seed {settings.seed}"
            f" --disclosure {settings.disclosure} --buyers {settings.buyers}"
            f" --rounds {settings.rounds} --attempts {settings.attempts}"
            f" --interval {settings.interval} --service-time {settings.service_time}"
            f" --dependency-capacity {settings.dependency_capacity} --out NEW_DIRECTORY"
        )
        run = RunRecord(
            run_id=engine.run_id, profile_name=spec.name,
            profile_fingerprint=fingerprint(spec.model_dump()),
            created_at=0.0,
            participants=[{"name": a.name, "role": a.role} for a in spec.agents],
            releases={
                "nandatown": nandatown.__version__,
                "nandatown_compatibility_baseline": UPSTREAM_COMPATIBILITY_BASELINE,
                "resilience_experiment": __version__,
                "source_experiment_version": SOURCE_EXPERIMENT_VERSION,
                "evaluator": LAB_EVALUATOR_VERSION,
                "resilience_evaluator": EVALUATOR,
                "python": sys.version.split()[0],
            },
            config={"mode": "lab", "seed": settings.seed, "layers": spec.layers,
                    "logical_time": engine.now, "rerun_command": rerun,
                    "settings": settings.model_dump()},
        )
        write_bundle(str(out), spec, run, engine.intents, engine.events, result, mode="lab")
        # Only public attestation material enters the bundle; no personal keystore.
        with TemporaryDirectory(prefix=".resilience-keys-", dir=out.parent) as keys:
            attest_bundle(str(out), Keystore(keys), signer="resilience-demo")
        problems = verify_bundle(str(out))
        if problems:
            raise ValueError(f"bundle verification failed: {problems}")
        row["bundle"] = out.name
        (out / "metrics.json").write_text(
            json.dumps(row, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return row
