#!/usr/bin/env python3
"""The observer lane: what Town's record says the seller did, checked
against what the seller's memory says it did.

    python examples/gitmem/gitmem_observe.py runs/<run-id>

Reads the bundle's events.jsonl (Town's coordinator record, which the
seller never writes) and the seller's gitmem store under
state/seller/memory. For each quote request it derives, from events
alone:

    quote.<id>.reply_id           the response Town accepted from the seller
    quote.<id>.reply_fingerprint  Town's fingerprint of that response body
    quote.<id>.total_cents        the total the buyer acknowledged receiving

Each observed fact quotes the event it came from and passes the same
write contract as the seller's own facts, into the same store, in the
`observed` lane. Then the two lanes are reconciled, and the store's
history is read for two things the evaluator cannot see:

    applications   how many times the seller's memory committed
                   "applied <id>": more than one means the work was done
                   twice, even when Town absorbed the second answer
    outside edits  fact files that differ from the store's last commit
                   and that the store did not write

Writes BUNDLE/gitmem-check.json and prints a summary. This file is not one
of the five canonical records and `nandatown verify` ignores it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gitmem_seller import (  # noqa: E402
    HOLDER, SCHEMA, Contract, RejectedWrite, Store, outside_edits,
)
from nandatown.records import fingerprint  # noqa: E402

OBSERVER = "observer"
KEYS = ("reply_id", "reply_fingerprint", "total_cents")


def load_events(bundle: Path) -> list[dict]:
    with open(bundle / "events.jsonl") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _line(event: dict) -> str:
    return json.dumps({k: event[k] for k in
                       ("event_id", "kind", "observer", "subject", "detail")
                       if k in event}, sort_keys=True)


def observed_facts(events: list[dict], run_id: str) -> dict[str, dict]:
    """request id -> key -> (value, event) from Town's events only."""
    out: dict[str, dict] = {}
    requests = [e["subject"] for e in events
                if e["kind"] == "message_accepted"
                and e["detail"].get("kind") == "quote_request"]
    for rid in requests:
        facts: dict = {}
        resp = next((e for e in events
                     if e["kind"] == "message_accepted"
                     and e["detail"].get("kind") == "quote_response"
                     and e["detail"].get("sender") == HOLDER
                     and e["detail"].get("request_id") == rid), None)
        if resp is not None:
            facts["reply_id"] = (resp["subject"], resp)
            facts["reply_fingerprint"] = (
                resp["detail"]["content_fingerprint"], resp)
            ack = next((e for e in events
                        if e["kind"] == "ack_recorded"
                        and e["observer"] != HOLDER
                        and e["subject"] == resp["subject"]
                        and isinstance(e["detail"].get("note"), dict)
                        and "total_cents" in e["detail"]["note"]), None)
            if ack is not None:
                facts["total_cents"] = (ack["detail"]["note"]["total_cents"],
                                        ack)
            refused = [e for e in events
                       if e["kind"] == "identity_reuse_rejected"
                       and e["subject"] == resp["subject"]
                       and e["detail"].get("sender") == HOLDER]
            if refused:
                facts["refused_resends"] = (len(refused), refused[0])
        out[rid] = facts
    return out


def self_facts(store: Store, rid: str) -> dict:
    """What the seller would believe now: the files as they stand."""
    facts: dict = {}
    reply_fact = store.current(HOLDER, f"quote.{rid}.reply", "self")
    if reply_fact is not None:
        reply = reply_fact.value
        if isinstance(reply, str):
            try:
                reply = json.loads(reply)
            except json.JSONDecodeError:
                reply = None
        if isinstance(reply, dict):
            facts["reply_id"] = reply.get("message_id")
            facts["reply_fingerprint"] = fingerprint(reply.get("body"))
    total = store.current(HOLDER, f"quote.{rid}.total_cents", "self")
    if total is not None:
        facts["total_cents"] = total.value
    return facts


def applications(store: Store, rid: str) -> int:
    subjects = store._git("log", "--format=%s").splitlines()
    return sum(1 for s in subjects if s.endswith(f"(applied {rid})"))


def check(bundle: Path) -> dict:
    events = load_events(bundle)
    run = json.loads((bundle / "run.json").read_text())
    run_id = run.get("run_id") or bundle.name
    root = bundle / "state" / HOLDER / "memory"
    if not (root / ".git").exists():
        return {"run_id": run_id, "error": f"no gitmem store at {root}"}
    store = Store(root)
    # Read the tamper signal before this observer writes anything.
    edits_at_end = outside_edits(store)

    contract = Contract(str(SCHEMA))
    agents = {HOLDER, "buyer"}
    observed = observed_facts(events, run_id)
    ops, refused = [], []
    for rid, facts in observed.items():
        for name, (value, event) in facts.items():
            op = {"op": "ADD", "lane": "observed", "holder": HOLDER,
                  "asserter": OBSERVER, "key": f"quote.{rid}.{name}",
                  "value": value, "subject": rid,
                  "evidence": _line(event),
                  "source": f"{run_id}#{event['event_id']}"}
            try:
                ops.append(contract.check(op, agents))
            except RejectedWrite as exc:
                refused.append({"key": op["key"], "reason": str(exc)})
    changes = store.apply(ops, source=f"{run_id}#events.jsonl", when="9.00")
    commit = store.commit(changes, source=f"{run_id}#events.jsonl",
                          title="observer: derived from Town's events",
                          when="9.00")

    # Late import: reconcile lives in the observer module of gitmem.
    from gitmem.observer import reconcile

    per_request = {}
    for rid, facts in observed.items():
        mine = self_facts(store, rid)
        theirs = {k: v for k, (v, _) in facts.items() if k in KEYS}
        rec = reconcile(mine, theirs)
        for finding in rec["findings"]:
            finding["self"] = mine.get(finding["key"])
            finding["observed"] = theirs.get(finding["key"])
        per_request[rid] = {
            "self": mine, "observed": theirs,
            "reconcile": rec,
            "applications": applications(store, rid),
            "town_refused_resends": facts.get("refused_resends", (0,))[0],
        }

    seller_report = None
    for e in events:
        if (e["kind"] == "ack_recorded" and e["observer"] == HOLDER
                and isinstance(e["detail"].get("note"), dict)
                and "gitmem" in e["detail"]["note"]):
            seller_report = e["detail"]["note"]["gitmem"]
    result = json.loads((bundle / "result.json").read_text())
    return {
        "run_id": run_id,
        "town_verdict": result.get("verdict"),
        "seller_reported": seller_report,
        "outside_edits_unrepaired": edits_at_end,
        "observer_commit": commit,
        "observer_refused": refused,
        "requests": per_request,
        "history": store.log(20),
    }


def summarize(report: dict) -> str:
    if "error" in report:
        return f"{report['run_id']}: {report['error']}"
    lines = [f"run {report['run_id']}: Town verdict {report['town_verdict']}"]
    seller = report.get("seller_reported") or {}
    found = seller.get("outside_edits") or []
    lines.append(
        f"  seller policy {seller.get('policy', '?')}: found {len(found)}"
        f" outside edit(s) on restart"
        + (", restored from git" if seller.get("restored") else ""))
    unrepaired = report["outside_edits_unrepaired"]
    lines.append(f"  outside edits still in the files: {len(unrepaired)}"
                 + (f" ({', '.join(e['change'] + ' ' + e['path'] for e in unrepaired)})"
                    if unrepaired else ""))
    for rid, r in report["requests"].items():
        c = r["reconcile"]["counts"]
        lines.append(f"  {rid}: applications in history {r['applications']},"
                     f" town refused resends {r['town_refused_resends']}")
        lines.append(f"    self vs observed: {c['agreed']} agreed,"
                     f" {c['contradiction']} contradicted,"
                     f" {c['omission']} missing from memory,"
                     f" {c['unsupported']} unsupported")
        for f in r["reconcile"]["findings"]:
            if f["kind"] != "agreed":
                lines.append(f"      {f['kind']}: {f['key']} memory="
                             f"{f['self']!r} town={f['observed']!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--json", type=Path,
                        help="where to write the report (default"
                             " BUNDLE/gitmem-check.json)")
    args = parser.parse_args(argv)
    report = check(args.bundle)
    out = args.json or args.bundle / "gitmem-check.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(summarize(report))
    return 1 if "error" in report else 0


if __name__ == "__main__":
    sys.exit(main())
