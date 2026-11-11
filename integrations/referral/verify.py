"""Verify a Referral bundle: load this integration, then run Town's verify.

The scenarios judge with validators that live in ``plugin.py`` and are loaded
through ``plugin_files:``. On current main ``nandatown verify`` does not load
those files, so a fresh process cannot replay the evaluation and reports a
replay mismatch. This wrapper registers the plugin first and then hands over
to ``nandatown verify`` unchanged. Verification is offline: it replays the
recorded events and does not start Referral.

    python integrations/referral/verify.py runs/sim-XXXXXXXXXXXX
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
        print("usage: python integrations/referral/verify.py BUNDLE_DIR")
        return 2
    load_plugin_files([str(PLUGIN)])
    return town_main(["verify", args[0]])


if __name__ == "__main__":
    sys.exit(main())
