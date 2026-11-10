"""Verify a JANG example bundle with this example's validators loaded.

`nandatown verify` replays only built-in validators, so a Lab bundle whose
validator comes from scenario `plugin_files` is reported as an "evaluator
replay mismatch". Loading plugin code is an explicit, local choice; this
wrapper does it for this one reviewed file and then runs Town's own
verification (hashes, fingerprints, evaluator replay).

    python examples/jang/verify_bundle.py runs/sim-...
"""
import os
import sys

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files

load_plugin_files([os.path.join(os.path.dirname(os.path.abspath(__file__)), "jang_plugin.py")])
problems = verify_bundle(sys.argv[1])
for p in problems:
    print(f"problem: {p}")
print("bundle verified: hashes match and the evaluator reproduces the recorded result"
      if not problems else f"{len(problems)} problem(s)")
sys.exit(1 if problems else 0)
