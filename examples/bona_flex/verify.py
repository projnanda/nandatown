"""`nandatown verify` for a bona_flex bundle, with the pure bona_flex validator loaded so Town can re-judge it.

    python examples/bona_flex/verify.py <case dir>      (the folder holding bundle/ and bona_sidecar/)

Runs Town's own verify_bundle (record hashes, manifest, attestation, replay of the recorded result) on bundle/,
then this pack's sidecar and binding checks (check_evidence.py). No `bona` or `bench` import.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_evidence  # noqa: E402


def main() -> int:
    case = pathlib.Path(sys.argv[1]).resolve()
    probs = check_evidence.check_case(case)
    if not probs:
        print("bundle verified: Town hashes and replay match; sidecar files, binding and receipt correspondence hold")
        return 0
    for p in probs:
        print(f"problem: {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
