"""A custom payments plugin for NANDA Town.

Reference: the default implementation lives in
src/nandatown/layers/payments.py. bound.v1 keeps its balances,
transfers, and refunds, and changes the escrow rule for a hold that
names a payee: release can only pay that payee, and once the town has
recorded the payee delivering that order to the payer, the hold can no
longer be refunded and the payee may claim it. A hold that names no
payee behaves exactly like ledger.v1.
"""

from __future__ import annotations

from typing import Any

from nandatown.layers import register
from nandatown.layers.payments import Ledger, PaymentError

# Message kinds by which a payee hands an order's goods to its payer.
DELIVERY_KINDS = frozenset({"delivery", "product_delivery"})


@register("payments", "bound.v1")
class BoundV1(Ledger):
    """bound.v1: escrow is bound to a named payee; release to anyone else is refused."""

    # Agents and the TownAPI check this flag before using payee binding.
    binds_payee = True

    def hold(self, frm: str, cents: int, ref: str,
             payee: str | None = None) -> None:
        super().hold(frm, cents, ref)
        if payee is None:
            return
        self.escrow[ref]["payee"] = payee
        # escrow_held keeps the ledger.v1 shape; the binding is its own event.
        self.engine.emit("town", "escrow_payee_bound", ref,
                         {"from": frm, "payee": payee, "cents": cents})

    def hold_status(self, ref: str) -> dict[str, Any] | None:
        h = self.escrow.get(ref)
        if h is None:
            return None
        return {"from": h["from"], "payee": h.get("payee"),
                "cents": h["cents"], "state": h["state"]}

    def release(self, ref: str, to: str) -> None:
        h = self.escrow.get(ref)
        payee = h.get("payee") if h is not None else None
        if payee is not None and h["state"] == "held" and to != payee:
            self.engine.emit("town", "escrow_release_refused", ref,
                             {"to": to, "payee": payee,
                              "cents": h["cents"],
                              "reason": "release target is not the payee"})
            raise PaymentError(f"escrow ref {ref} is bound to {payee},"
                               f" not {to}")
        super().release(ref, to)

    def refund(self, ref: str) -> None:
        h = self.escrow.get(ref)
        payee = h.get("payee") if h is not None else None
        if (payee is not None and h["state"] == "held"
                and self._delivered(ref, payee, h["from"])):
            self.engine.emit("town", "escrow_refund_refused", ref,
                             {"to": h["from"], "payee": payee,
                              "cents": h["cents"],
                              "reason": "the payee already delivered"})
            raise PaymentError(f"escrow ref {ref} was delivered by {payee};"
                               " it can no longer be refunded")
        super().refund(ref)

    def claim(self, ref: str, by: str) -> None:
        """The payee collects a hold it delivered against. Paid only when
        `by` is the bound payee and the town recorded the payee's delivery
        of this order reaching the payer; refused and recorded otherwise."""
        h = self.escrow.get(ref)
        payee = h.get("payee") if h is not None else None
        if h is None or h["state"] != "held":
            reason = "escrow is not held"
        elif payee is None:
            reason = "escrow names no payee"
        elif by != payee:
            reason = "claimant is not the payee"
        elif not self._delivered(ref, payee, h["from"]):
            reason = "no recorded delivery reached the payer"
        else:
            self.engine.emit("town", "escrow_claimed", ref,
                             {"by": by, "from": h["from"],
                              "cents": h["cents"]})
            super().release(ref, payee)
            return
        self.engine.emit("town", "escrow_claim_refused", ref,
                         {"by": by, "payee": payee, "reason": reason})
        raise PaymentError(f"claim on escrow ref {ref} refused: {reason}")

    def _delivered(self, ref: str, payee: str, payer: str) -> bool:
        """The town recorded a delivery of this order from the payee
        reaching the payer. A dropped or rejected delivery does not count,
        so the payer can still be refunded and the payee cannot claim."""
        sent, delivered, failed = set(), set(), set()
        for event in getattr(self.engine, "events", []):
            detail = event.detail if isinstance(event.detail, dict) else {}
            body = detail.get("body")
            if (event.kind == "message_sent" and event.observer == payee
                    and detail.get("to") == payer
                    and detail.get("kind") in DELIVERY_KINDS
                    and isinstance(body, dict)
                    and body.get("order_id") == ref):
                sent.add(event.subject)
            elif event.kind == "message_delivered":
                delivered.add(event.subject)
            elif event.kind == "delivery_failed":
                failed.add(event.subject)
        return bool(sent & delivered - failed)
