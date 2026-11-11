#!/usr/bin/env python3
"""Run, record and verify the Common Ground compatibility check.

Live (starts the real Common Ground room from a checkout, one per case):

    python examples/integrations/common-ground/run_check.py \\
        --project ../common-ground-mvp --out runs/cg-live

Replay (no checkout needed; answers come from fixtures/):

    python examples/integrations/common-ground/run_check.py --out runs/cg

Verify recorded bundles offline:

    python examples/integrations/common-ground/run_check.py \\
        --verify examples/integrations/common-ground/evidence/*/sim-*
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "cg_plugin.py")
CASES = {"clean": "cg_meetup", "duplicate": "cg_meetup_duplicate",
         "dropout": "cg_meetup_dropout"}
# The HW7 "nov4" case: 15 OpenStreetMap cafes plus two homes.
VENUES = "evidence/hw7/20261104-185511-nov4-v2-r3/venues.json"
PEOPLE = {"jack": "Hermes", "javier": "Claude Code", "sergio": "Codex"}
# The two homes in that case are blurred to neighborhood level (coordinates
# rounded to two decimals, no street) before the room sees them, so no
# street-level home location reaches this public evidence.
BLURRED_HOMES = {
    "home-js": {"lat": 42.36, "lon": -71.09, "area": "The Port, Cambridge",
                "address": "The Port, Cambridge (neighborhood level)"},
    "home-jack": {"lat": 42.37, "lon": -71.11, "area": "Mid-Cambridge",
                  "address": "Mid-Cambridge (neighborhood level)"},
}


def _write_venues(src: str, dst: str) -> None:
    with open(src) as f:
        data = json.load(f)
    for v in data["venues"]:
        v.update(BLURRED_HOMES.get(v["id"], {}))
    with open(dst, "w") as f:
        json.dump(data, f, indent=1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, seconds: float = 20) -> None:
    end = time.time() + seconds
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise SystemExit(f"the room did not open port {port}")


def _project_version(project: str) -> dict:
    def git(*a):
        return subprocess.run(["git", "-C", project, *a], text=True,
                              capture_output=True).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}


REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def _run_lab_portable(scenario_rel: str, out: str):
    """run_lab, but the bundle records repo-relative paths (the scenario in
    the rerun command, the plugin file in the profile) instead of this
    machine's absolute ones, so the evidence can be shared as is."""
    from nandatown.sim import runner

    original = runner.resolve_spec

    def resolve(name_or_path):
        spec = original(name_or_path)
        spec.plugin_files = [os.path.relpath(p, REPO)
                             for p in spec.plugin_files]
        return spec

    cwd = os.getcwd()
    runner.resolve_spec = resolve
    os.chdir(REPO)
    try:
        return runner.run_lab(scenario_rel, out)
    finally:
        os.chdir(cwd)
        runner.resolve_spec = original


def run_case(case: str, out: str, project: str | None, python: str,
             record: bool) -> tuple[str, object]:
    scenario = os.path.relpath(os.path.join(HERE, CASES[case] + ".yaml"),
                               REPO)
    out = os.path.abspath(out)
    case_out = os.path.join(out, case)
    os.makedirs(case_out, exist_ok=True)
    env_keys = ("CG_MODE", "CG_ROOM_URL", "CG_ROOM_AGENTS", "CG_RECORD")
    saved = {k: os.environ.get(k) for k in env_keys}
    room = None
    try:
        if project:
            room_dir = tempfile.mkdtemp(prefix=f"cg-room-{case}-")
            tokens = {f"tok_{p}_{secrets.token_hex(6)}": p for p in PEOPLE}
            with open(os.path.join(room_dir, "agents.json"), "w") as f:
                json.dump({"tokens": tokens, "people": {
                    p: {"name": p.capitalize(), "harness": "Town " + h}
                    for p, h in PEOPLE.items()}}, f)
            _write_venues(os.path.join(project, VENUES),
                          os.path.join(room_dir, "venues.json"))
            port = _free_port()
            room = subprocess.Popen(
                [python, os.path.join(project, "room", "server.py"),
                 "--run", room_dir, "--port", str(port)],
                stdout=open(os.path.join(room_dir, "room_server.out"), "w"),
                stderr=subprocess.STDOUT)
            _wait_port(port)
            os.environ.update(CG_MODE="live",
                              CG_ROOM_URL=f"http://127.0.0.1:{port}/mcp",
                              CG_ROOM_AGENTS=os.path.join(room_dir,
                                                          "agents.json"))
            if record:
                os.environ["CG_RECORD"] = os.path.join(
                    HERE, "fixtures", CASES[case] + ".json")
            else:
                os.environ.pop("CG_RECORD", None)
        else:
            os.environ["CG_MODE"] = "replay"
        bundle, result = _run_lab_portable(scenario, case_out)
        if room is not None:
            # The room's own side of the run, next to (not inside) the bundle.
            side = os.path.join(case_out, "room")
            os.makedirs(side, exist_ok=True)
            for name in ("room_log.jsonl", "room_state.json"):
                src = os.path.join(room_dir, name)
                if os.path.exists(src):
                    shutil.copy(src, side)
            with open(os.path.join(side, "project_version.json"), "w") as f:
                json.dump(_project_version(project), f, indent=1)
        return bundle, result
    finally:
        if room is not None:
            room.terminate()
            room.wait(timeout=10)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def verify(bundles: list[str]) -> int:
    from nandatown.bundle import verify_bundle
    from nandatown.sim.runner import load_plugin_files

    load_plugin_files([PLUGIN])
    bad = 0
    for b in bundles:
        problems = verify_bundle(b)
        print(("verified  " if not problems else "PROBLEMS  ") + b)
        for p in problems:
            print("   ", p)
        bad += bool(problems)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--project", help="a Common Ground checkout (live mode)")
    ap.add_argument("--python", help="the Python that runs the room"
                    " (default: <project>/.venv/bin/python)")
    ap.add_argument("--case", choices=["all", *CASES], default="all")
    ap.add_argument("--out", default="runs/common-ground")
    ap.add_argument("--record", action="store_true",
                    help="live only: rewrite fixtures/ from this run")
    ap.add_argument("--verify", nargs="+", metavar="BUNDLE")
    args = ap.parse_args()
    if args.verify:
        return verify(args.verify)
    if args.record and not args.project:
        ap.error("--record needs --project (a live room)")
    project = os.path.abspath(args.project) if args.project else None
    python = args.python or (project and os.path.join(project, ".venv",
                                                      "bin", "python"))
    cases = list(CASES) if args.case == "all" else [args.case]
    rows = []
    for case in cases:
        bundle, result = run_case(case, args.out, project, python,
                                  args.record)
        rows.append((case, result.verdict, bundle))
        print(f"\n== {case} ({CASES[case]}): {result.verdict}")
        for s in result.stages:
            print(f"   {s.name:<24} {s.status:<20} {s.note}")
    print()
    for case, verdict, bundle in rows:
        print(f"{case:<10} {verdict:<14} {bundle}")
    # The clean and duplicate cases must pass; the dropout case is a
    # control that must fail (the room has no deadline for a silent agent).
    expected = {"clean": "passed", "duplicate": "passed",
                "dropout": "failed"}
    ok = all(v == expected[c] for c, v, _ in rows)
    print("\nas expected" if ok else "\nNOT as expected")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
