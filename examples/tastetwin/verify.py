"""Verify a tastetwin_shared_taste bundle.

`nandatown verify` replays the Lab evaluator in a fresh process, where the
scenario's own validator (registered by tastetwin_plugin.py through
`plugin_files:`) is not loaded, so it reports a replay mismatch. This loads
the plugin first and then runs the same check:

    python examples/tastetwin/verify.py runs/tt/<run-id>
"""

from __future__ import annotations

import os
import sys

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    load_plugin_files([os.path.join(HERE, "tastetwin_plugin.py")])
    problems = verify_bundle(argv[1])
    if not problems:
        print("bundle verified: hashes match and the evaluator reproduces the recorded result")
        return 0
    for problem in problems:
        print(f"problem: {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
