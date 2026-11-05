#!/usr/bin/env python3
"""The TrailProof compatibility check: Town's quote task with TrailProof underneath.

Starts one TrailProof ledger (from ``TRAILPROOF_HOME``), runs Town Track
profiles with the TrailProof buyer and seller as ``cmd:`` participants, and
cross-checks the two evidence trails:

* Town's bundle verifies and its stage verdicts are the expected ones;
* every ledger receipt the participants put in their acknowledgement notes
  verifies offline with the ledger public key, against the committed hash;
* the seller's committed OFFER is the one the buyer checked;
* the live TrailProof chain for the order has the expected event types and
  the event ids recorded in Town's bundle.

    TRAILPROOF_HOME=/path/to/TrailProof python examples/trailproof/run_check.py
    python examples/trailproof/run_check.py --cases clean,duplicate --record

``--record`` writes the summary and the canonical bundle files under
``examples/trailproof/evidence/``; the default keeps them in a temporary
directory. Exit status is 0 only when every case matches its expectation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from nandatown import __version__ as TOWN_VERSION  # noqa: E402
from nandatown.bundle import load_bundle, verify_bundle  # noqa: E402
from nandatown.runner import run_town  # noqa: E402

from trailproof_link import (  # noqa: E402
    TrailProofClient,
    start_server,
    stop_server,
    verify_receipt,
)

BUNDLE_FILES = ("profile.json", "run.json", "intents.jsonl", "events.jsonl", "result.json", "manifest.json",
                "attestation.json")

# name -> (profile, participant env, expectation)
CASES: dict[str, dict[str, Any]] = {
    "clean": {
        "profile": "quote-clean",
        "env": {},
        "about": "baseline: mandate, committed offer, in-scope order, Town passes",
        "verdict": "passed",
        "stages": {"correct": "passed"},
        "chain": ["MANDATE", "OFFER", "ORDER"],
        "buyer": {"in_scope": True},
    },
    "duplicate": {
        "profile": "quote-duplicate-delivery",
        "env": {},
        "about": "Town redelivers the request; the seller commits one OFFER, not two",
        "verdict": "passed",
        "stages": {"correct": "passed", "duplicate_recognized": "passed"},
        "chain": ["MANDATE", "OFFER", "ORDER"],
        "buyer": {"in_scope": True},
    },
    "lying_commit": {
        "profile": "quote-clean",
        "env": {"TRAILPROOF_SELLER_MODE": "lying_commit"},
        "about": "the seller's committed OFFER differs from the plaintext it sent; check_order refuses",
        "verdict": "failed",
        "stages": {"response": "passed", "correct": "failed"},
        "chain": ["MANDATE", "OFFER"],
        "buyer": {"in_scope": False, "reason": "COMMIT_MISMATCH"},
    },
    "over_budget": {
        "profile": "quote-clean",
        "env": {"TRAILPROOF_MANDATE_MAX": "30.00"},
        "about": "the user's mandate is 30.00, the quote is 39.90; the agent does not order",
        "verdict": "failed",
        "stages": {"response": "passed", "correct": "failed"},
        "chain": ["MANDATE", "OFFER"],
        "buyer": {"in_scope": False, "reason": "OVER_BUDGET"},
    },
    "ledger_down": {
        "profile": "quote-clean",
        "env": {"TRAILPROOF_URL": "http://127.0.0.1:1/mcp"},
        "about": "the ledger is unreachable; both agents fail open and the note says no_receipt",
        "verdict": "passed",
        "stages": {"correct": "passed"},
        "chain": None,
        "buyer": {"no_receipt": True},
    },
}


def participant_commands() -> dict[str, list[str]]:
    return {role: [sys.executable, os.path.join(HERE, f"trailproof_{role}.py")] for role in ("buyer", "seller")}


def ack_notes(bundle: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"buyer": [], "seller": []}
    for e in bundle["events"]:
        if e.kind == "ack_recorded" and e.observer in out and isinstance(e.detail.get("note"), dict):
            out[e.observer].append(e.detail["note"])
    return out


def run_case(name: str, ledger_url: str, out_dir: str) -> dict[str, Any]:
    case = CASES[name]
    env_backup = dict(os.environ)
    os.environ["TRAILPROOF_URL"] = ledger_url
    os.environ.update(case["env"])
    try:
        bundle_dir, result = run_town(case["profile"], out_dir, external=participant_commands())
    finally:
        os.environ.clear()
        os.environ.update(env_backup)
    bundle = load_bundle(bundle_dir)
    notes = ack_notes(bundle)
    seller_tp = next((n["trailproof"] for n in notes["seller"] if n.get("applied") and "trailproof" in n), {})
    buyer_tp = next((n["trailproof"] for n in notes["buyer"] if "correct" in n and "trailproof" in n), {})
    observed: dict[str, Any] = {
        "verdict": result.verdict,
        "stages": {s.name: s.status for s in result.stages},
        "bundle_verify": verify_bundle(bundle_dir),
        "bundle_dir": bundle_dir,
        "run_id": bundle["run"].run_id,
        "seller_note": seller_tp,
        "buyer_note": buyer_tp,
    }
    checks: dict[str, bool] = {
        "town verdict as expected": result.verdict == case["verdict"],
        "town bundle verifies": observed["bundle_verify"] == [],
    }
    for stage, status in case["stages"].items():
        checks[f"stage {stage} {status}"] = observed["stages"].get(stage) == status

    exp_buyer = case["buyer"]
    if "in_scope" in exp_buyer:
        checks["buyer in_scope as expected"] = buyer_tp.get("in_scope") is exp_buyer["in_scope"]
    if "reason" in exp_buyer:
        codes = [r.get("code") for r in buyer_tp.get("reasons", [])]
        checks[f"buyer refused with {exp_buyer['reason']}"] = exp_buyer["reason"] in codes
    if exp_buyer.get("no_receipt"):
        checks["buyer failed open (no_receipt)"] = buyer_tp.get("no_receipt") is True
        checks["seller committed nothing"] = seller_tp.get("committed") is False

    if case["chain"] is not None:
        key = buyer_tp.get("ledger_public_key")
        checks["one ledger key seen by both agents"] = bool(key) and key == seller_tp.get("ledger_public_key")
        checks["seller committed the offer the buyer checked"] = (
            bool(seller_tp.get("offer_commit")) and seller_tp.get("offer_commit") == buyer_tp.get("offer_commit"))
        receipts = [("MANDATE", buyer_tp.get("mandate_receipt"), buyer_tp.get("mandate_commit")),
                    ("OFFER", seller_tp.get("receipt"), seller_tp.get("offer_commit"))]
        if buyer_tp.get("order_receipt"):
            receipts.append(("ORDER", buyer_tp["order_receipt"], buyer_tp.get("order_commit")))
        for typ, receipt, c in receipts:
            checks[f"{typ} receipt verifies offline"] = bool(receipt and c and key) and verify_receipt(key, receipt, c)
        # The live chain, read back from the ledger by the buyer agent's party id.
        client = TrailProofClient(ledger_url)
        client.initialize()
        trail = client.call("get_trail", order_id=buyer_tp["order_id"], party_id=buyer_tp["agent_party_id"])
        types = [e["type"] for e in trail["events"]]
        checks["ledger chain is " + ",".join(case["chain"])] = types == case["chain"]
        linked = all((e["prev"] == (trail["events"][i - 1]["commit"] if i else None)) for i, e in enumerate(trail["events"]))
        checks["ledger chain prev links hold"] = linked
        ids_in_town = {r["event_id"] for _, r, _ in receipts if r}
        checks["town-recorded receipts are the chain's events"] = ids_in_town <= {e["event_id"] for e in trail["events"]}
        observed["trail"] = [{k: e[k] for k in ("type", "party_id", "commit", "prev", "event_id", "ledger_ts")}
                             for e in trail["events"]]
    return {"case": name, "about": case["about"], "profile": case["profile"], "env": case["env"],
            "expected": {k: v for k, v in case.items() if k in ("verdict", "stages", "chain", "buyer")},
            "observed": observed, "checks": checks, "ok": all(checks.values())}


def copy_bundle(bundle_dir: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    for name in BUNDLE_FILES:
        src = os.path.join(bundle_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, name))


def git_head(path: str) -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True,
                              timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=",".join(CASES), help="comma-separated subset of: " + ", ".join(CASES))
    ap.add_argument("--record", action="store_true", help="write summary.json and bundles under examples/trailproof/evidence")
    ap.add_argument("--out", default=None, help="where to write (default: temp dir, or evidence/ with --record)")
    args = ap.parse_args(argv)
    home = os.environ.get("TRAILPROOF_HOME")
    if not home:
        print("set TRAILPROOF_HOME to a TrailProof checkout", file=sys.stderr)
        return 2
    out = args.out or (os.path.join(HERE, "evidence") if args.record else tempfile.mkdtemp(prefix="trailproof-town-"))
    os.makedirs(out, exist_ok=True)
    os.environ.setdefault("NANDATOWN_HOME", tempfile.mkdtemp(prefix="townhome-"))

    process, url = start_server(home)
    results = []
    try:
        print(f"TrailProof ledger at {url}  (TRAILPROOF_HOME={home})")
        print(f"nandatown {TOWN_VERSION} @ {git_head(os.path.dirname(os.path.dirname(HERE))) or 'unknown commit'}")
        for name in [c.strip() for c in args.cases.split(",") if c.strip()]:
            started = time.time()
            r = run_case(name, url, os.path.join(out, "runs"))
            r["seconds"] = round(time.time() - started, 1)
            results.append(r)
            print()
            print(f"[{'OK ' if r['ok'] else 'FAIL'}] {name}: {r['about']}  ({r['profile']}, {r['seconds']}s)")
            print(f"       expected verdict {r['expected']['verdict']}, observed {r['observed']['verdict']};"
                  f" chain {r['expected']['chain']} -> {[e['type'] for e in r['observed'].get('trail', [])] or 'n/a'}")
            for check, ok in r["checks"].items():
                print(f"       [{'PASS' if ok else 'FAIL'}] {check}")
            if args.record:
                copy_bundle(r["observed"]["bundle_dir"], os.path.join(out, name))
    finally:
        stop_server(process)

    summary = {
        "nandatown_version": TOWN_VERSION,
        "nandatown_commit": git_head(os.path.dirname(os.path.dirname(HERE))),
        "trailproof_home_commit": git_head(home),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cases": [{k: v for k, v in r.items() if k != "observed"} | {
            "observed": {k: v for k, v in r["observed"].items() if k != "bundle_dir"}} for r in results],
        "all_ok": all(r["ok"] for r in results),
    }
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
    print()
    print(f"{sum(r['ok'] for r in results)}/{len(results)} cases as expected; summary in {os.path.join(out, 'summary.json')}")
    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
