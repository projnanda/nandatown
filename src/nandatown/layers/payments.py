"""Payments layer: a testnet ledger with escrow.

All amounts are integer cents. Money is conserved: the sum of balances
plus held escrow never changes after accounts open. Every movement is an
event.
"""

from __future__ import annotations

import hashlib
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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


@register("payments", "deadline.v1")
class DeadlineEscrow(Ledger):
    """Deadline refund without the hashlock: the time half of hashlock.v1.

    Exists to show what hashlock.v1 is for, as plain.v1 does for auth.
    A hold still held at its deadline is refunded to its payer, and a
    release after that refund is refused. That keeps money from being
    stranded, the gap open PR #291 addresses with leased.v1, but it
    cannot stop goods that arrive late from being used after the refund:
    the payer ends with the goods and the money. hashlock.v1 closes
    that. If #291 lands first, hashlock.v1 should build on leased.v1.
    """

    TIMEOUT = 5.0

    def __init__(self, engine):
        super().__init__(engine)
        self.expired: set[str] = set()

    def hold(self, frm: str, cents: int, ref: str) -> None:
        super().hold(frm, cents, ref)
        expires_at = self.engine.now + self.TIMEOUT
        self.escrow[ref]["expires_at"] = expires_at
        self.engine.emit("town", "escrow_deadline_set", ref,
                         {"expires_at": expires_at,
                          "timeout": self.TIMEOUT})
        self.engine.schedule(self.TIMEOUT, lambda: self._expire(ref))

    def _expire(self, ref: str) -> None:
        h = self.escrow.get(ref)
        if h is None or h["state"] != "held":
            return
        self.expired.add(ref)
        self.engine.emit("town", "escrow_expired", ref,
                         {"expires_at": h["expires_at"]})
        self.refund(ref)

    def release(self, ref: str, to: str) -> None:
        if ref in self.expired:
            self.engine.emit("town", "escrow_release_refused", ref,
                             {"to": to, "reason": "deadline passed"})
            return
        super().release(ref, to)


# -- sealed goods ---------------------------------------------------------


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def seal(key: bytes, nonce: bytes, plaintext: bytes) -> bytes:
    """AES-GCM: the ciphertext carries a tag, so tampering is detected."""
    return AESGCM(key).encrypt(nonce, plaintext, None)


def unseal(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes | None:
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except (InvalidTag, ValueError):
        return None


@register("payments", "hashlock.v1")
class HashlockLedger(DeadlineEscrow):
    """Atomic exchange: the payee is paid only by revealing the goods' key.

    The payer holds sealed goods before paying, and locks the escrow to
    the payee, the key's digest and the listed content digest. The payee
    claims by presenting the key. The ledger pays only if the claim is
    before the deadline, the key matches its digest, the box decrypts
    (AES-GCM rejects tampering) and the plaintext matches the listing;
    the same step reveals the key to the payer. Otherwise the deadline
    refunds the payer, who keeps a box it cannot open. Payment and the
    ability to use the goods happen together or not at all.

    This binds payment to the exact listed bytes; it does not judge
    whether those bytes were worth buying. The ledger sees the plaintext
    while checking it.
    """

    def __init__(self, engine):
        super().__init__(engine)
        self.locks: dict[str, dict[str, Any]] = {}
        self.revealed: dict[str, bytes] = {}

    def hold_locked(self, frm: str, to: str, cents: int, ref: str,
                    key_digest: str, content_digest: str,
                    ciphertext: bytes, nonce: bytes) -> None:
        self.hold(frm, cents, ref)
        self.locks[ref] = {"payee": to, "key_digest": key_digest,
                           "content_digest": content_digest,
                           "ciphertext": ciphertext, "nonce": nonce}
        self.engine.emit("town", "escrow_hashlocked", ref,
                         {"payee": to, "key_digest": key_digest,
                          "content_digest": content_digest,
                          "box_digest": digest(ciphertext)})

    def claim(self, ref: str, claimant: str, key: bytes) -> bool:
        lock, held = self.locks.get(ref), self.escrow.get(ref)
        if lock is None or held is None:
            raise PaymentError(f"escrow ref {ref} is not hashlocked")
        reason = None
        if claimant != lock["payee"]:
            reason = "claimant is not the payee"
        elif not isinstance(key, bytes):
            reason = "key is not bytes"
        elif held["state"] != "held":
            reason = ("deadline passed" if ref in self.expired
                      else f"escrow {held['state']}")
        elif digest(key) != lock["key_digest"]:
            reason = "key does not match its digest"
        else:
            plaintext = unseal(key, lock["nonce"], lock["ciphertext"])
            if plaintext is None:
                reason = "box does not decrypt"
            elif digest(plaintext) != lock["content_digest"]:
                reason = "content does not match the listing"
        if reason:
            self.engine.emit("town", "claim_refused", ref,
                             {"claimant": claimant, "reason": reason})
            return False
        super().release(ref, lock["payee"])
        self.revealed[ref] = key
        self.engine.emit("town", "key_revealed", ref,
                         {"to": held["from"], "key_hex": key.hex()})
        return True

    def release(self, ref: str, to: str) -> None:
        if ref in self.locks:
            self.engine.emit("town", "escrow_release_refused", ref,
                             {"to": to, "reason": "hashlocked: only a key"
                                                  " claim pays"})
            return
        super().release(ref, to)
