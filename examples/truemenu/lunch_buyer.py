#!/usr/bin/env python3
"""A lunch-ordering buyer that checks TrueMenu quotes for itself.

It plays the buyer role in a Track run: it finds the seller through the
town's participant list, sends one quote_request carrying a menu_request
(area, budget, dislikes, diet, meal time), and judges the quote_response.

For a quoted response it picks one offer the way the TrueMenu lunch agent
prefers (a main dish first, then the cheapest), then verifies that offer's
quote directly with TrueMenu's public POST /v1/verify_quote. That second
channel does not go through the seller or through Town, so a seller that
altered a price or forwarded a forged quote id would be caught here. The
buyer's verdict is its own attributed assertion: Town records it on the
buyer's acknowledgement and does not compute it.

TRUEMENU_EXPECT says what a correct answer looks like for this run:
"quote" (a verified offer that respects the request) or "decline" (no
offer, for example because every source is stale). The default request
is a low-carb lunch near Kendall Square under $20 with no pork or avocado
on Monday 16 November 2026 at 12:30; TRUEMENU_REQUEST overrides it with a
JSON object.

The deterministic pick stands in for the TrueMenu lunch agent's model
decision step, which is not exercised here.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from nandatown.client import TownClient
from nandatown.participants.base import Journal, run_loop
from nandatown.participants.buyer import find_seller

EXIT_CORRECT, EXIT_INCORRECT, EXIT_NO_RESPONSE = 0, 4, 5
MAIN_CATEGORIES = ("bowl", "salad", "plate", "main", "pita", "pizza")
DEFAULT_REQUEST = {
    "near": "Kendall Square", "budget_usd_max": 20,
    "exclude": ["pork", "avocado"], "diet": ["low_carb"],
    "available_at": "2026-11-16T12:30",
}

Verify = Callable[[str], dict[str, Any]]


def http_verify(url: str, timeout: float = 10.0) -> Verify:
    def call(quote_id: str) -> dict[str, Any]:
        req = urllib.request.Request(
            url.rstrip("/") + "/v1/verify_quote",
            data=json.dumps({"quote_id": quote_id}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"error": type(exc).__name__}
    return call


def pick(offers: list[dict[str, Any]]) -> dict[str, Any]:
    mains = [o for o in offers if o.get("category") in MAIN_CATEGORIES]
    return min(mains or offers, key=lambda o: o["total_cents"])


def judge(body: dict[str, Any], request: dict[str, Any], expect: str,
          verify: Verify) -> dict[str, Any]:
    """The buyer's note for one quote_response body."""
    status = body.get("status")
    if status == "declined":
        return {"correct": expect == "decline", "status": status,
                "reason": body.get("reason"), "expect": expect}
    offers = body.get("offers") or []
    if status != "quoted" or not offers:
        return {"correct": False, "status": status,
                "reason": "response is neither a quote nor a decline",
                "expect": expect}
    chosen = pick(offers)
    v = verify(chosen["quote_id"])
    budget = request.get("budget_usd_max")
    checks = {
        "signature_valid": v.get("valid_signature") is True,
        "issued_by_truemenu": v.get("issued_by_this_service") is True,
        "not_expired": v.get("expired") is False,
        "price_unchanged_since_quote": v.get("price_unchanged") is True,
        "same_item_as_offer": v.get("item_id") == chosen["item_id"],
        "offer_price_matches_quote":
            v.get("quoted_price") is not None
            and round(v["quoted_price"] * 100) == chosen["total_cents"],
        "within_budget": budget is None
            or chosen["total_cents"] <= round(float(budget) * 100),
        # TrueMenu labels a source fresh, aging or stale and quotes only
        # the first two, so "aging" is a normal answer between syncs.
        "source_not_stale": chosen.get("freshness") in ("fresh", "aging"),
    }
    return {"correct": expect == "quote" and all(checks.values()),
            "status": status, "expect": expect,
            "item_id": chosen["item_id"], "merchant_id": chosen["merchant_id"],
            "total_cents": chosen["total_cents"],
            "truemenu_verdict": v.get("verdict"), "checks": checks}


def main() -> int:
    env = os.environ
    request = (json.loads(env["TRUEMENU_REQUEST"])
               if env.get("TRUEMENU_REQUEST") else DEFAULT_REQUEST)
    expect = env.get("TRUEMENU_EXPECT", "quote")
    verify = http_verify(env.get("TRUEMENU_URL", "http://127.0.0.1:8088"))
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    deadline = time.time() + float(env.get("DEADLINE", "25"))

    seller = None
    while seller is None and time.time() < deadline:
        seller = find_seller(client)
        if seller is None:
            time.sleep(0.3)
    if seller is None:
        return EXIT_NO_RESPONSE
    client.send(message_id="q-1", to=seller, kind="quote_request",
                body={"sku": "truemenu-lunch", "quantity": 1,
                      "menu_request": request})

    journal = Journal(os.path.join(env["STATE_DIR"], "journal.db"))
    outcome: dict[str, Any] = {}

    def handler(claim: dict[str, Any]):
        if claim["kind"] != "quote_response":
            return "rejected", {"reason": "unknown kind"}, []
        if journal.seen(claim["message_id"]):
            return "processed", {"duplicate": True}, []
        note = judge(claim["body"], request, expect, verify)
        journal.record(claim["message_id"], note)
        outcome.update(note)
        print(json.dumps(note, indent=1), file=sys.stderr)
        return "processed", note, []

    run_loop(client, handler,
             until=lambda: "correct" in outcome or time.time() > deadline)
    if "correct" not in outcome:
        return EXIT_NO_RESPONSE
    return EXIT_CORRECT if outcome["correct"] else EXIT_INCORRECT


if __name__ == "__main__":
    sys.exit(main())
