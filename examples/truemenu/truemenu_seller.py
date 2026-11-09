#!/usr/bin/env python3
"""TrueMenu as a Town seller: answer quote requests from real menu data.

TrueMenu (https://github.com/IntelCodes/truemenu) is a separately hosted
service that gives ordering agents merchant-sourced menu items, prices
and open-now status, with a freshness label on every source and a signed
quote any party can check. This adapter lets it play the seller role in
a Track run without moving it into Town:

    quote_request  {"menu_request": {near, budget_usd_max, exclude, diet,
                                     include, available_at}}
      -> POST {TRUEMENU_URL}/v1/find_items          (X-Agent-Key)
    quote_response {"request_id", "status": "quoted", "offers": [...]}
                or {"request_id", "status": "declined", "reason", ...}

Each offer carries TrueMenu's own quote_id. The adapter never invents a
price: when TrueMenu returns no quotable item (stale source, closed
branch, unverified price, nothing in budget) the response is a decline
with TrueMenu's reasons. A request without a menu_request, such as Town's
reference widget quote, is declined as unsupported rather than guessed.

Duplicate delivery: TrueMenu signs a new quote on every find_items call,
so a second call for the same request would produce different content
under the same response identity. The adapter journals the first reply
and resends it unchanged, the same rule as Town's reference seller.
TRUEMENU_ADAPTER_NAIVE=1 turns the journal off; it is the negative
control, not a supported mode.

Environment: Town supplies TOWN_URL, RUN_ID, NAME, TOKEN, STATE_DIR,
DEADLINE. The operator supplies TRUEMENU_URL (default
http://127.0.0.1:8088) and TRUEMENU_AGENT_KEY (a TrueMenu key with the
read scope). The key is sent only to TRUEMENU_URL and never recorded.
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
from nandatown.participants.seller import ack_accepted, response_id

ADAPTER_VERSION = "truemenu-town-seller@0.1"
MAX_OFFERS = 5
ALLOWED_FIELDS = ("near", "budget_usd_max", "exclude", "diet", "include",
                  "available_at")

FindItems = Callable[[dict[str, Any]], dict[str, Any]]


class Unavailable(Exception):
    """TrueMenu could not be reached or answered with an error."""


def http_find_items(url: str, key: str, timeout: float = 10.0) -> FindItems:
    def call(query: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            url.rstrip("/") + "/v1/find_items",
            data=json.dumps(query).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Agent-Key": key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise Unavailable(f"TrueMenu answered HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise Unavailable(f"TrueMenu unreachable: {type(exc).__name__}"
                              ) from None
    return call


def to_query(menu_request: dict[str, Any]) -> dict[str, Any]:
    query = {k: menu_request[k] for k in ALLOWED_FIELDS
             if k in menu_request}
    query["limit"] = 8
    return query


def offer(item: dict[str, Any]) -> dict[str, Any]:
    price = item["price"]
    return {
        "merchant_id": item["merchant"]["id"],
        "merchant_name": item["merchant"]["name"],
        "item_id": item["item_id"],
        "name": item["name"],
        "category": item.get("category"),
        "total_cents": round(price["amount"] * 100),
        "currency": price["currency"],
        "price_source": price.get("source"),
        "freshness": item["freshness"]["label"],
        "data_age_s": item["freshness"].get("age_s"),
        "quote_id": item["quote_id"],
    }


def answer(request_id: str, body: Any, find_items: FindItems
           ) -> dict[str, Any]:
    """The quote_response body for one request. Pure apart from find_items."""
    menu_request = body.get("menu_request") if isinstance(body, dict) else None
    if not isinstance(menu_request, dict):
        return {"request_id": request_id, "status": "declined",
                "reason": "unsupported_request",
                "detail": "TrueMenu quotes menu items; send a menu_request"}
    try:
        found = find_items(to_query(menu_request))
    except Unavailable as exc:
        return {"request_id": request_id, "status": "declined",
                "reason": "truemenu_unavailable", "detail": str(exc)}
    results = found.get("results", [])
    quoted = [offer(r) for r in results if r.get("quote_id")][:MAX_OFFERS]
    if quoted:
        return {"request_id": request_id, "status": "quoted",
                "offers": quoted,
                "evaluated_at": found.get("evaluated_at")}
    refused = sorted({r.get("quote_refused_reason") or "unknown"
                      for r in results})
    return {"request_id": request_id, "status": "declined",
            "reason": "no_verifiable_quote" if results else "no_match",
            "matching_items": found.get("count", len(results)),
            "quote_refused": refused,
            "dropped": found.get("dropped", {}),
            "evaluated_at": found.get("evaluated_at")}


def build_handler(journal: Journal | None, find_items: FindItems):
    def handler(claim: dict[str, Any]):
        if claim["kind"] != "quote_request":
            return "rejected", {"reason": "unknown kind"}, []
        message_id = claim["message_id"]
        if journal is not None and journal.seen(message_id):
            done = journal.get(message_id)
            note: dict[str, Any] = {"duplicate": True}
            if journal.unreported(message_id) and claim.get("duplicate"):
                journal.mark_reported(message_id)
            if journal.unreported(message_id):
                note["applied"] = True
                note["status"] = done["reply"]["body"]["status"]
            return "processed", note, [done["reply"]]
        body = answer(message_id, claim.get("body"), find_items)
        reply = {"message_id": response_id(message_id), "to": claim["from"],
                 "kind": "quote_response", "body": body}
        if journal is not None:
            journal.record(message_id, {"reply": reply}, unreported=True)
        note = {"applied": True, "status": body["status"],
                "offers": len(body.get("offers", [])),
                "adapter": ADAPTER_VERSION}
        if body["status"] == "declined":
            note["reason"] = body["reason"]
        return "processed", note, [reply]

    return handler


def main() -> int:
    env = os.environ
    key = env.get("TRUEMENU_AGENT_KEY", "")
    if not key:
        print("TRUEMENU_AGENT_KEY is not set", file=sys.stderr)
        return 2
    find_items = http_find_items(env.get("TRUEMENU_URL",
                                         "http://127.0.0.1:8088"), key)
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    naive = env.get("TRUEMENU_ADAPTER_NAIVE") == "1"
    journal = None if naive else Journal(
        os.path.join(env["STATE_DIR"], "journal.db"))
    deadline = time.time() + float(env.get("DEADLINE", "45"))
    run_loop(client, build_handler(journal, find_items),
             until=lambda: time.time() > deadline,
             on_ack_accepted=None if journal is None else ack_accepted(journal))
    return 0


if __name__ == "__main__":
    sys.exit(main())
