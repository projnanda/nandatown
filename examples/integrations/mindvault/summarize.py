#!/usr/bin/env python3
"""results.csv from a run_compare.sh output directory (one row per run)."""

import csv
import json
import sys
from pathlib import Path

FIELDS = ["run", "memory", "profile", "rep", "town_run_id", "verdict",
          "stages_passed", "stages_failed", "wall_seconds", "mindvault_calls",
          "deal_notes", "note_levels", "note_statuses", "intern_level_too_low",
          "intern_not_found", "intern_leaked"]


def row(run_dir: Path) -> dict:
    memory, rest = run_dir.name.split("-", 1)
    profile, rep = rest.rsplit("-", 1)
    out = {"run": run_dir.name, "memory": memory, "profile": profile, "rep": rep}
    bundles = sorted(run_dir.glob("run-*"))
    if bundles:
        b = bundles[-1]
        result = json.loads((b / "result.json").read_text())
        created = json.loads((b / "run.json").read_text())["created_at"]
        stages = result["stages"]
        out.update(town_run_id=result["run_id"], verdict=result["verdict"],
                   stages_passed=sum(s["status"] == "passed" for s in stages),
                   stages_failed=sum(s["status"] == "failed" for s in stages),
                   wall_seconds=round(result["evaluated_at"] - created, 2))
    log = run_dir / "mv-calls.jsonl"
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    writes = [c["response"] for c in calls if c["tool"] == "memory_write"]
    out.update(mindvault_calls=len(calls), deal_notes=len(writes),
               note_levels="|".join(str(w.get("level")) for w in writes),
               note_statuses="|".join(str(w.get("status")) for w in writes))
    probe = run_dir / "probe.json"
    if probe.exists():
        p = json.loads(probe.read_text())
        out.update(intern_level_too_low=p["protected"],
                   intern_not_found=sum(r["intern"] == "NOT_FOUND" for r in p["reads_by_id"]),
                   intern_leaked=p["leaked"])
    return out


def main() -> None:
    root = Path(sys.argv[1])
    w = csv.DictWriter(sys.stdout, fieldnames=FIELDS)
    w.writeheader()
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(("off-", "mindvault-"))):
        w.writerow(row(d))


if __name__ == "__main__":
    main()
