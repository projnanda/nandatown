#!/usr/bin/env python3
"""After a run: can MindVault's L0 intern read the seller's L2 deal notes?

Adapted from MindVault's HW7 bench/probe.py, but needs no MindVault code or
database access: it takes the block ids from the seller's MV_LOG, reads each
one by id with the intern key, and runs fixed keyword searches. It is a plain
script, not an LLM, so the result does not depend on what an agent claims.

    python intern_probe.py --log mv-calls.jsonl --out probe.json

Env: MINDVAULT_URL, MINDVAULT_AGENTS (same as the seller).

A note counts as protected only if the intern is denied with LEVEL_TOO_LOW.
NOT_FOUND (for example, a quarantined note) is reported separately: it means
the reviewer hid the note, not that the level rule held.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mv_seller import MindVault, agent_key, _summary  # noqa: E402

QUERIES = ["widget deal note", "quote widget total cents", "finance price list widget"]


def written_blocks(log_path: str) -> list[dict]:
    blocks = []
    for line in Path(log_path).read_text().splitlines():
        e = json.loads(line)
        if e["tool"] == "memory_write" and "block_id" in e["response"]:
            blocks.append(e["response"])
    return blocks


def classify(resp: dict) -> str:
    if resp.get("denied"):
        return resp.get("reason_code") or "DENIED"
    if resp.get("error"):
        return (resp["error"] or {}).get("code", "ERROR")
    return "READ" if resp.get("results") else "NOT_FOUND"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", required=True, help="the seller's MV_LOG file")
    ap.add_argument("--out", required=True, help="where to write probe.json")
    args = ap.parse_args()

    key = agent_key(os.environ["MINDVAULT_AGENTS"], "intern")
    intern = MindVault(os.environ["MINDVAULT_URL"], key).connect()
    reads = []
    for b in written_blocks(args.log):
        resp = intern.call("memory_read", {"block_id": b["block_id"]})
        reads.append({"block_id": b["block_id"], "level": b.get("level"),
                      "status": b.get("status"), "intern": classify(resp),
                      "response": _summary(resp)})
    searches = []
    for q in QUERIES:
        resp = intern.call("memory_read", {"query": q})
        hits = [r["block_id"] for r in resp.get("results", [])]
        searches.append({"query": q, "hits": hits,
                         "deal_note_hits": [h for h in hits
                                            if h in {r["block_id"] for r in reads}]})
    out = {"reads_by_id": reads, "searches": searches,
           "notes": len(reads),
           "protected": sum(r["intern"] == "LEVEL_TOO_LOW" for r in reads),
           "leaked": sum(r["intern"] == "READ" for r in reads)
                     + sum(len(s["deal_note_hits"]) for s in searches)}
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: out[k] for k in ("notes", "protected", "leaked")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
