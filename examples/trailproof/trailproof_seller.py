#!/usr/bin/env python3
"""A Town seller that commits every quote to TrailProof before answering.

Same mailbox contract as the stock seller (claim under a lease, apply once,
respond, acknowledge), plus: when the buyer's request names a TrailProof
order, the seller commits its OFFER (sku, qty, unit price, total, terms) to
the shared ledger and hands the plaintext to the buyer inside the Town
reply. The ledger receipt travels in the acknowledgement note, so Town's
evidence bundle and TrailProof's chain can be cross-checked offline.

    TRAILPROOF_URL          the ledger's MCP endpoint (required for commitments)
    TRAILPROOF_SELLER_MODE  honest (default) | lying_commit: the committed OFFER
                            says a different total than the plaintext the buyer
                            receives; the buyer's check_order must catch it
    TRAILPROOF_SETTLE       off (default) | deliver | wrong_product | tamper_reveal.
                            Anything but off: once the buyer's ORDER is on the
                            chain the seller settles on a second thread. Its
                            payment provider commits CHARGE, the seller commits
                            FULFILMENT and drops the parcel. wrong_product ships
                            another sku; on a dispute the seller reveals its
                            plaintexts and refunds when the ledger's verdict is
                            against it. tamper_reveal also ships the wrong sku,
                            then reveals a FULFILMENT claiming the right one.
    TRAILPROOF_PARCEL_DIR   where parcels are dropped (the delivery channel)

Run it through the town together with the TrailProof buyer:

    nandatown run quote-clean \\
        --agent "seller=cmd:python examples/trailproof/trailproof_seller.py" \\
        --agent "buyer=cmd:python examples/trailproof/trailproof_buyer.py"
"""

from __future__ import annotations

import os
import sys
import threading
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
    cents_to_amount,
    commit,
    drop_parcel,
    iso_z,
    wait_for,
)

RUNTIME = "trailproof-seller"
SETTLE_BUDGET = 20.0


def response_id(request_id: str) -> str:
    return "r-" + request_id.removeprefix("q-")


def connect_ledger(url: str | None) -> Party | None:
    """Register the merchant key. Unreachable ledger: sell without receipts, say so."""
    if not url:
        return None
    try:
        client = TrailProofClient(url)
        client.initialize()
        merchant = Party(client, "merchant", "town-seller")
        merchant.register()
        return merchant
    except (LedgerUnreachable, ToolError):
        return None


def settle(merchant: Party, order_id: str, offer_event_id: str, mode: str, parcel_dir: str) -> None:
    """What the seller does after the quote: charge, ship, and answer a dispute.

    Runs on its own thread and coordinates through the ledger alone: it acts
    when the buyer's ORDER appears on the chain and when a case is opened.
    The payment provider is the seller's own (a second key, role ``psp``).
    """

    def event(type_: str):
        return next((e for e in merchant.trail(order_id)["events"] if e["type"] == type_), None)

    def open_case():
        return next((c for c in merchant.trail(order_id)["cases"] if c["status"] == "open"), None)

    try:
        offer = merchant.vault[offer_event_id]
        deadline = time.time() + SETTLE_BUDGET
        if not wait_for(lambda: event("ORDER"), deadline):
            return
        psp = Party(merchant.client, "psp", "town-seller-psp")
        psp.register()
        charge = psp.record(order_id, "CHARGE", {"amount": offer["total"], "currency": offer["currency"],
                                                 "psp_ref": f"ch_{offer['nonce']}"}, prev=merchant.head(order_id))
        shipped_sku = offer["sku"] if mode == "deliver" else offer["sku"] + "-b"
        fulfilment = {"sku": shipped_sku, "qty": offer["qty"], "unit": offer["unit"], "carrier": "town-parcel",
                      "tracking": f"TP{offer['nonce']}"}
        ful = merchant.record(order_id, "FULFILMENT", fulfilment, prev=charge["commit"])
        drop_parcel(parcel_dir, order_id, {"sku": shipped_sku, "qty": offer["qty"], "unit": offer["unit"],
                                           "tracking": fulfilment["tracking"]})

        case = wait_for(open_case, deadline)
        if not case:
            return
        case_id = case["case_id"]
        merchant.reveal(case_id, offer_event_id)
        # tamper_reveal: claim the right sku was shipped. The ledger re-hashes the claim.
        claimed = dict(fulfilment, sku=offer["sku"]) if mode == "tamper_reveal" else None
        merchant.reveal(case_id, ful["receipt"]["event_id"], claimed)
        psp.reveal(case_id, charge["receipt"]["event_id"])
        if mode == "tamper_reveal":
            return  # a seller that doctors its evidence does not refund either

        def complete():  # decide once nothing is owed by the buyer's side
            b = merchant.bundle(case_id)
            return None if any(f["code"] == "UNVERIFIABLE" for f in b["findings"]) else b

        bundle = wait_for(complete, deadline)
        if bundle and bundle["verdict"] == "merchant_at_fault" and bundle["remedy"]["type"] == "refund":
            psp.record(order_id, "REFUND", {"amount": bundle["remedy"]["amount"], "currency": bundle["remedy"]["currency"],
                                            "psp_ref": f"re_{offer['nonce']}", "reason": f"ledger verdict, case {case_id}"},
                       prev=merchant.head(order_id))
    except (LedgerUnreachable, ToolError, KeyError):
        return  # settlement is best effort; the chain shows how far it got


def offer_payload(body: dict[str, Any], request_id: str, currency: str, total_cents: int) -> dict[str, Any]:
    return {
        "sku": body["sku"],
        "title": f"{body['sku']} (Town quote)",
        "category": body["sku"],
        "qty": body["quantity"],
        "unit": "each",
        "unit_price": cents_to_amount(body["unit_price_cents"]),
        "total": cents_to_amount(total_cents),
        "currency": currency,
        "shipping_terms": "town mailbox, immediate",
        "return_terms": "none; synthetic quote",
        "valid_until": iso_z(time.time() + 3600),
        "nonce": request_id,
    }


def build_handler(journal: Journal, merchant: Party | None, mode: str, settle_mode: str = "off",
                  parcel_dir: str = ""):
    def handler(claim: dict[str, Any]):
        if claim["kind"] != "quote_request":
            return "rejected", {"reason": "unknown kind"}, []
        message_id = claim["message_id"]
        if journal.seen(message_id):
            # Already applied: resend the original response, commit nothing twice.
            done = journal.get(message_id)
            note = {"duplicate": True, "runtime": RUNTIME}
            if journal.unreported(message_id):
                if claim.get("duplicate"):
                    journal.mark_reported(message_id)
                else:
                    note["applied"] = True
                    note["total_cents"] = done["total_cents"]
                    note["trailproof"] = done.get("trailproof")
            return "processed", note, [done["reply"]]

        body = claim["body"]
        total_cents = body["quantity"] * body["unit_price_cents"]
        reply_body: dict[str, Any] = {"request_id": message_id, "sku": body["sku"],
                                      "quantity": body["quantity"], "total_cents": total_cents}
        tp_note: dict[str, Any] = {"committed": False}
        tp_req = body.get("trailproof") if isinstance(body.get("trailproof"), dict) else None
        if tp_req and merchant is not None:
            order_id = tp_req["order_id"]
            currency = tp_req.get("currency", "EUR")
            offer = offer_payload(body, message_id, currency, total_cents)
            committed = offer
            if mode == "lying_commit":
                # The hash on the ledger is of a different offer than the one the buyer gets.
                committed = dict(offer, total=cents_to_amount(total_cents + 100))
            try:
                prev = merchant.head(order_id)  # the buyer's MANDATE, normally
                rec = merchant.record(order_id, "OFFER", committed, prev=prev)
                reply_body["trailproof"] = {"order_id": order_id, "offer": offer, "offer_commit": rec["commit"],
                                            "receipt": rec["receipt"], "merchant_party_id": merchant.party_id}
                tp_note = {"committed": True, "order_id": order_id, "offer_commit": rec["commit"],
                           "receipt": rec["receipt"], "merchant_party_id": merchant.party_id,
                           "ledger_public_key": merchant.ledger_public_key, "mode": mode}
                if settle_mode != "off":
                    tp_note["settle"] = settle_mode
                    threading.Thread(target=settle, daemon=True,
                                     args=(merchant, order_id, rec["receipt"]["event_id"], settle_mode, parcel_dir)).start()
            except ToolError as exc:
                tp_note = {"committed": False, "error": exc.code, "detail": exc.detail}
            except LedgerUnreachable as exc:
                tp_note = {"committed": False, "no_receipt": True, "detail": str(exc)[:200]}
        elif tp_req:
            tp_note = {"committed": False, "no_receipt": True, "detail": "ledger not configured or unreachable"}
        reply = {"message_id": response_id(message_id), "to": claim["from"], "kind": "quote_response",
                 "body": reply_body}
        journal.record(message_id, {"reply": reply, "total_cents": total_cents, "trailproof": tp_note},
                       unreported=True)
        return "processed", {"applied": True, "total_cents": total_cents, "runtime": RUNTIME,
                             "trailproof": tp_note}, [reply]

    return handler


def ack_accepted(journal: Journal):
    def on_ack_accepted(claim: dict[str, Any], note: dict[str, Any]) -> None:
        if note.get("applied"):
            journal.mark_reported(claim["message_id"])
    return on_ack_accepted


def run(client: TownClient, name: str, token: str, state_dir: str, deadline_seconds: float,
        grant_json: str | None, ledger_url: str | None, mode: str, settle_mode: str = "off",
        parcel_dir: str = "") -> int:
    client.join_auto(name, token, grant_json)
    journal = Journal(os.path.join(state_dir, "journal.db"))
    merchant = connect_ledger(ledger_url)
    handler = build_handler(journal, merchant, mode, settle_mode, parcel_dir or os.path.join(state_dir, "parcels"))
    deadline = time.time() + deadline_seconds
    run_loop(client, handler, until=lambda: time.time() > deadline, on_ack_accepted=ack_accepted(journal))
    return 0


def main() -> None:
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    code = run(client, env["NAME"], env["TOKEN"], env["STATE_DIR"], float(env.get("DEADLINE", "60")),
               env.get("TOWN_GRANT"), env.get("TRAILPROOF_URL"), env.get("TRAILPROOF_SELLER_MODE", "honest"),
               env.get("TRAILPROOF_SETTLE", "off") or "off", env.get("TRAILPROOF_PARCEL_DIR", ""))
    sys.exit(code)


if __name__ == "__main__":
    main()
