#!/usr/bin/env python3
"""Turn a check.sh output directory into the results tables and verify it.

    python results.py runs/cerata > runs/cerata/RESULTS.md

For every Track bundle it runs `nandatown verify` (hashes plus evaluator
replay) and prints, per section, expected vs observed with the decision
Cerata returned, the buyer's failed checks if any, and the bundle
fingerprint. Exit status 0 only if every bundle verifies.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "runs/cerata"
    rows = list(csv.DictReader(open(os.path.join(out, "summary.tsv")), delimiter="\t"))
    bad_verify = 0
    by_section: dict[str, list[str]] = {}
    for r in rows:
        decision, failed, fp, verified = "", "", "", ""
        bundle = os.path.join(out, "bundles", r["bundle"])
        if r["bundle"].startswith("run-") and os.path.isdir(bundle):
            fp = json.load(open(os.path.join(bundle, "manifest.json"))).get("bundle_fingerprint", "")
            ok = subprocess.run(["nandatown", "verify", bundle], capture_output=True, text=True).returncode == 0
            verified = "yes" if ok else "NO"
            bad_verify += 0 if ok else 1
            for line in open(os.path.join(bundle, "events.jsonl")):
                e = json.loads(line)
                if e.get("kind") == "ack_recorded" and e.get("observer") == "buyer":
                    note = (e.get("detail") or {}).get("note") or {}
                    decision = note.get("decision") or note.get("status") or ""
                    failed = ", ".join(k for k, v in (note.get("checks") or {}).items() if not v)
        short = fp.replace("sha256:", "")[:12] + "…" if fp else r["bundle"]
        by_section.setdefault(r["section"], []).append(
            f"| {r['case']} | {r['profile']} | {r['expected']} | {r['observed']} | {decision} | "
            f"{failed or '—'} | {r['match']} | {verified or '—'} | `{short}` |")
    total, matched = len(rows), sum(r["match"] == "yes" for r in rows)
    print(f"**{matched} of {total} cases matched their expected verdict; "
          f"{'every bundle verifies' if bad_verify == 0 else f'{bad_verify} bundle(s) failed verify'}.**\n")
    for section, lines in by_section.items():
        print(f"### {section}\n")
        print("| case | profile | expected | observed | Cerata decision | failed buyer checks | match | `verify` | bundle |")
        print("|---|---|---|---|---|---|---|---|---|")
        print("\n".join(lines) + "\n")
    mcp = os.path.join(out, "mcp.json")
    if os.path.exists(mcp):
        m = json.load(open(mcp))
        print("### mcp steps\n")
        for s in m["steps"]:
            print(f"- {'✓' if s['ok'] else '✗'} {s['step']}")
    return 0 if bad_verify == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
