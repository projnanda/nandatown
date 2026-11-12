#!/usr/bin/env python3
"""A Town buyer whose user key signs a mandate, and whose agent key orders only in scope.

Same mailbox contract as the stock buyer (find the seller, request a quote,
check the total, acknowledge with its own correctness assertion), plus the
TrailProof flow around it:

1. the *user key* commits a MANDATE (category, max amount, validity, nonce)
   to the shared ledger as the first event of the order;
2. the quote request names that order and mandate, so the seller can commit
   its OFFER behind it;
3. on the quote response the *agent key* runs ``check_order``: the ledger
   verifies both plaintexts against the committed hashes, runs the scope
   rules, and records ORDER only if the offer is inside the mandate.

The buyer's ``correct`` assertion is Town's total check AND the ledger's
in-scope verdict when a committed offer was presented. Fail-open: when the
ledger is unreachable the purchase goes ahead un-receipted and the note
says ``no_receipt``.

    TRAILPROOF_URL          the ledger's MCP endpoint
    TRAILPROOF_MANDATE_MAX  the mandate's max amount, default 40.00 (two widgets are 39.90)
    TRAILPROOF_CURRENCY     default EUR
    TRAILPROOF_SETTLE       anything but off (the default): after an in-scope
                            ORDER the buyer waits for the parcel, commits a
                            DELIVERY of what it actually found, and if that is
                            not what the offer said opens a dispute, reveals
                            its plaintexts and reports the ledger's verdict
    TRAILPROOF_PARCEL_DIR   where the seller drops parcels
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nandatown.client import TownClient  # noqa: E402
from nandatown.participants.base import Journal, run_loop  # noqa: E402

from trailproof_link import (  # noqa: E402
    LedgerUnreachable,
    Party,
    ToolError,
    TrailProofClient,
    commit,
    iso_z,
    read_parcel,
    wait_for,
)

RUNTIME = "trailproof-buyer"
# Settlement runs inside the claim, so it must end well inside Town's lease.
SETTLE_BUDGET = 3.5
EXIT_CORRECT = 0
EXIT_INCORRECT = 4
EXIT_NO_RESPONSE = 5


def find_seller(client: TownClient, capability: str = "quote.read") -> str | None:
    for p in client.participants():
        if capability in p.get("capabilities", []):
            return p["name"]
    return None


class Ledger:
    """The buyer's two keys on the ledger, or nothing when it is unreachable."""

    def __init__(self, url: str | None, name: str):
        self.url = url
        self.user: Party | None = None
        self.agent: Party | None = None
        self.detail = "ledger not configured" if not url else ""
        if not url:
            return
        try:
            client = TrailProofClient(url)
            client.initialize()
            self.user = Party(client, "user", f"user:{name}")
            self.agent = Party(client, "buyer_agent", "town-buyer")
            self.user.register()
            self.agent.register()
        except (LedgerUnreachable, ToolError) as exc:
            self.user = self.agent = None
            self.detail = str(exc)[:200]

    @property
    def up(self) -> bool:
        return self.agent is not None


def settle(ledger: Ledger, order_id: str, offer: dict[str, Any], parcel_dir: str) -> dict[str, Any]:
    """After the ORDER: take delivery, and dispute it on the ledger if it is not what was offered.

    Returns the note fragment Town records: the DELIVERY receipt and, when a
    case was opened, its id, the DISPUTE receipt and the ledger's verdict.
    """
    agent, user = ledger.agent, ledger.user
    deadline = time.time() + SETTLE_BUDGET
    out: dict[str, Any] = {"delivered": False}

    def parcel_arrived():
        shipped = any(e["type"] == "FULFILMENT" for e in agent.trail(order_id)["events"])
        return read_parcel(parcel_dir, order_id) if shipped else None

    try:
        parcel = wait_for(parcel_arrived, deadline)
        if not parcel:
            return dict(out, detail="no parcel within the settlement budget")
        as_offered = all(str(parcel.get(k)) == str(offer.get(k)) for k in ("sku", "qty", "unit"))
        delivery = {"received": True, "sku": parcel.get("sku"), "qty": parcel.get("qty"), "unit": parcel.get("unit"),
                    "note": "" if as_offered else f"offer said {offer.get('sku')}"}
        rec = agent.record(order_id, "DELIVERY", delivery, prev=agent.head(order_id))
        out.update(delivered=True, as_offered=as_offered, delivery_commit=rec["commit"], delivery_receipt=rec["receipt"])
        if as_offered:
            return out
        opened = agent.open_dispute(order_id, "not_as_described",
                                    f"received {parcel.get('sku')}, offer was {offer.get('sku')}", prev=rec["commit"])
        case_id = opened["case_id"]
        out.update(case_id=case_id, dispute_receipt=opened["receipt"], dispute_commit=commit(opened["dispute_payload"]))
        for party in (user, agent):
            for event_id in list(party.vault):
                party.reveal(case_id, event_id)

        def decided():
            b = agent.bundle(case_id)
            done = b["status"] == "closed" or any(f["code"] == "TAMPERED" for f in b["findings"])
            return b if done else None

        bundle = wait_for(decided, deadline) or agent.bundle(case_id)
        out.update(case_status=bundle["status"], verdict=bundle["verdict"], remedy=bundle["remedy"],
                   findings=sorted({f["code"] for f in bundle["findings"]}))
    except ToolError as exc:
        out.update(error=exc.code, detail=exc.detail)
    except LedgerUnreachable as exc:
        out.update(no_receipt=True, detail=str(exc)[:200])
    return out


def run(client: TownClient, name: str, token: str, state_dir: str, deadline_seconds: float,
        grant_json: str | None, ledger_url: str | None, max_amount: str, currency: str,
        settle_mode: str = "off", parcel_dir: str = "") -> int:
    client.join_auto(name, token, grant_json)
    task = client.run_context["task"]
    seller = find_seller(client)
    if seller is None:
        return EXIT_NO_RESPONSE

    request_id = "q-1"
    order_id = f"town:{client.run_id}:{request_id}"
    body: dict[str, Any] = {"sku": task["sku"], "quantity": task["quantity"],
                            "unit_price_cents": task["unit_price_cents"]}
    ledger = Ledger(ledger_url, name)
    mandate: dict[str, Any] | None = None
    mandate_commit: str | None = None
    tp_setup: dict[str, Any] = {"no_receipt": True, "detail": ledger.detail}
    if ledger.up:
        mandate = {"user_id": name, "categories": [task["sku"]], "max_amount": max_amount, "currency": currency,
                   "merchants": "*", "valid_until": iso_z(time.time() + 86400), "nonce": f"{client.run_id}:{request_id}"}
        try:
            rec = ledger.user.record(order_id, "MANDATE", mandate, prev=None)
            mandate_commit = rec["commit"]
            body["trailproof"] = {"order_id": order_id, "mandate_commit": mandate_commit, "currency": currency,
                                  "user_party_id": ledger.user.party_id, "agent_party_id": ledger.agent.party_id}
            tp_setup = {"order_id": order_id, "mandate_commit": mandate_commit, "mandate_receipt": rec["receipt"],
                        "user_party_id": ledger.user.party_id, "agent_party_id": ledger.agent.party_id,
                        "ledger_public_key": ledger.user.ledger_public_key}
        except (LedgerUnreachable, ToolError) as exc:
            tp_setup = {"no_receipt": True, "detail": str(exc)[:200]}
            mandate = None

    client.send(message_id=request_id, to=seller, kind="quote_request", body=body)
    journal = Journal(os.path.join(state_dir, "journal.db"))
    outcome: dict[str, Any] = {}

    def handler(claim: dict[str, Any]):
        if claim["kind"] != "quote_response":
            return "rejected", {"reason": "unknown kind"}, []
        if journal.seen(claim["message_id"]):
            return "processed", {"duplicate": True, "runtime": RUNTIME}, []
        total = claim["body"]["total_cents"]
        total_ok = total == task["expected_total_cents"]
        tp = dict(tp_setup)
        offered = claim["body"].get("trailproof") if isinstance(claim["body"].get("trailproof"), dict) else None
        in_scope: bool | None = None
        if mandate is not None and offered and offered.get("order_id") == order_id:
            offer = offered.get("offer") or {}
            tp["offer_commit"] = offered.get("offer_commit")
            tp["offer_commit_matches_plaintext"] = commit(offer) == offered.get("offer_commit")
            try:
                res = ledger.agent.check_order(order_id, mandate, offer, prev=offered.get("offer_commit"),
                                               offer_commit=offered.get("offer_commit"))
                in_scope = bool(res["in_scope"])
                tp.update(in_scope=in_scope, reasons=res.get("reasons", []), order_commit=res.get("order_commit"),
                          order_receipt=res.get("receipt"))
                if in_scope and settle_mode != "off":
                    tp["settlement"] = settle(ledger, order_id, offer, parcel_dir)
            except ToolError as exc:
                in_scope = False
                tp.update(in_scope=False, reasons=[{"code": exc.code, "detail": exc.detail}])
            except LedgerUnreachable as exc:
                tp.update(no_receipt=True, detail=str(exc)[:200])
        elif mandate is not None:
            tp.update(in_scope=None, seller_committed=False, detail="seller answered without a TrailProof offer")
        correct = total_ok and in_scope is not False
        journal.record(claim["message_id"], {"correct": correct})
        outcome["correct"] = correct
        return "processed", {"correct": correct, "total_cents": total, "expected_total_cents": task["expected_total_cents"],
                             "runtime": RUNTIME, "trailproof": tp}, []

    deadline = time.time() + deadline_seconds
    run_loop(client, handler, until=lambda: "correct" in outcome or time.time() > deadline)
    if "correct" not in outcome:
        return EXIT_NO_RESPONSE
    return EXIT_CORRECT if outcome["correct"] else EXIT_INCORRECT


def main() -> None:
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    code = run(client, env["NAME"], env["TOKEN"], env["STATE_DIR"], float(env.get("DEADLINE", "25")),
               env.get("TOWN_GRANT"), env.get("TRAILPROOF_URL"), env.get("TRAILPROOF_MANDATE_MAX", "40.00"),
               env.get("TRAILPROOF_CURRENCY", "EUR"), env.get("TRAILPROOF_SETTLE", "off") or "off",
               env.get("TRAILPROOF_PARCEL_DIR", ""))
    sys.exit(code)


if __name__ == "__main__":
    main()
