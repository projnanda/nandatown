#!/usr/bin/env python3
"""Cross-check a Town Track bundle against the Fervo server's event log.

Town's evaluator judges the quote exchange. This script judges the side
effect: for the one quote request in the bundle, Fervo must hold exactly
one claim, one node and one merge by the Town seller, main's score.txt
must have risen once (10 -> 20 with the default step), and the node the
seller reported to Town must be the node Fervo merged.

    python check.py runs/<run-id> [--fervo URL] [--work DIR] [--out FILE]

The Fervo event log needs the operator (human) token, read from
$FERVO_WORK/tokens/operator.json as written by setup_fervo.sh. The check
assumes the server was reset for this run (setup_fervo.sh does this), so
every object the Town seller authored belongs to it.

Exit status: 0 when every check holds, 1 when any fails. The fresh-keys
controls are expected to exit 1.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request

SELLER_ACTOR = "town-seller"


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def town_summary(bundle):
    with open(os.path.join(bundle, "run.json"), encoding="utf-8") as f:
        run = json.load(f)
    with open(os.path.join(bundle, "result.json"), encoding="utf-8") as f:
        result = json.load(f)
    events = read_jsonl(os.path.join(bundle, "events.jsonl"))
    requests = [e["subject"] for e in events
                if e["kind"] == "message_accepted"
                and e["detail"].get("kind") == "quote_request"]
    seller = "seller"
    # A claim through the inbox records message_claimed; the injected
    # re-offer of completed work records only duplicate_offered.
    deliveries = [e for e in events if e["subject"] in requests and (
        (e["kind"] == "message_claimed"
         and e["detail"].get("claimant") == seller)
        or e["kind"] == "duplicate_offered")]
    acks = [e for e in events if e["kind"] == "ack_recorded"
            and e["observer"] == seller and e["subject"] in requests
            and e["detail"].get("status") == "processed"]
    reported = [a["detail"]["note"].get("fervo", {}) for a in acks
                if isinstance(a["detail"].get("note"), dict)]
    return {
        "run_id": run["run_id"],
        "profile": run["profile_name"],
        "verdict": result["verdict"],
        "requests": requests,
        "deliveries": len(deliveries),
        "stale_fences": sum(1 for e in events
                            if e["kind"] == "stale_fence_rejected"),
        "restarts": sum(1 for e in events
                        if e["kind"] == "participant_restarted"),
        "processed_acks": len(acks),
        "reported_nodes": sorted({r.get("node") for r in reported
                                  if r.get("node")}),
    }


def fervo_events(url, token):
    req = urllib.request.Request(f"{url}/events?after=0")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main_score(clone):
    subprocess.run(["git", "-C", clone, "fetch", "--quiet", "origin"],
                   check=True)

    def show(rev):
        out = subprocess.run(["git", "-C", clone, "show", f"{rev}:score.txt"],
                             capture_output=True, text=True, check=True)
        return int(out.stdout.strip())

    root = subprocess.run(["git", "-C", clone, "rev-list", "--max-parents=0",
                           "origin/main"], capture_output=True, text=True,
                          check=True).stdout.split()[0]
    return show(root), show("origin/main")


def fervo_summary(events):
    by_seller = [e for e in events if e["actor"] == SELLER_ACTOR]
    claims = [e["subject"] for e in by_seller if e["type"] == "claim_created"]
    nodes = [e["subject"] for e in by_seller if e["type"] == "node_submitted"]
    merged = [e for e in events if e["type"] == "node_merged"
              and e["subject"] in nodes]
    postmerge_jobs = [e["subject"] for e in events if e["type"] == "job_queued"
                      and e["data"].get("kind") == "postmerge"
                      and e["data"].get("node") in nodes]
    done_jobs = {e["subject"] for e in events if e["type"] == "job_done"}
    return {
        "claims": claims,
        "nodes": nodes,
        "merged": [e["subject"] for e in merged],
        "merged_commits": [e["data"].get("commit") for e in merged],
        "needs_rebase": [e["subject"] for e in events
                         if e["type"] == "needs_rebase"
                         and e["subject"] in nodes],
        "reverted": [e["subject"] for e in events
                     if e["type"] in ("node_reverted", "postmerge_failed")
                     and e["subject"] in nodes],
        "postmerge_done": [j for j in postmerge_jobs if j in done_jobs],
        "events_seen": len(events),
    }


def checks(town, fervo, scores, step):
    start, end = scores
    return [
        ("one quote request in the bundle", len(town["requests"]) == 1,
         f"{len(town['requests'])} accepted"),
        ("exactly one Fervo claim", len(fervo["claims"]) == 1,
         f"{len(fervo['claims'])} claims for {town['deliveries']} Town"
         " deliveries"),
        ("exactly one Fervo node", len(fervo["nodes"]) == 1,
         f"{len(fervo['nodes'])} nodes"),
        ("exactly one merge", len(fervo["merged"]) == 1,
         f"{len(fervo['merged'])} merges"),
        ("no rebase cycle", not fervo["needs_rebase"],
         f"needs_rebase on {fervo['needs_rebase'] or 'none'}"),
        ("main score rose once", end - start == step,
         f"score.txt {start} -> {end}, expected +{step}"),
        ("postmerge check passed, not reverted",
         bool(fervo["postmerge_done"]) and not fervo["reverted"],
         f"postmerge done {len(fervo['postmerge_done'])},"
         f" reverted {fervo['reverted'] or 'none'}"),
        ("Town's reported node is the merged node",
         bool(fervo["merged"]) and town["reported_nodes"] == fervo["merged"],
         f"reported {town['reported_nodes']}, merged {fervo['merged']}"),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("bundle")
    parser.add_argument("--fervo", default=os.environ.get(
        "FERVO_URL", "http://127.0.0.1:8775"))
    parser.add_argument("--work", default=os.environ.get("FERVO_WORK"),
                        help="setup_fervo.sh work directory")
    parser.add_argument("--clone", default=os.environ.get("FERVO_CLONE"))
    parser.add_argument("--step", type=int,
                        default=int(os.environ.get("FERVO_STEP", "10")))
    parser.add_argument("--out", help="write the summary as JSON here")
    parser.add_argument("--events-out",
                        help="write Fervo's event log as JSON lines here")
    args = parser.parse_args(argv)
    if not args.work:
        parser.error("--work or FERVO_WORK is required")
    clone = args.clone or os.path.join(args.work, "clone")
    with open(os.path.join(args.work, "tokens", "operator.json")) as f:
        token = json.load(f)["token"]

    town = town_summary(args.bundle)
    events = fervo_events(args.fervo.rstrip("/"), token)
    fervo = fervo_summary(events)
    scores = main_score(clone)
    results = checks(town, fervo, scores, args.step)

    print(f"Town run {town['run_id']} ({town['profile']}): verdict"
          f" {town['verdict']}, {town['deliveries']} deliveries,"
          f" {town['stale_fences']} stale fences, {town['restarts']} restarts")
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
    passed = all(ok for _, ok, _ in results)
    print("Fervo applied the contribution exactly once" if passed else
          "Fervo did NOT apply the contribution exactly once")

    summary = {"town": town, "fervo": fervo,
               "score": {"start": scores[0], "end": scores[1]},
               "checks": [{"name": n, "passed": ok, "detail": d}
                          for n, ok, d in results],
               "passed": passed}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
            f.write("\n")
    if args.events_out:
        with open(args.events_out, "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, sort_keys=True) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
