"""Run the Rebbi compatibility checks in the Lab, or verify recorded bundles.

    python examples/rebbi/run.py --project ../rebbi --case all --out runs/rebbi
    python examples/rebbi/run.py --verify examples/rebbi/evidence

Each case gets a fresh Rebbi ledger, seeded by Rebbi's own CLI from
fleet.toml, then one Lab run whose adapter talks to Rebbi's MCP server.
Run from the root of the Town checkout. Exits nonzero unless every run
passed (or, with --verify, every bundle verified).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# Relative to the Town root on purpose: these strings are recorded in the
# bundle, and a local absolute path would leak into shared evidence.
PLUGIN = "examples/rebbi/rebbi_plugin.py"
CASES = {
    "lend": "examples/rebbi/rebbi_lend.yaml",
    "lost-reply": "examples/rebbi/rebbi_lost_reply.yaml",
    "duplicate": "examples/rebbi/rebbi_duplicate.yaml",
    "no-room": "examples/rebbi/rebbi_no_room.yaml",
}


def rebbi_python(project: str) -> str:
    if os.environ.get("REBBI_PYTHON"):
        return os.environ["REBBI_PYTHON"]
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return os.path.join(project, ".venv", *sub)


def run_case(case: str, project: str, out: str) -> str:
    from nandatown.sim.runner import run_lab

    work = os.path.join(out, "work", case)
    os.makedirs(work, exist_ok=True)
    db = os.path.join(work, "rebbi.db")
    python = rebbi_python(project)
    fleet = os.path.join(HERE, "fleet.toml")
    env = dict(os.environ, PYTHONPATH=project)
    subprocess.run([python, "-m", "rebbi", "--db", db, "--scenario", fleet,
                    "seed", "--reset"], cwd=project, env=env, check=True,
                   stdout=subprocess.DEVNULL)
    os.environ.update(REBBI_PROJECT=project, REBBI_PYTHON=python,
                      REBBI_DB=db, REBBI_SCENARIO=fleet,
                      REBBI_SERVER_LOG=os.path.join(work, "rebbi-mcp.log"))
    bundle, result = run_lab(CASES[case], out, plugins=[PLUGIN])
    print(f"\n{case}: {result.verdict.upper()}  ({os.path.relpath(bundle)})")
    for stage in result.stages:
        print(f"  {stage.status:<20} {stage.name:<26} {stage.note}")
    print("Rebbi's ledger after the run:", flush=True)
    subprocess.run([python, "-m", "rebbi", "--db", db, "status"],
                   cwd=project, env=env, check=False)
    return result.verdict


def verify(root: str) -> int:
    from nandatown.bundle import verify_bundle
    from nandatown.sim.runner import load_plugin_files

    load_plugin_files([os.path.join(HERE, "rebbi_plugin.py")])
    bad = 0
    for name in sorted(os.listdir(root)):
        bundle = os.path.join(root, name)
        if not os.path.isfile(os.path.join(bundle, "manifest.json")):
            continue
        problems = verify_bundle(bundle)
        print(f"{name}: {'verified' if not problems else 'PROBLEMS'}")
        for problem in problems:
            print(f"  {problem}")
        bad += bool(problems)
    return 1 if bad else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", help="the Rebbi checkout to test")
    p.add_argument("--case", choices=[*CASES, "all"], default="all")
    p.add_argument("--out", default="runs/rebbi")
    p.add_argument("--verify", metavar="DIR",
                   help="verify recorded bundles instead of running")
    args = p.parse_args()
    if args.verify:
        return verify(args.verify)
    if not args.project:
        p.error("--project is required to run")
    project = os.path.abspath(args.project)
    out = os.path.abspath(args.out)
    os.chdir(ROOT)
    cases = list(CASES) if args.case == "all" else [args.case]
    verdicts = [run_case(case, project, out) for case in cases]
    return 0 if all(v == "passed" for v in verdicts) else 1


if __name__ == "__main__":
    sys.exit(main())
