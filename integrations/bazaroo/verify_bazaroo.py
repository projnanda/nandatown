"""Verify a Bazaroo run bundle with the Bazaroo plugin and validators loaded.

Plain `nandatown verify` re-evaluates a bundle without our validators registered, so it
reports an "evaluator replay mismatch" for these runs. Loading the two files first
registers the validators, then Town's own verify runs unchanged.

Usage (from the repo root):  python integrations/bazaroo/verify_bazaroo.py runs/<run-id>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import registry_bazaroo_v1  # noqa: E402,F401  (registers the bazaroo.v1 plugin)
import validators_bazaroo  # noqa: E402,F401  (registers the Bazaroo validators)
from nandatown.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = ["nandatown", "verify", *sys.argv[1:]]
    sys.exit(main())
