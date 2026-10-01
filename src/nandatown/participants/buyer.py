"""The stock buyer: discovers the seller, requests a quote, checks it.

The buyer's correctness assertion is its own attributed fact: it travels
as the note on the buyer's processed acknowledgement of the quote
response. The town records it but cannot synthesize it.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from ..client import TownClient
from .base import Journal, run_loop

EXIT_CORRECT = 0
EXIT_INCORRECT = 4
EXIT_NO_RESPONSE = 5
EXIT_DEAD_LETTER = 6


def find_seller(client: TownClient, capability: str = "quote.read") -> str | None:
    for p in client.participants():
        if capability in p.get("capabilities", []):
            return p["name"]
    return None


def run(client: TownClient, name: str, token: str, state_dir: str,
        deadline_seconds: float = 25.0,
        grant_json: str | None = None) -> int:
    client.join_auto(name, token, grant_json)
    task = client.run_context["task"]
    seller = find_seller(client)
    if seller is None:
        return EXIT_NO_RESPONSE
    client.send(
        message_id="q-1", to=seller, kind="quote_request",
        body={"sku": task["sku"], "quantity": task["quantity"],
              "unit_price_cents": task["unit_price_cents"]},
    )
    journal = Journal(os.path.join(state_dir, "journal.db"))
    outcome: dict[str, Any] = {}

    def handler(claim: dict[str, Any]):
        if claim["kind"] == "dead_letter":
            # Only the town ends a request this way, and only one we sent.
            if (claim["from"] != "town"
                    or claim["body"].get("request_id") != "q-1"):
                return "rejected", {"reason": "not my dead letter"}, []
            return "processed", {"dead_letter": True}, []
        if claim["kind"] != "quote_response":
            return "rejected", {"reason": "unknown kind"}, []
        if journal.seen(claim["message_id"]):
            return "processed", {"duplicate": True}, []
        total = claim["body"]["total_cents"]
        correct = total == task["expected_total_cents"]
        journal.record(claim["message_id"], {"correct": correct})
        outcome["correct"] = correct
        return "processed", {"correct": correct, "total_cents": total,
                             "expected_total_cents":
                                 task["expected_total_cents"]}, []

    def settled(claim: dict[str, Any], note: dict[str, Any]) -> None:
        # Stop only once the town has recorded the notice's ack.
        if note.get("dead_letter") is True:
            outcome["dead_letter"] = True

    deadline = time.time() + deadline_seconds
    run_loop(client, handler,
             until=lambda: ("correct" in outcome or "dead_letter" in outcome
                            or time.time() > deadline),
             on_ack_accepted=settled)
    if "dead_letter" in outcome:
        return EXIT_DEAD_LETTER
    if "correct" not in outcome:
        return EXIT_NO_RESPONSE
    return EXIT_CORRECT if outcome["correct"] else EXIT_INCORRECT


def main() -> None:
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    code = run(client, env["NAME"], env["TOKEN"], env["STATE_DIR"],
               deadline_seconds=float(env.get("DEADLINE", "25")),
               grant_json=env.get("TOWN_GRANT"))
    sys.exit(code)


if __name__ == "__main__":
    main()
