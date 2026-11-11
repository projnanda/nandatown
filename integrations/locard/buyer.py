#!/usr/bin/env python3
"""An insured buyer for Town's Track quote task: it gets Locard cover, then asks for a quote.

The buyer role, playing the Locard MVP's procurement agent. Before it messages
the vendor it goes through Locard end to end: submit its manifest and trace
history for underwriting, take the quote, bind it, and receive a signed policy
token. It then sends Town's quote_request with the token attached and judges the
reply exactly like Town's stock buyer (the correctness note on its ack).

Modes (--mode):
  insured   attach a valid policy                                   (default)
  violate   after binding, report an over-cap purchase to Locard's live
            monitoring; Locard suspends the policy, then the buyer asks anyway
  tamper    attach the policy token with its signature altered

Exit codes: 0 quote correct, 4 quote wrong, 5 no reply, 6 the vendor refused,
7 Locard would not bind cover.

    nandatown run quote-clean \\
      --agent seller=cmd:"python integrations/locard/vendor.py" \\
      --agent buyer=cmd:"python integrations/locard/buyer.py --mode insured"

Options: --locard-url URL (default http://127.0.0.1:8700); --agent-id ID gives
the agent a fresh identity so runs against one Locard database don't collide
(Locard refuses new cover to an agent it has suspended).
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

from locard_client import (DEFAULT_URL, LocardRefused, LocardUnavailable,  # noqa: E402
                           load_packet, obtain_cover, report_event)
from nandatown.client import TownClient  # noqa: E402
from nandatown.participants.base import Journal, run_loop  # noqa: E402
from nandatown.participants.buyer import find_seller  # noqa: E402

EXIT_CORRECT, EXIT_INCORRECT, EXIT_NO_RESPONSE, EXIT_REFUSED, EXIT_NO_COVER = 0, 4, 5, 6, 7


def tampered(token: str) -> str:
    """The same JWT with the last characters of its signature changed."""
    head, _, sig = token.rpartition(".")
    flipped = "".join("A" if c != "A" else "B" for c in sig[-6:])
    return f"{head}.{sig[:-6]}{flipped}"


def get_cover(args, state: Path) -> dict[str, Any] | None:
    """Locard end to end: underwrite, quote, bind; then misbehave if asked to."""
    record: dict[str, Any] = {"mode": args.mode, "locard_url": args.locard_url}
    try:
        cover = obtain_cover(args.locard_url, load_packet(args.agent_id))
        record["cover"] = {k: v for k, v in cover.items() if k not in ("policy_token", "monitoring")}
        if args.mode == "violate":
            cap = cover["spend_cap_per_tx_cents"] or 0
            event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "agent_id": cover["agent_id"], "run_id": os.environ.get("RUN_ID", ""),
                     "tool": "purchase", "args": {"item": "server-rack", "amount_cents": cap + 50_000},
                     "outcome": "ok", "amount_cents": cap + 50_000, "input_flags": []}
            record["monitoring_report"] = report_event(args.locard_url, cover, event)
        if args.mode == "tamper":
            cover["policy_token"] = tampered(cover["policy_token"])
            record["token_tampered"] = True
        return cover
    except (LocardUnavailable, LocardRefused) as exc:
        record["error"] = str(exc)[:300]
        return None
    finally:
        (state / "locard_buyer.json").write_text(json.dumps(record, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mode", choices=["insured", "violate", "tamper"], default="insured")
    ap.add_argument("--locard-url", default=DEFAULT_URL)
    ap.add_argument("--agent-id", default=None)
    args = ap.parse_args()
    env = os.environ
    state = Path(env["STATE_DIR"])
    deadline = time.time() + float(env.get("DEADLINE", "25"))

    cover = get_cover(args, state)
    if cover is None:
        return EXIT_NO_COVER

    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    task = client.run_context["task"]
    seller = None
    while seller is None and time.time() < deadline:
        seller = find_seller(client)
        if seller is None:
            time.sleep(0.2)
    if seller is None:
        return EXIT_NO_RESPONSE
    client.send(message_id="q-1", to=seller, kind="quote_request", body={
        "sku": task["sku"], "quantity": task["quantity"],
        "unit_price_cents": task["unit_price_cents"],
        "coverage": {"provider": "locard", "agent_id": cover["agent_id"],
                     "policy_token": cover["policy_token"]},
    })

    journal = Journal(str(state / "journal.db"))
    outcome: dict[str, Any] = {}

    def handler(claim: dict[str, Any]):
        if journal.seen(claim["message_id"]):
            return "processed", {"duplicate": True}, []
        if claim["kind"] == "quote_refused":
            journal.record(claim["message_id"], {"refused": True})
            outcome["refused"] = claim["body"].get("reasons", [])
            return "processed", {"refused": True, "reasons": outcome["refused"]}, []
        if claim["kind"] != "quote_response":
            return "rejected", {"reason": "unknown kind"}, []
        total = claim["body"]["total_cents"]
        correct = total == task["expected_total_cents"]
        journal.record(claim["message_id"], {"correct": correct})
        outcome["correct"] = correct
        return "processed", {"correct": correct, "total_cents": total,
                             "expected_total_cents": task["expected_total_cents"]}, []

    run_loop(client, handler,
             until=lambda: bool(outcome) or time.time() > deadline)
    if "refused" in outcome:
        return EXIT_REFUSED
    if "correct" not in outcome:
        return EXIT_NO_RESPONSE
    return EXIT_CORRECT if outcome["correct"] else EXIT_INCORRECT


if __name__ == "__main__":
    sys.exit(main())
