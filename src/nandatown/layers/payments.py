"""Payments layer: a testnet ledger with escrow.

All amounts are integer cents. Money is conserved: the sum of balances
plus held escrow never changes after accounts open. Every movement is an
event.
"""

from __future__ import annotations

from typing import Any

from . import register


class PaymentError(Exception):
    pass


def _require_cents(cents: int, minimum: int) -> None:
    if type(cents) is not int or cents < minimum:
        raise PaymentError(f"cents must be an integer >= {minimum}")


@register("payments", "ledger.v1")
class Ledger:
    """Balances, transfers, and escrow hold, release, refund."""

    def __init__(self, engine):
        self.engine = engine
        self.balances: dict[str, int] = {}
        self.escrow: dict[str, dict[str, Any]] = {}

    def open_account(self, name: str, cents: int) -> None:
        _require_cents(cents, 0)
        if name not in self.balances:
            self.balances[name] = cents
            self.engine.emit("town", "account_opened", name,
                             {"balance_cents": cents})

    def balance(self, name: str) -> int:
        return self.balances.get(name, 0)

    def total(self) -> int:
        return (sum(self.balances.values())
                + sum(h["cents"] for h in self.escrow.values()
                      if h["state"] == "held"))

    def transfer(self, frm: str, to: str, cents: int, memo: str) -> None:
        _require_cents(cents, 1)
        if self.balance(frm) < cents:
            self.engine.emit("town", "payment_rejected", frm,
                             {"to": to, "cents": cents, "memo": memo,
                              "reason": "insufficient funds"})
            raise PaymentError(f"{frm} lacks {cents}")
        self.balances[frm] -= cents
        self.balances[to] = self.balance(to) + cents
        self.engine.emit("town", "payment_settled", memo,
                         {"from": frm, "to": to, "cents": cents})

    def hold(self, frm: str, cents: int, ref: str) -> None:
        _require_cents(cents, 1)
        if ref in self.escrow:
            raise PaymentError(f"escrow ref {ref} reused")
        if self.balance(frm) < cents:
            self.engine.emit("town", "payment_rejected", frm,
                             {"cents": cents, "ref": ref,
                              "reason": "insufficient funds"})
            raise PaymentError(f"{frm} lacks {cents}")
        self.balances[frm] -= cents
        self.escrow[ref] = {"from": frm, "cents": cents, "state": "held"}
        self.engine.emit("town", "escrow_held", ref,
                         {"from": frm, "cents": cents})

    def release(self, ref: str, to: str) -> None:
        h = self.escrow.get(ref)
        if h is None or h["state"] != "held":
            raise PaymentError(f"escrow ref {ref} not held")
        h["state"] = "released"
        self.balances[to] = self.balance(to) + h["cents"]
        self.engine.emit("town", "escrow_released", ref,
                         {"to": to, "cents": h["cents"]})
        self.engine.emit("town", "payment_settled", ref,
                         {"from": h["from"], "to": to, "cents": h["cents"],
                          "via": "escrow"})

    def refund(self, ref: str) -> None:
        h = self.escrow.get(ref)
        if h is None or h["state"] != "held":
            raise PaymentError(f"escrow ref {ref} not held")
        h["state"] = "refunded"
        self.balances[h["from"]] += h["cents"]
        self.engine.emit("town", "escrow_refunded", ref,
                         {"to": h["from"], "cents": h["cents"]})


@register("payments", "leased.v1")
class LeasedLedger(Ledger):
    """The same ledger, but every escrow hold carries a lease.

    A hold nobody releases before its lease ends refunds itself to the
    payer, on the logical clock, and a release that arrives after that is
    refused and recorded rather than paid out. The Track fences a claim
    whose lease ran out; this fences the money. A scenario that needs a
    different lease registers a subclass with another LEASE.
    """

    LEASE = 5.0

    def hold(self, frm: str, cents: int, ref: str) -> None:
        super().hold(frm, cents, ref)
        expires_at = self.engine.now + self.LEASE
        self.escrow[ref]["expires_at"] = expires_at
        self.engine.emit("town", "escrow_leased", ref,
                         {"from": frm, "cents": cents,
                          "expires_at": expires_at})
        self.engine.schedule(self.LEASE, lambda: self._expire(ref))

    def release(self, ref: str, to: str) -> None:
        h = self.escrow.get(ref)
        if h is not None and h["state"] == "expired":
            self.engine.emit("town", "escrow_release_rejected", ref,
                             {"to": to, "cents": h["cents"],
                              "reason": "lease expired",
                              "expires_at": h["expires_at"]})
            return
        super().release(ref, to)

    def _expire(self, ref: str) -> None:
        h = self.escrow[ref]
        if h["state"] != "held":
            return
        self.engine.emit("town", "escrow_expired", ref,
                         {"from": h["from"], "cents": h["cents"],
                          "expires_at": h["expires_at"]})
        super().refund(ref)
        h["state"] = "expired"
