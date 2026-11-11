#!/usr/bin/env python3
"""A Town buyer that admits a seller only if its qualification card says so.

Standard-library Python, no nandatown import. Reads TOWN_URL, RUN_ID,
NAME, TOKEN, DEADLINE from the environment.

    qualifying_buyer.py --card PATH

Exit codes: 0 correct quote; 4 wrong total; 5 no response (as Town's own
buyer); 6 card valid but does not qualify; 7 card invalid.

The card is checked by check_card.py (next to this file). When it does
not pass, the buyer sends the seller one `qualification_refused` message
and NO quote request. When it passes, the buyer does what Town's own
buyer does for the quote task and adds the card reference to its
acknowledgement note, which Town records in the evidence bundle.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _town_http as town  # noqa: E402
from check_card import check_card  # noqa: E402

EXIT_CORRECT, EXIT_INCORRECT, EXIT_NO_RESPONSE = 0, 4, 5
EXIT_NOT_QUALIFIED, EXIT_BAD_CARD = 6, 7


def find_seller(deadline):
    while time.time() < deadline:
        for p in town.call("GET", "/participants"):
            if "quote.read" in p.get("capabilities", []):
                return p["name"]
        time.sleep(0.2)
    return None


def refuse(seller, body):
    town.call("POST", "/messages",
              {"message_id": "qr-1", "to": seller,
               "kind": "qualification_refused", "body": body})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--card", required=True)
    args = parser.parse_args()
    deadline = time.time() + float(os.environ.get("DEADLINE", "25"))

    joined = town.join(os.environ["NAME"], os.environ["TOKEN"])
    task = joined["run"]["task"]
    status, reason, card = check_card(args.card)
    candidate = (card or {}).get("candidate_id") if isinstance(card, dict) else None
    seller = find_seller(deadline)
    if seller is None:
        return EXIT_NO_RESPONSE

    if status in ("card_malformed", "card_hash_mismatch"):
        refuse(seller, {"reason": status, "candidate_id": candidate})
        return EXIT_BAD_CARD
    if status == "not_qualified":
        refuse(seller, {"reason": "not_qualified", "candidate_id": candidate,
                        "failed_checks": card["failed_checks"]})
        return EXIT_NOT_QUALIFIED

    reference = {"benchmark_id": card["benchmark_id"],
                 "candidate_id": card["candidate_id"],
                 "exact_pass": card["exact_pass"],
                 "card_sha256": card["card_sha256"]}
    town.call("POST", "/messages",
              {"message_id": "q-1", "to": seller, "kind": "quote_request",
               "body": {"sku": task["sku"], "quantity": task["quantity"],
                        "unit_price_cents": task["unit_price_cents"]}})
    seen = set()
    outcome = None
    while outcome is None and time.time() < deadline:
        town.call("GET", "/inbox/notify?wait=0.3")
        claim = town.call("POST", "/inbox/claim")
        if claim is None:
            continue
        mid, fence = claim["message_id"], claim["fence"]
        if claim["kind"] != "quote_response":
            note, ack_status = {"reason": "unknown kind"}, "rejected"
        elif mid in seen:
            note, ack_status = {"duplicate": True}, "processed"
        else:
            seen.add(mid)
            total = claim["body"]["total_cents"]
            correct = total == task["expected_total_cents"]
            note = {"correct": correct, "total_cents": total,
                    "expected_total_cents": task["expected_total_cents"],
                    "qualification": reference}
            ack_status = "processed"
            outcome = correct
        try:
            town.call("POST", "/inbox/ack",
                      {"message_id": mid, "fence": fence,
                       "status": ack_status, "note": note})
        except Exception:
            # a lost or fenced ack is redelivered; the journal above
            # then answers it as a duplicate, as Town's own buyer does
            pass
    if outcome is None:
        return EXIT_NO_RESPONSE
    return EXIT_CORRECT if outcome else EXIT_INCORRECT


if __name__ == "__main__":
    sys.exit(main())
