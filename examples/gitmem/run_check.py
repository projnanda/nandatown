#!/usr/bin/env python3
"""The gitmem compatibility check: six Track runs, expected against actual.

    GITMEM_SRC=/path/to/nandatown-memory-mvp/memory_mvp \\
        python examples/gitmem/run_check.py

Each case starts the gitmem seller through `nandatown test-agent --cmd`
against Town's scripted buyer, under a Track profile that crashes the
seller, then:

  1. reads Town's verdict from the bundle,
  2. runs `nandatown verify` on the bundle,
  3. runs the observer lane (gitmem_observe.py) over Town's events and
     the seller's memory,
  4. compares all of it with the expectation written below.

Exits 0 only if every case matches. `--evidence DIR` copies each bundle's
records, the memory check and the memory's git history into DIR/<case>/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
SELLER = HERE / "gitmem_seller.py"
RECORDS = ("profile.json", "run.json", "intents.jsonl", "events.jsonl",
           "result.json", "manifest.json", "attestation.json", "report.md")


@dataclass
class Case:
    name: str
    profile: str
    edit: str            # GITMEM_EDIT: none | total | delete
    trust: str           # GITMEM_TRUST: verify | trust
    town: str            # Town's verdict
    agreed: int          # self vs observed facts that agree
    contradicted: int    # ... that contradict
    applications: int    # "applied q-1" commits in the memory's history
    found: int           # outside edits the seller found on restart
    unrepaired: int      # outside edits still in the files after the run
    refused: int         # resends Town refused (identity_reuse_rejected)
    why: str


CASES = [
    Case("intact", "quote-amnesia-restart", "none", "verify",
         "passed", 3, 0, 1, 0, 0, 0,
         "memory survives the crash; the restart resends the remembered"
         " reply and Town returns the original acceptance"),
    Case("edit-total-verify", "quote-amnesia-restart", "total", "verify",
         "passed", 3, 0, 1, 2, 0, 0,
         "the hand edit is found in the working tree, put back from git,"
         " and the identical reply is resent"),
    Case("edit-total-trust", "quote-amnesia-restart", "total", "trust",
         "incomplete", 1, 2, 1, 0, 2, 1,
         "the seller believes 3999 and resends r-1 with it; Town refuses"
         " the changed content under the same identity, and the observer"
         " contradicts the remembered total and fingerprint"),
    Case("delete-trust", "quote-amnesia-restart", "delete", "trust",
         "passed", 3, 0, 2, 0, 2, 0,
         "the seller forgets and applies the work a second time; Town"
         " absorbs the identical answer and passes, the memory's history"
         " shows two applications"),
    Case("delete-verify", "quote-amnesia-restart", "delete", "verify",
         "passed", 3, 0, 1, 2, 0, 0,
         "the deleted facts are restored from git before the redelivery,"
         " so the work is applied once"),
    Case("crash-restart", "quote-crash-restart", "none", "verify",
         "passed", 3, 0, 1, 0, 0, 0,
         "the seller stalls past its lease before applying anything; the"
         " stale fence is rejected and the restart applies the work once"),
]


def nandatown_cmd() -> list[str]:
    beside = Path(sys.executable).parent / "nandatown"
    if beside.exists():
        return [str(beside)]
    found = shutil.which("nandatown")
    if found:
        return [found]
    return [sys.executable, "-c",
            "import sys; from nandatown.cli import main; sys.exit(main())"]


def versions(gitmem_src: str) -> dict:
    out = {"seller_sha256": "sha256:" + hashlib.sha256(
        SELLER.read_bytes()).hexdigest()}
    try:
        out["gitmem_commit"] = subprocess.run(
            ["git", "-C", gitmem_src, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", gitmem_src, "status", "--porcelain", "--",
             "gitmem"], capture_output=True, text=True).stdout.strip()
        out["gitmem_dirty"] = bool(dirty)
    except (subprocess.CalledProcessError, FileNotFoundError):
        out["gitmem_commit"] = "unknown (not a git checkout)"
    return out


def run_case(case: Case, out_dir: Path, gitmem_src: str) -> dict:
    env = dict(os.environ, GITMEM_SRC=gitmem_src, GITMEM_EDIT=case.edit,
               GITMEM_TRUST=case.trust)
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(SELLER))}"
    proc = subprocess.run(
        [*nandatown_cmd(), "test-agent", "--role", "seller",
         "--profile", case.profile, "--cmd", cmd, "--timeout", "60",
         "--out", str(out_dir)],
        env=env, capture_output=True, text=True)
    match = re.search(r"Evidence bundle: (.+)", proc.stdout)
    if not match:
        return {"error": "no bundle printed",
                "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}
    bundle = Path(match.group(1).strip())
    verify = subprocess.run([*nandatown_cmd(), "verify", str(bundle)],
                            capture_output=True, text=True)
    sys.path.insert(0, str(HERE))
    import gitmem_observe

    report = gitmem_observe.check(bundle)
    (bundle / "gitmem-check.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n")
    report["_bundle"] = str(bundle)
    report["_verify_rc"] = verify.returncode
    report["_verify"] = (verify.stdout + verify.stderr).strip()
    return report


def actual_of(report: dict) -> dict:
    if "error" in report:
        return {"error": report["error"]}
    q = report["requests"].get("q-1", {})
    counts = q.get("reconcile", {}).get("counts", {})
    seller = report.get("seller_reported") or {}
    return {
        "town": report["town_verdict"],
        "agreed": counts.get("agreed"),
        "contradicted": counts.get("contradiction"),
        "applications": q.get("applications"),
        "found": len(seller.get("outside_edits") or []),
        "unrepaired": len(report["outside_edits_unrepaired"]),
        "refused": q.get("town_refused_resends"),
        "verified": report["_verify_rc"] == 0,
    }


def expected_of(case: Case) -> dict:
    return {"town": case.town, "agreed": case.agreed,
            "contradicted": case.contradicted,
            "applications": case.applications, "found": case.found,
            "unrepaired": case.unrepaired, "refused": case.refused,
            "verified": True}


def save_evidence(report: dict, dest: Path, case: Case, meta: dict) -> None:
    bundle = Path(report["_bundle"])
    dest.mkdir(parents=True, exist_ok=True)
    for name in RECORDS:
        if (bundle / name).exists():
            shutil.copy2(bundle / name, dest / name)
    check = {k: v for k, v in report.items() if not k.startswith("_")}
    check["case"] = case.name
    check["env"] = {"GITMEM_EDIT": case.edit, "GITMEM_TRUST": case.trust,
                    "profile": case.profile}
    check["versions"] = meta
    (dest / "gitmem-check.json").write_text(
        json.dumps(check, indent=2, default=str) + "\n")
    memory = bundle / "state" / "seller" / "memory"
    history = subprocess.run(
        ["git", "-C", str(memory), "log", "--reverse", "-p",
         "--format=commit %h  %ad%n%B", "--date=iso"],
        capture_output=True, text=True).stdout
    status = subprocess.run(
        ["git", "-C", str(memory), "status", "--short"],
        capture_output=True, text=True).stdout
    (dest / "memory-history.txt").write_text(
        history + "\n# git status at the end of the run\n"
        + (status or "(clean)\n"))
    log = bundle / "state" / "seller" / "gitmem_seller.log"
    if log.exists():
        (dest / "gitmem_seller.log").write_text(log.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append",
                        help="run just this case (repeatable)")
    parser.add_argument("--out", type=Path,
                        help="directory for the run bundles"
                             " (default: a temporary directory)")
    parser.add_argument("--evidence", type=Path,
                        help="copy each case's records here")
    args = parser.parse_args(argv)

    gitmem_src = os.environ.get("GITMEM_SRC", "")
    if not gitmem_src or not (Path(gitmem_src) / "gitmem").is_dir():
        print("Set GITMEM_SRC to the memory_mvp directory of a"
              " nandatown-memory-mvp checkout; see"
              " examples/gitmem/README.md.", file=sys.stderr)
        return 2
    sys.path.insert(0, gitmem_src)
    meta = versions(gitmem_src)
    out_dir = args.out or Path(tempfile.mkdtemp(prefix="gitmem-check-"))
    cases = [c for c in CASES if not args.only or c.name in args.only]

    print(f"gitmem {meta.get('gitmem_commit', '?')[:12]}"
          f"{' (modified)' if meta.get('gitmem_dirty') else ''},"
          f" seller {meta['seller_sha256'][:19]}, bundles in {out_dir}\n")
    failures = 0
    header = (f"{'case':<19}{'town':<12}{'agree':>6}{'contra':>7}"
              f"{'apps':>5}{'found':>6}{'left':>5}{'refused':>8}  result")
    print(header)
    print("-" * len(header))
    for case in cases:
        report = run_case(case, out_dir, gitmem_src)
        actual, expected = actual_of(report), expected_of(case)
        ok = actual == expected
        failures += not ok

        def cell(k, w):
            a, e = actual.get(k), expected.get(k)
            text = str(a) if a == e else f"{a}≠{e}"
            return f"{text:>{w}}"

        if "error" in actual:
            print(f"{case.name:<19}ERROR {actual['error']}")
            print(report.get("stdout", ""), report.get("stderr", ""))
            continue
        print(f"{case.name:<19}{actual['town']:<12}{cell('agreed', 6)}"
              f"{cell('contradicted', 7)}{cell('applications', 5)}"
              f"{cell('found', 6)}{cell('unrepaired', 5)}"
              f"{cell('refused', 8)}  {'ok' if ok else 'MISMATCH'}"
              + ("" if actual["verified"] else "  (verify failed)"))
        if not ok:
            print(f"    expected {expected}\n    actual   {actual}")
        if args.evidence:
            save_evidence(report, args.evidence / case.name, case, meta)
    print("\ncolumns: Town's verdict; self-vs-observed facts agreed and"
          " contradicted; 'applied q-1' commits in the memory's history;"
          " outside edits the seller found on restart; outside edits left"
          " in the files; resends Town refused. a≠e marks actual≠expected.")
    print(f"\n{len(cases) - failures} of {len(cases)} cases matched.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
