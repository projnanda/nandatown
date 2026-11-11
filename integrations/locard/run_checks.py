#!/usr/bin/env python3
"""Run the Locard compatibility checks through Town Track and compare expected vs observed.

Needs a Locard service (https://github.com/zaccytoymas/locard-mvp, `locard/`)
reachable at --locard-url. From the repository root, with Town installed:

    python integrations/locard/run_checks.py --locard-version <locard-mvp commit>

Each case is one `nandatown run` with this integration's vendor in the seller role
and a buyer (ours, or Town's own scripted buyer). For each case it reads the
evidence bundle Town wrote: the stage verdict, the vendor's acknowledgement note
(which carries Locard's verdict), the buyer's exit code, and how many times the
vendor asked Locard. It runs `nandatown verify` on every bundle, copies the
bundles' canonical files to --evidence, and writes RESULTS.md there.

Exit code 0 only if every case behaved as expected and every bundle verified.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BUNDLE_FILES = ("profile.json", "run.json", "intents.jsonl", "events.jsonl", "result.json",
                "manifest.json", "report.md")
DEAD_LOCARD = "http://127.0.0.1:9"  # nothing listens here: Locard is unreachable


def cases(py: str, stamp: str, locard: str):
    vendor = f"cmd:{py} integrations/locard/vendor.py --locard-url {locard}"
    buyer = lambda mode, tag: (f"cmd:{py} integrations/locard/buyer.py --mode {mode}"  # noqa: E731
                               f" --locard-url {locard} --agent-id procurement-bot-{tag}-{stamp}")
    return [
        {"name": "insured-buyer", "profile": "quote-clean",
         "what": "Insured agent asks the coverage-gated vendor for a quote",
         "seller": vendor, "buyer": buyer("insured", "insured"),
         "expect": {"verdict": "passed", "covered": True, "buyer_exit": 0, "locard_calls": 1}},
        {"name": "town-buyer-uninsured", "profile": "quote-clean",
         "what": "Town's own scripted buyer (another agent, no Locard policy) asks the same vendor",
         "seller": vendor, "buyer": "scripted",
         "expect": {"verdict": "not passed", "covered": False, "reason": "coverage_required",
                    "locard_calls": 0}},
        {"name": "suspended-after-violation", "profile": "quote-clean",
         "what": "Insured agent reports an over-cap purchase to Locard monitoring, then asks",
         "seller": vendor, "buyer": buyer("violate", "violate"),
         "expect": {"verdict": "not passed", "covered": False,
                    "reason": "policy_suspended:over_spend_cap", "buyer_exit": 6,
                    "locard_calls": 1}},
        {"name": "tampered-token", "profile": "quote-clean",
         "what": "Insured agent presents its policy token with the signature altered",
         "seller": vendor, "buyer": buyer("tamper", "tamper"),
         "expect": {"verdict": "not passed", "covered": False, "reason": "invalid_signature",
                    "buyer_exit": 6, "locard_calls": 1}},
        {"name": "locard-unreachable", "profile": "quote-clean",
         "what": "Valid policy, but the vendor cannot reach Locard (fail closed)",
         "seller": f"cmd:{py} integrations/locard/vendor.py --locard-url {DEAD_LOCARD}",
         "buyer": buyer("insured", "down"),
         "expect": {"verdict": "not passed", "covered": False, "reason": "locard_unreachable",
                    "buyer_exit": 6, "locard_calls": 1}},
        {"name": "duplicate-delivery", "profile": "quote-duplicate-delivery",
         "what": "Town offers the same quote request twice (fault); coverage checked once",
         "seller": vendor, "buyer": buyer("insured", "dup"),
         "expect": {"verdict": "passed", "covered": True, "buyer_exit": 0, "locard_calls": 1}},
        {"name": "lost-ack", "profile": "quote-lost-ack",
         "what": "Town drops the vendor's first acknowledgement (fault); the retry is safe",
         "seller": vendor, "buyer": buyer("insured", "lostack"),
         "expect": {"verdict": "passed", "covered": True, "buyer_exit": 0, "locard_calls": 1}},
    ]


def observe(bundle: Path) -> dict:
    result = json.loads((bundle / "result.json").read_text())
    events = [json.loads(line) for line in (bundle / "events.jsonl").read_text().splitlines()]
    vendor_notes = [e["detail"] for e in events
                    if e["kind"] == "ack_recorded" and e["observer"] == "seller"
                    and e["subject"].startswith("q-")]
    first = next((n for n in vendor_notes if not n.get("note", {}).get("duplicate")), {})
    note = first.get("note", {})
    buyer_exit = next((e["detail"].get("exit_code") for e in events
                       if e["kind"] == "participant_exited" and e["subject"] == "buyer"), None)
    decisions = bundle / "state" / "seller" / "locard_decisions.jsonl"
    broken = next((s for s in result["stages"] if s["status"] not in ("passed", "not_tested")), None)
    return {
        "verdict": "passed" if result["verdict"] == "passed" else "not passed",
        "first_broken_stage": broken["name"] if broken else None,
        "covered": note.get("coverage_verified"),
        "reasons": note.get("reasons", []),
        "policy_status": note.get("policy_status"),
        "vendor_acks": [(n.get("status"), bool(n.get("note", {}).get("duplicate"))) for n in vendor_notes],
        "buyer_exit": buyer_exit,
        # one decision per new (not redelivered) request; `locard` is null when there was no
        # policy to check, so the vendor never called Locard
        "locard_calls": sum(json.loads(line)["locard"] is not None
                            for line in decisions.read_text().splitlines()) if decisions.exists() else 0,
    }


def matches(expect: dict, seen: dict) -> list[str]:
    misses = []
    for key, want in expect.items():
        got = seen["reasons"] if key == "reason" else seen.get(key)
        ok = (want in got) if key == "reason" else (got == want)
        if not ok:
            misses.append(f"{key}: expected {want!r}, observed {got!r}")
    return misses


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--locard-url", default="http://127.0.0.1:8700")
    ap.add_argument("--locard-version", default="unrecorded",
                    help="the locard-mvp commit the service runs, recorded in RESULTS.md")
    ap.add_argument("--out", default=None, help="where Town writes bundles (default: a temp dir)")
    ap.add_argument("--evidence", default=str(HERE / "evidence"))
    ap.add_argument("--only", default=None, help="comma-separated case names")
    args = ap.parse_args()

    py = shlex.quote(sys.executable)  # Town splits cmd: with shlex, so quote paths with spaces
    nandatown = str(Path(sys.executable).with_name("nandatown"))
    stamp = time.strftime("%Y%m%d%H%M%S")
    runs = Path(args.out or f"/tmp/locard-town-checks-{stamp}")
    evidence = Path(args.evidence)
    town_commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                 capture_output=True, text=True).stdout.strip() or "unknown"
    town_version = subprocess.run([nandatown, "--version"], capture_output=True,
                                  text=True).stdout.strip()
    wanted = set(args.only.split(",")) if args.only else None

    rows, all_ok = [], True
    for case in cases(py, stamp, args.locard_url):
        if wanted and case["name"] not in wanted:
            continue
        out = runs / case["name"]
        cmd = [nandatown, "run", case["profile"], "--agent", f"seller={case['seller']}",
               "--agent", f"buyer={case['buyer']}", "--out", str(out)]
        started = time.time()
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        bundle = next(out.glob("run-*"), None)
        if bundle is None:
            print(f"FAIL {case['name']}: Town wrote no bundle\n{proc.stdout[-800:]}{proc.stderr[-800:]}")
            all_ok = False
            continue
        seen = observe(bundle)
        verified = subprocess.run([nandatown, "verify", str(bundle)], capture_output=True,
                                  text=True).returncode == 0
        misses = matches(case["expect"], seen) + ([] if verified else ["nandatown verify failed"])
        all_ok &= not misses
        dest = evidence / case["name"]
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for f in BUNDLE_FILES:
            if (bundle / f).exists():
                shutil.copy2(bundle / f, dest / f)
        rows.append({"case": case, "seen": seen, "verified": verified, "misses": misses,
                     "run_id": bundle.name, "seconds": round(time.time() - started, 1)})
        print(f"{'OK  ' if not misses else 'FAIL'} {case['name']:<26} verdict={seen['verdict']:<10} "
              f"covered={seen['covered']} reasons={seen['reasons']} buyer_exit={seen['buyer_exit']} "
              f"locard_calls={seen['locard_calls']} verify={'ok' if verified else 'FAILED'}"
              + (f"  <- {misses}" if misses else ""))

    lines = [
        "# Locard compatibility check results", "",
        f"- Town: `{town_version}`, commit `{town_commit}` (evaluator from the bundles: "
        f"`{json.loads((evidence / rows[0]['case']['name'] / 'result.json').read_text())['evaluator_version']}`)"
        if rows else "",
        f"- Locard service: locard-mvp `{args.locard_version}` at `{args.locard_url}`",
        f"- Run: {time.strftime('%Y-%m-%d %H:%M %Z')}, `python integrations/locard/run_checks.py`", "",
        "| Case | Profile | What it tests | Expected | Observed | Town verdict | Bundle verifies |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        c, s = r["case"], r["seen"]
        exp = "quote served" if c["expect"]["covered"] else f"refused ({c['expect']['reason']})"
        obs = ("quote served" if s["covered"] else f"refused ({', '.join(s['reasons'])})") + \
              f"; Locard asked {s['locard_calls']}x"
        if s["buyer_exit"] is not None:
            obs += f"; buyer exit {s['buyer_exit']}"
        verdict = s["verdict"] + (f" (first broken: {s['first_broken_stage']})" if s["first_broken_stage"] else "")
        lines.append(f"| {c['name']} {'✅' if not r['misses'] else '❌'} | `{c['profile']}` | {c['what']} | "
                     f"{exp} | {obs} | {verdict} | {'yes' if r['verified'] else 'NO'} (`{r['run_id']}`) |")
    lines += ["", "Every case passes only if all expected fields match. Bundles (canonical files, "
              "without `state/`) are in this folder, one directory per case; check any of them "
              "offline with `nandatown verify integrations/locard/evidence/<case>`."]
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "RESULTS.md").write_text("\n".join(lines) + "\n")
    print(f"\n{'ALL CASES AS EXPECTED' if all_ok else 'SOME CASES DIFFERED'} -> {evidence / 'RESULTS.md'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
