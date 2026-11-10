"""Verify an evidence bundle from these scenarios: `nandatown verify` replays the evaluator without loading a
scenario's plugin_files, so load this plugin first.

    python examples/probiai/verify.py <bundle-dir> [...]
"""
import os
import sys

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files

load_plugin_files([os.path.join(os.path.dirname(os.path.abspath(__file__)), "probiai_insured_ledger.py")])
failed = 0
for path in sys.argv[1:]:
    problems = verify_bundle(path)
    failed += bool(problems)
    print(f"{path}: {'verified' if not problems else '; '.join(map(str, problems))}")
sys.exit(1 if failed or len(sys.argv) < 2 else 0)
