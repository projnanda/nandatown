"""Negotiation layer: posted-price clocks, falling (Dutch) and rising.

The seller posts a price that moves by a fixed step on every tick. A
buyer accepts by naming the tick and price it saw. What a late accept
buys depends on which way the price moves, so each direction follows
the market convention that keeps both sides safe:

dutch.v1   Falling clock, Dutch-auction rule: the press stops the clock,
           so a late accept still buys at the price it named. Safe
           because a stale price is never below the current one.
rising.v1  Rising clock, immediate-or-cancel (IOC) limit rule: the named
           price is the most the buyer will pay; the accept fills at the
           current price if that is within the limit, and is refused
           otherwise. A stale quote can never be sniped as a bargain.

dutch.naive.v1 and rising.stale.v1 are the deliberately weak twins (like
auth plain.v1): each applies the other direction's rule, and fails.
"""

from __future__ import annotations

from typing import Any

from . import register
from .negotiation import NegotiationError


@register("negotiation", "dutch.v1")
class DutchClock:
    """Descending clock; an accept binds to the posted tick it names."""

    direction = -1                  # the price falls each tick
    bound_key = "floor_cents"       # the lowest price the clock will post

    def __init__(self, engine):
        self.engine = engine
        self.sessions: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def open(self, seller: str, subject: str, start_cents: int,
             step_cents: int, bound_cents: int) -> str:
        """Open a clock. step >= 1 fixes the direction: a falling clock
        only falls, so a delayed accept always names a price at or above
        the current one; a rising clock only rises."""
        if any(type(v) is not int
               for v in (start_cents, step_cents, bound_cents)):
            raise NegotiationError("clock prices must be integer cents")
        if self.direction < 0:
            ok = step_cents >= 1 and 0 <= bound_cents <= start_cents
            rule = "step >= 1 and start >= floor >= 0"
        else:
            ok = step_cents >= 1 and 0 <= start_cents <= bound_cents
            rule = "step >= 1 and 0 <= start <= cap"
        if not ok:
            raise NegotiationError(f"clock needs {rule}")
        self._seq += 1
        nid = f"d-{self._seq}"
        self.sessions[nid] = {"seller": seller, "subject": subject,
                              "next_cents": start_cents,
                              "step": step_cents, "bound": bound_cents,
                              "tick": 0, "posted": {}, "ended": False,
                              "sale": None, "sale_cents": None}
        self.engine.emit(seller, "negotiation_started", nid,
                         {"seller": seller, "subject": subject,
                          "start_cents": start_cents,
                          "step_cents": step_cents,
                          self.bound_key: bound_cents})
        return nid

    def post(self, nid: str, by: str) -> tuple[int, int] | None:
        """Post the next price; None once sold or past the bound."""
        s = self.sessions[nid]
        if by != s["seller"]:
            raise NegotiationError(f"only {s['seller']} posts in {nid}")
        if s["sale"] is not None or s["ended"]:
            return None
        past = (s["next_cents"] < s["bound"] if self.direction < 0
                else s["next_cents"] > s["bound"])
        if past:
            s["ended"] = True
            self.engine.emit(by, "clock_ended", nid,
                             {"last_tick": s["tick"]})
            return None
        s["tick"] += 1
        cents = s["next_cents"]
        s["posted"][s["tick"]] = cents
        s["next_cents"] += self.direction * s["step"]
        self.engine.emit(by, "price_posted", nid,
                         {"tick": s["tick"], "cents": cents})
        return s["tick"], cents

    def accept(self, nid: str, by: str, tick: Any = None,
               cents: Any = None) -> int | None:
        """Sell to `by` under this clock's acceptance rule; None if refused.

        Repeating the accept that already won is recognized and answered
        with the same price, so a duplicated message cannot buy twice.
        """
        if tick is None:
            raise NegotiationError(
                f"{self.plugin_id} accepts must name a posted tick;"
                " it runs with the dutch_seller and dutch_buyer roles")
        s = self.sessions[nid]
        if s["sale"] is not None:
            if s["sale"] == (by, tick, cents):
                self.engine.emit(s["seller"], "duplicate_recognized", nid,
                                 {"buyer": by, "tick": tick})
                return s["sale_cents"]
            self._reject(nid, by, tick, cents, "already sold")
            return None
        price = self._sale_price(s, tick, cents)
        if price is None:
            self._reject(nid, by, tick, cents,
                         self._refusal_reason(s, tick, cents))
            return None
        s["sale"] = (by, tick, cents)
        s["sale_cents"] = price
        self.engine.emit(s["seller"], "offer_accepted", nid,
                         {"buyer": by, "tick": tick, "cents": price})
        return price

    def start(self, buyer: str, seller: str, subject: str) -> str:
        raise NegotiationError(
            f"{self.plugin_id} is a seller's clock opened with open();"
            " it runs with the dutch_seller and dutch_buyer roles")

    def offer(self, nid: str, by: str, cents: int) -> None:
        raise NegotiationError(
            f"{self.plugin_id} has no counter-offers; the clock sets the"
            " price")

    def agreed_price(self, nid: str) -> int | None:
        return self.sessions[nid]["sale_cents"]

    def _quoted(self, s: dict[str, Any], tick: Any, cents: Any) -> bool:
        """The accept names exactly a price this clock posted."""
        return (type(tick) is int and type(cents) is int
                and s["posted"].get(tick) == cents)

    def _sale_price(self, s: dict[str, Any], tick: Any,
                    cents: Any) -> int | None:
        """Dutch rule: the sale is at the price the buyer named."""
        return cents if self._quoted(s, tick, cents) else None

    def _refusal_reason(self, s: dict[str, Any], tick: Any,
                        cents: Any) -> str:
        return "not a posted price"

    def _reject(self, nid: str, by: str, tick: Any, cents: Any,
                reason: str) -> None:
        self.engine.emit(self.sessions[nid]["seller"], "accept_rejected",
                         nid, {"buyer": by, "tick": tick, "cents": cents,
                               "reason": reason})


@register("negotiation", "dutch.naive.v1")
class NaiveDutchClock(DutchClock):
    """Negative control: sells at the clock's current price on arrival."""

    def _sale_price(self, s: dict[str, Any], tick: Any,
                    cents: Any) -> int | None:
        return s["posted"][s["tick"]] if self._quoted(s, tick, cents) \
            else None


@register("negotiation", "rising.v1")
class RisingClock(DutchClock):
    """Ascending clock; an accept is an immediate-or-cancel limit order."""

    direction = +1                  # the price rises each tick
    bound_key = "cap_cents"         # the highest price the clock will post

    def _sale_price(self, s: dict[str, Any], tick: Any,
                    cents: Any) -> int | None:
        """IOC rule: fill at the current price if within the named limit."""
        if not self._quoted(s, tick, cents):
            return None
        current = s["posted"][s["tick"]]
        return current if current <= cents else None

    def _refusal_reason(self, s: dict[str, Any], tick: Any,
                        cents: Any) -> str:
        if self._quoted(s, tick, cents):
            return "price moved above the limit"
        return "not a posted price"


@register("negotiation", "rising.stale.v1")
class StaleRisingClock(RisingClock):
    """Negative control: honours the stale named price, a sniped bargain."""

    def _sale_price(self, s: dict[str, Any], tick: Any,
                    cents: Any) -> int | None:
        return cents if self._quoted(s, tick, cents) else None
