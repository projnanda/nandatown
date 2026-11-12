"""Run the Signed Answers compatibility check and compare with expectations.

Offline (default): starts the stand-in outlet from stub_outlet.py, so no
network, model or credential is needed.

    python examples/signed_answers/run_check.py

Live: calls the real outlet agent named by the environment.

    export SIGNED_ANSWERS_URL=...       # the outlet agent's base URL
    export SIGNED_ANSWERS_TOKEN=...     # a subscriber credential
    export SIGNED_ANSWERS_QUESTION=...  # a question about its reporting
    python examples/signed_answers/run_check.py --live

Verify saved bundles (loads this example's validator first, which the
`nandatown verify` command does not do for a plugin validator):

    python examples/signed_answers/run_check.py --verify examples/signed_answers/evidence
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PLUGIN = "examples/signed_answers/signed_answers_town.py"
SCENARIOS = "examples/signed_answers/scenarios"
ENV = ("SIGNED_ANSWERS_URL", "SIGNED_ANSWERS_TOKEN", "SIGNED_ANSWERS_QUESTION")
DEAD_URL = "http://127.0.0.1:9"  # the discard port: nothing answers A2A there

# label, scenario, outlet, expected verdict, the stages expected to fail
CASES = [
    ("clean", "signed_answers_clean", "outlet", "passed", None),
    # The real outlet agent returns the first reply again for a repeated
    # messageId (since its commit f69f3c9), so the redelivery is safe.
    ("duplicate", "signed_answers_duplicate", "outlet", "passed", None),
    ("duplicate_remembered", "signed_answers_duplicate_remembered", "outlet",
     "passed", None),
    ("altered", "signed_answers_altered", "outlet", "passed", None),
    ("outlet_down", "signed_answers_clean", "down", "failed",
     ["outlet_listed", "outlet_discovered"]),
]
# Offline, the stand-in outlet plays both versions of the agent: the default
# one builds a fresh package per call, as the real agent did before the fix.
STUB_CASES = [
    ("clean", "signed_answers_clean", "outlet", "passed", None),
    ("duplicate_fresh_per_call_outlet", "signed_answers_duplicate", "outlet",
     "failed", ["one_package_per_request[reader-subscriber]"]),
    ("duplicate", "signed_answers_duplicate", "idempotent", "passed", None),
    ("duplicate_remembered", "signed_answers_duplicate_remembered", "outlet",
     "passed", None),
    ("altered", "signed_answers_altered", "outlet", "passed", None),
    ("outlet_down", "signed_answers_clean", "down", "failed",
     ["outlet_listed", "outlet_discovered"]),
]


def _load_plugin() -> None:
    from nandatown.sim.runner import load_plugin_files

    load_plugin_files([str(REPO / PLUGIN)])


def _start_stub(idempotent: bool):
    sys.path.insert(0, str(HERE))
    from stub_outlet import make_server

    server = make_server(0, idempotent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_cases(live: bool, out: str, save: str | None,
              fresh_per_call: bool = False) -> int:
    from nandatown.bundle import verify_bundle
    from nandatown.sim.runner import run_lab

    if live:
        missing = [name for name in ENV if not os.environ.get(name)]
        if missing:
            print("live mode needs: " + ", ".join(missing))
            return 2
        real = {name: os.environ[name] for name in ENV}
        cases = CASES
        if fresh_per_call:  # an outlet agent from before commit f69f3c9
            cases = [c if c[0] != "duplicate" else (
                *c[:3], "failed", ["one_package_per_request[reader-subscriber]"])
                for c in CASES]
    else:
        from stub_outlet import STUB_TOKEN  # noqa: F401  (path set below)
        cases = STUB_CASES

    rows, ok = [], True
    for label, scenario, outlet, want, want_failed in cases:
        server = None
        if outlet == "down":
            os.environ["SIGNED_ANSWERS_URL"] = DEAD_URL
        elif live:
            os.environ.update(real)
        else:
            server = _start_stub(idempotent=outlet == "idempotent")
            os.environ["SIGNED_ANSWERS_URL"] = (
                f"http://127.0.0.1:{server.server_address[1]}")
            os.environ["SIGNED_ANSWERS_TOKEN"] = STUB_TOKEN
            os.environ.pop("SIGNED_ANSWERS_QUESTION", None)
        try:
            bundle, result = run_lab(f"{SCENARIOS}/{scenario}.yaml", out,
                                     plugins=[PLUGIN])
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
        failed = [s.name for s in result.stages if s.status == "failed"]
        unknown = [s.name for s in result.stages
                   if s.status == "not_enough_evidence"]
        problems = verify_bundle(bundle)
        as_expected = (result.verdict == want and not problems and (
            failed == (want_failed or [])))
        ok = ok and as_expected
        if save:
            target = Path(save) / label
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(bundle, target)
        rows.append({
            "case": label, "scenario": scenario, "expected": want,
            "expected_failed_stages": want_failed or [], "observed": result.verdict,
            "failed_stages": failed, "not_enough_evidence": unknown,
            "bundle_verifies": not problems, "as_expected": as_expected,
            "stages": {s.name: s.status for s in result.stages},
            "notes": {s.name: s.note for s in result.stages if s.note
                      and s.status != "passed"},
        })
        mark = "as expected" if as_expected else "NOT AS EXPECTED"
        print(f"{label:30} expected {want:7} observed {result.verdict:10}"
              f" failed: {', '.join(failed) or '-':45} {mark}")
        for problem in problems:
            print(f"    verify problem: {problem}")
    summary = {"mode": "live" if live else "offline stand-in", "cases": rows}
    if save:
        Path(save).mkdir(parents=True, exist_ok=True)
        (Path(save) / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n")
    print("all cases as expected" if ok else "some cases differ from expectation")
    return 0 if ok else 1


def verify_saved(root: str) -> int:
    from nandatown.bundle import verify_bundle

    bad = count = 0
    for manifest in sorted(Path(root).rglob("manifest.json")):
        problems = verify_bundle(str(manifest.parent))
        print(f"{manifest.parent}: " + ("verified" if not problems
                                        else "; ".join(problems)))
        bad += bool(problems)
        count += 1
    print(f"{count} bundles checked, {bad} with problems")
    return 1 if bad or not count else 0  # nothing found is not a pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--fresh-per-call", action="store_true",
                        help="live only: the outlet agent under test builds a"
                        " new package for a repeated message, so the"
                        " duplicate case is expected to fail")
    parser.add_argument("--out", default="runs/signed-answers")
    parser.add_argument("--save", help="copy each bundle to this directory")
    parser.add_argument("--verify", metavar="DIR",
                        help="verify saved bundles under DIR and exit")
    args = parser.parse_args()
    # Paths in a bundle's rerun command are relative to the repository root.
    os.chdir(REPO)
    sys.path.insert(0, str(HERE))
    _load_plugin()
    if args.verify:
        return verify_saved(args.verify)
    return run_cases(args.live, args.out, args.save, args.fresh_per_call)


if __name__ == "__main__":
    sys.exit(main())
