"""Negotiation layer: alternating offers with an auditable session."""

from __future__ import annotations

from typing import Any

from . import register


class NegotiationError(Exception):
    pass


@register("negotiation", "haggle.v1")
class Haggle:
    """Offer, counter, accept, with alternation enforced and recorded."""

    def __init__(self, engine):
        self.engine = engine
        self.sessions: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def start(self, buyer: str, seller: str, subject: str) -> str:
        self._seq += 1
        nid = f"n-{self._seq}"
        self.sessions[nid] = {"buyer": buyer, "seller": seller,
                              "subject": subject, "turn": buyer,
                              "last_cents": None, "state": "open",
                              "offers": []}
        self.engine.emit(buyer, "negotiation_started", nid,
                         {"seller": seller, "subject": subject})
        return nid

    def _step(self, nid: str, by: str) -> dict[str, Any]:
        s = self.sessions[nid]
        if s["state"] != "open":
            raise NegotiationError(f"session {nid} is {s['state']}")
        if by != s["turn"]:
            raise NegotiationError(f"not {by}'s turn in {nid}")
        return s

    def offer(self, nid: str, by: str, cents: int) -> None:
        s = self._step(nid, by)
        s["offers"].append((by, cents))
        s["last_cents"] = cents
        s["turn"] = s["seller"] if by == s["buyer"] else s["buyer"]
        kind = "offer_made" if by == s["buyer"] else "counter_made"
        self.engine.emit(by, kind, nid, {"cents": cents})

    def accept(self, nid: str, by: str) -> int:
        s = self._step(nid, by)
        s["state"] = "agreed"
        cents = s["last_cents"]
        self.engine.emit(by, "offer_accepted", nid, {"cents": cents})
        return cents

    def abandon(self, nid: str, by: str, reason: str = "") -> None:
        s = self.sessions[nid]
        s["state"] = "abandoned"
        detail = {"reason": reason} if reason else {}
        self.engine.emit(by, "negotiation_abandoned", nid, detail)

    def agreed_price(self, nid: str) -> int | None:
        s = self.sessions[nid]
        return s["last_cents"] if s["state"] == "agreed" else None


# How long an offer stays acceptable after it is made, in logical seconds.
# A module constant: the Lab has no per-layer config channel (only transport
# and privacy get .configure()), so per-scenario tuning would need a
# layer_config field on ScenarioSpec threaded to plugin.configure(cfg).
OFFER_WINDOW_SECONDS = 1.0


@register("negotiation", "haggle.expiring.v1")
class ExpiringHaggle(Haggle):
    """Haggle with an offer validity window: a stale acceptance is refused.

    Every offer is acceptable only for OFFER_WINDOW_SECONDS after it is made.
    Accepting later emits offer_expired and returns None instead of a price,
    so a caller that honours the result does not trade at a lapsed offer.
    """

    def offer(self, nid: str, by: str, cents: int) -> None:
        super().offer(nid, by, cents)
        self.sessions[nid]["expires_at"] = self.engine.now + OFFER_WINDOW_SECONDS

    def accept(self, nid: str, by: str) -> int | None:
        s = self._step(nid, by)
        deadline = s.get("expires_at")
        if deadline is not None and self.engine.now > deadline:
            s["state"] = "expired"
            self.engine.emit(by, "offer_expired", nid,
                             {"cents": s["last_cents"],
                              "made_at": deadline - OFFER_WINDOW_SECONDS,
                              "now": self.engine.now,
                              "window": OFFER_WINDOW_SECONDS})
            return None
        s["state"] = "agreed"
        self.engine.emit(by, "offer_accepted", nid, {"cents": s["last_cents"]})
        return s["last_cents"]
