"""Verify a redeco bundle with this example's validators loaded.

`nandatown verify` replays only built-in validators: a Lab bundle whose
validator comes from scenario `plugin_files` is reported as an "evaluator
replay mismatch" because the validator is not registered at verify time.
Loading plugin code is an explicit, local choice, so this wrapper does it
for this one reviewed file and then runs Town's own verification.

    python examples/integrations/stellar-redeco/verify_bundle.py runs/sim-...
"""
import os
import sys

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files

load_plugin_files([os.path.join(os.path.dirname(os.path.abspath(__file__)), "redeco_plugin.py")])
problems = verify_bundle(sys.argv[1])
for p in problems:
    print(f"problem: {p}")
print("bundle verified: hashes match and the evaluator reproduces the recorded result"
      if not problems else f"{len(problems)} problem(s)")
sys.exit(1 if problems else 0)
