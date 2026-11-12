"""Replay-verify a bundle produced with the registry_snapshot profile.

    python verify_bundle.py <bundle_dir>

Stock `nandatown verify` replays the evaluator with the validators it
already knows and never imports plugin files named inside a bundle, because
a bundle is untrusted input. It still checks a bundle's hashes and
attestation, but it cannot check the evaluation of these bundles: on them it
reports a replay mismatch, because its own lab-0.2.6 has no
registry_rotation validator. Only this wrapper can check the evaluation.

The wrapper reads what the run recorded in its registry_snapshot event (the
validator version, the core profile's sha256, and the file and sha256 of the
registry plugin that loaded the snapshot) and refuses to replay unless this
checkout has exactly those files. The gate is a whole-file hash: any edit to
either file, a comment included, orphans earlier bundles. That is
deliberate for an example; a later change to the rules can never pass for a
good bundle. Only then does it import the core profile and the recorded
source plugin, by the operator's choice, and run the stock verify unchanged.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from nandatown.cli import main as nandatown
from nandatown.sim.runner import load_plugin_files

HERE = Path(__file__).resolve().parent


def recorded(bundle: Path) -> dict | None:
    for line in (bundle / "events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("kind") in ("registry_snapshot_loaded",
                                 "registry_snapshot_unavailable"):
            return event.get("detail", {})
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    bundle = Path(argv[0])
    rec = recorded(bundle)
    if rec is None:
        print("problem: no registry_snapshot event in this bundle; it was not"
              " produced with this profile")
        return 1
    spec = importlib.util.spec_from_file_location(
        "registry_snapshot_profile", HERE / "registry_snapshot.py")
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    source = (HERE / rec.get("source_file", "")).resolve()
    if HERE not in source.parents and source.parent != HERE or not source.is_file():
        print(f"version difference: the recorded source plugin {rec.get('source_file')!r}"
              " is not in this checkout. Replay not attempted.")
        return 3
    mine = (core.VALIDATOR_VERSION, core.file_sha256(HERE / "registry_snapshot.py"),
            core.file_sha256(source))
    theirs = (rec.get("validator_version"), rec.get("profile_sha256"),
              rec.get("source_sha256"))
    if mine != theirs:
        print(f"version difference: the bundle was judged by {theirs[0]} with core"
              f" {theirs[1]} and source {theirs[2]}; this checkout has {mine[0]} with"
              f" core {mine[1]} and source {mine[2]}. Replay not attempted.")
        return 3
    if source != (HERE / "registry_snapshot.py").resolve():
        load_plugin_files([str(source)])
    print(f"profile {mine[0]}, core {mine[1]}, source {rec.get('source_file')}"
          f" {mine[2]} match the bundle")
    return nandatown(["verify", str(bundle)])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
