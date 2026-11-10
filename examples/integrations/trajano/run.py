#!/usr/bin/env python3
"""Run or verify the Trajano compatibility check.

    run.py --project ../trajano --case deal --out /tmp/trajano-town
    run.py --project ../trajano --case lost-accept --out /tmp/trajano-town
    run.py --project ../trajano --case over-mandate --out /tmp/trajano-town
    run.py --verify examples/integrations/trajano/evidence

A run exits nonzero unless every stage passed. Verification needs no project:
it loads this example's validators, which `nandatown verify` does not do, and
then runs Town's own bundle check.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files, run_lab

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "trajano_plugin.py")
CASES = {"deal": "trajano_deal.yaml",
         "lost-accept": "trajano_lost_accept.yaml",
         "over-mandate": "trajano_over_mandate.yaml"}
DEADLINE = 180  # seconds for one whole run


def _bundles(root: str) -> list[str]:
    return sorted(path for path, _, files in os.walk(root)
                  if "manifest.json" in files)


def verify(root: str) -> int:
    load_plugin_files([PLUGIN])
    bundles = _bundles(root)
    failed = 0 if bundles else 1
    for bundle in bundles:
        problems = verify_bundle(bundle)
        name = os.path.relpath(bundle, root)
        print(f"{name}: {'verified' if not problems else 'NOT verified'}")
        for problem in problems:
            print(f"  problem: {problem}")
        failed += bool(problems)
    return 1 if failed else 0


class RunDeadline(BaseException):
    """The whole run ran out of time. Not an Exception, so a role's error
    handling cannot absorb it."""


def _expire(*_) -> None:
    raise RunDeadline(f"run exceeded {DEADLINE} seconds")


def run(project: str, case: str, out: str) -> int:
    project = os.path.abspath(project)
    needed = [os.path.join(project, "scripts", "town_world.py"),
              os.path.join(project, ".venv", "bin", "python")]
    missing = [p for p in needed if not os.path.exists(p)]
    if missing:
        print(f"project setup incomplete, missing: {missing}", file=sys.stderr)
        return 2
    os.environ["TRAJANO_PROJECT"] = project
    signal.signal(signal.SIGALRM, _expire)
    signal.alarm(DEADLINE)
    try:
        bundle, result = run_lab(
            os.path.relpath(os.path.join(HERE, CASES[case])), out)
    except RunDeadline as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
        plugin = next((m for m in sys.modules.values()
                       if getattr(m, "__file__", None) == PLUGIN), None)
        if plugin is not None:
            plugin.close_all()
    for stage in result.stages:
        print(f"{stage.name:<20}{stage.status:<22}{stage.note}")
    print(f"verdict: {result.verdict}")
    print(f"bundle: {bundle}")
    return 0 if result.verdict == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project")
    parser.add_argument("--case", choices=sorted(CASES))
    parser.add_argument("--out", default="runs")
    parser.add_argument("--verify", metavar="DIR")
    args = parser.parse_args()
    if args.verify:
        return verify(args.verify)
    if not args.project or not args.case:
        parser.error("use --verify DIR, or both --project and --case")
    return run(args.project, args.case, args.out)


if __name__ == "__main__":
    sys.exit(main())
