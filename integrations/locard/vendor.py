#!/usr/bin/env python3
"""A coverage-gated vendor for Town's Track quote task: no valid Locard policy, no quote.

The seller role. For each quote_request it calls Locard's /v1/verify with the
buyer's policy token, agent id and the order's value, before doing any work:

  covered        -> quote_response {request_id, total_cents}; ack processed, applied=true
  not covered    -> quote_refused {request_id, reasons};      ack rejected,  applied=false
  Locard down    -> refused as well (reason locard_unreachable): fail closed, never serve blind

The Locard verdict travels in the acknowledgement note, so it lands in Town's
evidence bundle as an attributed fact of this participant. Redelivered work is
recognised through Town's journal and answered with the original reply, without
a second verification.

    nandatown run quote-clean \\
      --agent seller=cmd:"python integrations/locard/vendor.py" \\
      --agent buyer=cmd:"python integrations/locard/buyer.py"

Options: --locard-url URL (default http://127.0.0.1:8700).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from locard_client import DEFAULT_URL, LocardRefused, LocardUnavailable, verify  # noqa: E402
from nandatown.client import TownClient  # noqa: E402
from nandatown.participants.base import Journal, run_loop  # noqa: E402


def decide(locard_url: str, body: dict[str, Any]) -> dict[str, Any]:
    """The coverage decision for one quote request, before any work is done."""
    total = int(body.get("quantity", 0)) * int(body.get("unit_price_cents", 0))
    coverage = body.get("coverage") or {}
    if coverage.get("provider") != "locard" or not coverage.get("policy_token"):
        return {"covered": False, "total_cents": total, "reasons": ["coverage_required"],
                "locard": None}
    try:
        verdict = verify(locard_url, coverage["policy_token"], coverage.get("agent_id", ""), total)
    except (LocardUnavailable, LocardRefused) as exc:
        return {"covered": False, "total_cents": total, "reasons": ["locard_unreachable"],
                "locard": {"error": str(exc)[:200]}}
    covered = bool(verdict.get("valid") and verdict.get("covers_transaction"))
    return {"covered": covered, "total_cents": total,
            "reasons": [] if covered else (verdict.get("reasons") or ["not_covered"]),
            "locard": verdict}


def make_handler(locard_url: str, journal: Journal, log_path: Path):
    def handler(claim: dict[str, Any]):
        mid = claim["message_id"]
        if claim["kind"] != "quote_request":
            return "rejected", {"reason": "unknown kind"}, []
        prior = journal.get(mid)
        if prior is not None:
            # Redelivered work: resend the same reply, with no second verification. The ack
            # says only "duplicate": repeating applied=true would tell Town (and a ledger
            # downstream) that the order was applied twice. Town's duplicate-delivery
            # profile caught exactly that in an earlier version of this vendor.
            return prior["status"], {"duplicate": True}, [prior["reply"]]
        decision = decide(locard_url, claim["body"])
        with log_path.open("a") as log:
            log.write(json.dumps({"at": time.time(), "message_id": mid, **decision}) + "\n")
        if decision["covered"]:
            reply = {"message_id": "r-" + mid.removeprefix("q-"), "to": claim["from"],
                     "kind": "quote_response",
                     "body": {"request_id": mid, "total_cents": decision["total_cents"]}}
            status = "processed"
            note = {"applied": True, "total_cents": decision["total_cents"],
                    "coverage_verified": True,
                    "policy_status": decision["locard"].get("status")}
        else:
            reply = {"message_id": "x-" + mid.removeprefix("q-"), "to": claim["from"],
                     "kind": "quote_refused",
                     "body": {"request_id": mid, "reasons": decision["reasons"]}}
            status = "rejected"
            note = {"applied": False, "coverage_verified": False,
                    "reasons": decision["reasons"],
                    "policy_status": (decision["locard"] or {}).get("status")}
        journal.record(mid, {"status": status, "note": note, "reply": reply})
        return status, note, [reply]
    return handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--locard-url", default=DEFAULT_URL)
    args = ap.parse_args()
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    state = Path(env["STATE_DIR"])
    journal = Journal(str(state / "vendor_journal.db"))
    deadline = time.time() + float(env.get("DEADLINE", "25"))
    run_loop(client, make_handler(args.locard_url, journal, state / "locard_decisions.jsonl"),
             until=lambda: time.time() > deadline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
