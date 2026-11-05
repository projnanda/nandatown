# SPDX-License-Identifier: MIT
# Copyright (c) 2026 nanda-resilience contributors
from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from pathlib import Path
from statistics import mean

import nandatown

from . import (
    EVALUATOR, SOURCE_EXPERIMENT_VERSION, UPSTREAM_COMPATIBILITY_BASELINE, __version__,
)
from .experiment import run_experiment
from .model import CONDITIONS, POLICIES, Settings, stable_rank
from .report import render_html

MEASURES = (
    "completion_rate", "stress_completion_rate", "attempts_per_task",
    "cost_per_success_cents", "p95_success_latency",
    "dependency_hhi_before_stress", "agent_hhi_before_stress",
    "recovery_after_outage", "repeated_dependency_retries",
)


def bootstrap_interval(values: list[float], seed: int) -> list[float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    samples = sorted(
        mean(rng.choices(values, k=len(values))) for _ in range(2000)
    )
    return [samples[49], samples[1949]]


def summarize(rows: list[dict]) -> dict:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["condition"], row["disclosure"], row["policy"])].append(row)
    aggregate = []
    for (condition, disclosure, policy), group in sorted(groups.items()):
        entry = {"condition": condition, "disclosure": disclosure,
                 "policy": policy, "runs": len(group)}
        for measure in MEASURES:
            values = [r[measure] for r in group if r[measure] is not None]
            entry[measure] = mean(values) if values else None
            entry[f"{measure}_observed"] = len(values)
        aggregate.append(entry)
    paired = []
    for condition in CONDITIONS:
        for disclosure in sorted({r["disclosure"] for r in rows}):
            candidate = groups.get((condition, disclosure, "dependency"), [])
            for baseline in ("price", "random", "load", "oracle"):
                controls = {r["seed"]: r for r in groups.get(
                    (condition, disclosure, baseline), [])}
                for measure in ("stress_completion_rate", "attempts_per_task",
                                "cost_per_success_cents"):
                    differences = [
                        r[measure] - controls[r["seed"]][measure]
                        for r in candidate if r["seed"] in controls
                        and r[measure] is not None
                        and controls[r["seed"]][measure] is not None
                    ]
                    if differences:
                        paired.append({
                            "condition": condition, "disclosure": disclosure,
                            "contrast": f"dependency - {baseline}", "metric": measure,
                            "paired_seeds": len(differences), "mean_difference": mean(differences),
                            "bootstrap_95_percentile": bootstrap_interval(
                                differences, stable_rank(condition, disclosure, baseline, measure)),
                        })
    return {"aggregate": aggregate, "paired": paired}


def run_campaign(out: Path, seeds: list[int], disclosures: list[float],
                 keep_all: bool = False) -> dict:
    if not seeds or len(seeds) != len(set(seeds)):
        raise ValueError("seeds must be nonempty and unique")
    if not disclosures or len(disclosures) != len(set(disclosures)):
        raise ValueError("disclosures must be nonempty and unique")
    for seed in seeds:
        for disclosure in disclosures:
            Settings(seed=seed, disclosure=disclosure)
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    total = len(seeds) * len(disclosures) * len(CONDITIONS) * len(POLICIES)
    for condition in CONDITIONS:
        for disclosure in disclosures:
            for seed in seeds:
                for policy in POLICIES:
                    name = f"{condition}-{disclosure:g}-{seed}-{policy}"
                    retain = keep_all or seed == seeds[0]
                    row = run_experiment(
                        Settings(seed=seed, policy=policy, condition=condition,
                                 disclosure=disclosure),
                        out / "bundles" / name if retain else None,
                    )
                    rows.append(row)
                    print(f"[{len(rows)}/{total}] {name}: "
                          f"{row['completed']}/{row['tasks']} completed", flush=True)
    summary = {
        "title": "Many Agents, One Point of Failure",
        "version": __version__, "evaluator": EVALUATOR,
        "nandatown_version": nandatown.__version__,
        "upstream_compatibility_baseline": UPSTREAM_COMPATIBILITY_BASELINE,
        "source_experiment_version": SOURCE_EXPERIMENT_VERSION,
        "design": {
            "seeds": seeds, "disclosures": disclosures,
            "conditions": list(CONDITIONS), "policies": list(POLICIES),
            "retained_bundles": "all" if keep_all else "first seed in every condition",
            "interval_method": "paired-seed percentile bootstrap, 2000 resamples",
            "scope": "synthetic Nanda Town Lab; shared coordinator; no model inference",
        },
        **summarize(rows),
        "runs": rows,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    fields = [key for key in rows[0] if key != "timeline"]
    with (out / "runs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    (out / "report.html").write_text(render_html(summary), encoding="utf-8")
    return summary
