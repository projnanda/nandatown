"""Verify an Agents Gym bundle: load this integration, then run Town's verify.

These scenarios judge with validators that live in ``plugin.py``, loaded by
``plugin_files:``. On current main, ``nandatown verify`` does not load those
files, so a fresh process cannot replay the evaluation and reports
``evaluator replay mismatch``. This wrapper registers the plugin first and
then hands over to ``nandatown verify`` unchanged.

Example::

    python integrations/agents_gym/verify.py runs/sim-5765ee2cfa1f
"""

from __future__ import annotations

import sys
from pathlib import Path

from nandatown.cli import main as town_main
from nandatown.sim.runner import load_plugin_files

PLUGIN = Path(__file__).resolve().parent / "plugin.py"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python integrations/agents_gym/verify.py BUNDLE_DIR")
        return 2
    load_plugin_files([str(PLUGIN)])
    return town_main(["verify", args[0]])


if __name__ == "__main__":
    sys.exit(main())
