"""Verify a LOOM Lab evidence bundle.

    python examples/loom/verify_loom.py runs/loom/<run-id>

`nandatown verify` replays the evaluator in a fresh process, but it does
not import the scenario's `plugin_files`, so the custom LOOM validators
are not registered and the replay cannot reproduce result.json. This
script imports loom_plugin.py first and then runs the same
`verify_bundle` check, unchanged.
"""

from __future__ import annotations

import os
import sys

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    load_plugin_files([os.path.join(HERE, "loom_plugin.py")])
    problems = verify_bundle(argv[0])
    for p in problems:
        print(f"problem: {p}")
    if not problems:
        print("bundle verified with LOOM validators loaded: hashes match"
              " and the evaluator reproduces the recorded result")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
