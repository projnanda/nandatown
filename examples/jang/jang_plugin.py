"""JANG in a Lab marketplace: adapter roles and their validators.

JANG (https://github.com/byujan/JANGmarket) is a negotiation service. An owner
signs a mandate stating the limits their agent may not cross on each term.
JANG enforces the mandate on every offer and accept, never shows it to the
counterparty, and signs the agreed package with Ed25519.

The two roles here are Town participants that hold a JANG agent key each.
Town supplies discovery (registry), messaging and its faults, escrow, payment
and reputation; JANG supplies the negotiation and the signed agreement.
Town messages carry only turn signals and the signed agreement; mandates stay
with their owner and JANG.

    jang_seller.on_start   provision a JANG agent and lodge a signed mandate;
                           publish a Town card without any limit
    jang_buyer             lookup -> quote_request -> provision -> jang_open
    jang_seller            open the JANG session and make the first move
    both, on jang_turn     read the session from JANG, decide, offer or accept
    on agreement           the seller presents an invoice carrying the signed
                           agreement; the buyer verifies JANG's signature with
                           the key it trusts and audits the terms against its
                           own mandate before any escrow is held
    then                   purchase_order -> delivery -> escrow release -> rating

Each role records what it did and what JANG answered as attributed events,
which the validators below judge. JANG failures become events rather than
exceptions, because the Lab engine does not catch them.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jang_client import DEFAULT_URL, JangClient, JangRefusal, JangUnreachable  # noqa: E402
from jang_sign import OwnerKey, signer_from_env, verify_agreement  # noqa: E402
from jang_strategy import ClaudeStrategy, RuleStrategy, check_terms  # noqa: E402

from nandatown.sim.agents import Buyer, Seller, role  # noqa: E402
from nandatown.sim.validators import (  # noqa: E402
    _check,
    _missing,
    reputation_consistent,
    validator,
)

# Keys an inter-agent message body may carry. Anything else, in particular a
# mandate's constraints, preferences or reservation utility, is a leak.
MESSAGE_KEYS = {
    "quote_request": {"sku"},
    "quote_response": {"sku", "unit_cents", "jang_agent_id", "item_id"},
    "jang_open": {"buyer_agent_id", "item_id"},
    "jang_turn": {"session_id", "offer_id"},
    "jang_agreed": {"session_id", "agreement_id"},
    "jang_ended": {"session_id", "reason"},
    "invoice": {"agreement", "unit_cents", "quantity", "order_ref"},
    "invoice_rejected": {"agreement_id", "reason"},
    "purchase_order": {"order_id", "agreement_id", "sku", "quantity", "unit_cents"},
    "order_rejected": {"order_id", "reason"},
    "delivery": {"order_id", "sku", "quantity"},
}
MANDATE_KEYS = {"constraints", "preferences", "reservationUtility", "yourMandate",
                "reservation_utility", "violations", "limit"}


def _fingerprint(pem: str) -> str:
    return "sha256:" + hashlib.sha256(pem.encode()).hexdigest()[:16]


def _usd_to_cents(usd: float) -> int:
    return int(round(usd * 100))


class JangParty:
    """What both adapter roles share: one JANG agent, one mandate, one strategy."""

    def _jang_setup(self) -> None:
        self.jang_url = os.environ.get("JANG_URL") or self.config.get("jang_url") or DEFAULT_URL
        self.jang: JangClient | None = None
        self.jang_key = ""
        self.mandate: dict[str, Any] | None = None
        self.jang_agent_id = ""
        self.dead = False
        self.last_refusal = ""
        self.sessions: dict[str, dict[str, Any]] = {}
        spec = self.config.get("strategy", {}) or {}
        if spec.get("kind") == "claude":
            self.strategy = ClaudeStrategy(model=spec.get("model"),
                                           fallback=RuleStrategy(spec.get("options")))
        else:
            self.strategy = RuleStrategy(spec.get("options"))

    # -- recording helpers
    def _down(self, call: str, exc: Exception) -> None:
        self.dead = True
        self.api.observe("jang_unreachable", self.name, {"call": call, "error": str(exc)[:120]})

    def _refused(self, call: str, subject: str, exc: JangRefusal) -> None:
        self.last_refusal = exc.code
        self.api.observe("jang_refused", subject, {"call": call, **exc.public_detail()})

    def _guard(self, call: str, subject: str, fn):
        """Run one JANG call; turn every failure into an event and return None."""
        if self.dead:
            return None
        try:
            return fn()
        except JangUnreachable as exc:
            self._down(call, exc)
        except JangRefusal as exc:
            self._refused(call, subject, exc)
        except Exception as exc:  # noqa: BLE001 - the engine must not crash
            self.api.observe("adapter_error", subject,
                             {"call": call, "error": type(exc).__name__})
        return None

    # -- provisioning
    def _provision(self, role_name: str, item_id: str) -> bool:
        mcfg = self.config["mandate"]
        suffix = uuid.uuid4().hex[:8]
        agent_id = f"{mcfg['agent_id']}-{suffix}"

        def go():
            health = JangClient(self.jang_url).health()
            self.jang_key = health["jangPublicKey"]
            api_key = JangClient(self.jang_url).register_agent(agent_id, mcfg["owner_id"])
            expires = (datetime.now(timezone.utc)
                       + timedelta(seconds=self.config.get("mandate_ttl_seconds", 3600)))
            body = {
                "mandateId": f"mdt_{agent_id}", "agentId": agent_id,
                "ownerId": mcfg["owner_id"], "role": role_name, "itemId": item_id,
                "constraints": mcfg["constraints"], "preferences": mcfg["preferences"],
                "reservationUtility": mcfg["reservation_utility"],
                "expiresAt": expires.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }
            if mcfg.get("disclose_priorities"):
                body["disclosePriorities"] = True
            signer = signer_from_env()
            mandate = signer.sign_mandate(body, OwnerKey.generate())
            client = JangClient(self.jang_url, api_key)
            lodged = client.lodge_mandate(mandate)
            return client, mandate, signer.name, lodged

        out = self._guard("provision", self.name, go)
        if out is None:
            return False
        self.jang, self.mandate, signer_name, lodged = out
        self.jang_agent_id = agent_id
        self.api.observe("jang_provisioned", agent_id, {
            "role": role_name, "mandate_id": lodged.get("mandateId"),
            "item_id": lodged.get("itemId"), "signer": signer_name,
            "owner_signed": True, "jang_key": _fingerprint(self.jang_key),
            "strategy": self.strategy.name,
        })
        return True

    # -- one turn of the negotiation
    def _take_turn(self, session_id: str, counterparty: str, signal: str | None = None) -> None:
        view = self._guard("get_session", session_id, lambda: self.jang.get_session(session_id))
        if view is None:
            return
        standing = view.get("standingOffer")
        if view["status"] != "open":
            self.api.observe("jang_turn_replay", session_id,
                             {"offer_id": signal, "status": view["status"]})
            return
        if signal and (not standing or standing["offerId"] != signal
                       or standing["fromAgentId"] == self.jang_agent_id):
            # A duplicated or stale turn signal: JANG's state says it is not ours.
            self.api.observe("jang_turn_replay", session_id, {"offer_id": signal})
            return
        if self._maybe_misbehave(session_id, view):
            view = self._guard("get_session", session_id,
                               lambda: self.jang.get_session(session_id))
            if view is None:
                return
        move = self._decide(session_id, view)
        if move is None:
            return
        action = move["action"]
        if action == "wait":
            self.api.observe("jang_turn_replay", session_id, {"offer_id": signal})
        elif action == "offer":
            after = self._guard("offer", session_id, lambda: self.jang.offer(
                session_id, move["terms"], move.get("note", ""), move.get("alternatives")))
            if after is None:
                self._end(session_id, counterparty, f"offer_refused:{self.last_refusal}")
                return
            offer = after["standingOffer"]
            self.api.observe("jang_offer_posted", session_id, {
                "offer_id": offer["offerId"], "round": offer["round"],
                "terms": offer["terms"],
                "alternatives": len(offer.get("alternatives") or []),
                "strategy": move.get("_strategy", self.strategy.name),
            })
            self.api.send(counterparty, "jang_turn",
                          {"session_id": session_id, "offer_id": offer["offerId"]})
        elif action == "accept":
            out = self._guard("accept", session_id,
                              lambda: self.jang.accept(session_id, move.get("offerId")))
            if out is None:
                self._end(session_id, counterparty, f"accept_refused:{self.last_refusal}")
                return
            agreement = out["agreement"]
            self.api.observe("jang_agreement_signed", agreement["agreementId"], {
                "session_id": session_id, "terms": agreement["terms"],
                "rounds": agreement["rounds"], "accepted_by": self.name,
                "strategy": move.get("_strategy", self.strategy.name),
            })
            self._agreed(session_id, counterparty, agreement)
        else:
            self._end(session_id, counterparty, "stopped")

    def _decide(self, session_id: str, view: dict[str, Any]) -> dict[str, Any] | None:
        try:
            move = self.strategy.decide(view)
            move["_strategy"] = self.strategy.name
            return move
        except Exception as exc:  # noqa: BLE001
            fallback = getattr(self.strategy, "fallback", None)
            self.api.observe("strategy_error", session_id,
                             {"strategy": self.strategy.name, "error": type(exc).__name__,
                              "fallback": fallback.name if fallback else None})
            if fallback is None:
                return None
            move = fallback.decide(view)
            move["_strategy"] = fallback.name + "-fallback"
            return move

    def _end(self, session_id: str, counterparty: str, reason: str) -> None:
        self.api.observe("jang_session_ended", session_id, {"reason": reason})
        self.api.send(counterparty, "jang_ended", {"session_id": session_id, "reason": reason})

    def handle_jang_ended(self, msg):
        self.api.observe("jang_session_ended", msg["body"]["session_id"],
                         {"reason": msg["body"].get("reason", ""), "reported_by": msg["sender"]})

    def _maybe_misbehave(self, session_id: str, view: dict[str, Any]) -> bool:
        return False

    def _agreed(self, session_id, counterparty, agreement) -> None:
        raise NotImplementedError


@role("jang_seller")
class JangSeller(JangParty, Seller):
    def on_start(self):
        self._jang_setup()
        self.stock = self.config.get("stock", 10)
        self.item = self.config["item"]
        self.agreements: dict[str, dict[str, Any]] = {}
        self.invoiced: set[str] = set()
        if not self._provision("seller", self.item["itemId"]):
            return
        self.api.register([f"sell.{self.config['sku']}"], {
            "sku": self.config["sku"], "ask_cents": self.config["ask_cents"],
            "jang_agent_id": self.jang_agent_id, "item_id": self.item["itemId"]})

    def handle_quote_request(self, msg):
        self.api.reply(msg, "quote_response", {
            "sku": self.config["sku"], "unit_cents": self.config["ask_cents"],
            "jang_agent_id": self.jang_agent_id, "item_id": self.item["itemId"]})

    def handle_jang_open(self, msg):
        buyer_agent = msg["body"]["buyer_agent_id"]
        if buyer_agent in self.sessions:
            self.api.observe("jang_turn_replay", self.sessions[buyer_agent]["session_id"],
                             {"offer_id": None, "kind": "jang_open"})
            return
        view = self._guard("open_session", buyer_agent, lambda: self.jang.open_session(
            buyer_agent, self.item, self.config.get("max_rounds", 12)))
        if view is None:
            return
        sid = view["sessionId"]
        self.sessions[buyer_agent] = {"session_id": sid, "buyer": msg["sender"]}
        self.api.observe("jang_session_opened", sid, {
            "buyer_agent_id": buyer_agent, "seller_agent_id": self.jang_agent_id,
            "max_rounds": view["maxRounds"], "dimensions": len(view["itemSpec"]["dimensions"])})
        self._take_turn(sid, msg["sender"])

    def handle_jang_turn(self, msg):
        self._take_turn(msg["body"]["session_id"], msg["sender"], msg["body"]["offer_id"])

    def handle_jang_agreed(self, msg):
        aid = msg["body"]["agreement_id"]
        agreement = self._guard("get_agreement", aid, lambda: self.jang.get_agreement(aid))
        if agreement is None:
            return
        self._agreed(msg["body"]["session_id"], msg["sender"], agreement)

    def _agreed(self, session_id, counterparty, agreement):
        aid = agreement["agreementId"]
        if aid in self.invoiced:
            self.api.observe("jang_turn_replay", session_id, {"agreement_id": aid})
            return
        ok, within = self._audit(agreement)
        self.api.observe("agreement_verified" if ok and within else "agreement_rejected", aid, {
            "signature_ok": ok, "within_mandate": within, "verified_by": "seller"})
        if not (ok and within):
            return
        self.agreements[aid] = agreement
        self.invoiced.add(aid)
        presented = agreement
        delta = self.config.get("tamper_agreement_delta")
        if delta:
            presented = {**agreement, "terms": {**agreement["terms"],
                                                "price": agreement["terms"]["price"] + delta}}
            self.api.observe("adapter_fault_injected", aid, {
                "fault": "tampered_agreement", "dimension": "price", "delta": delta})
        qty = self.config.get("quantity", 1)
        self.api.send(counterparty, "invoice", {
            "agreement": presented, "quantity": qty, "order_ref": aid,
            "unit_cents": _usd_to_cents(presented["terms"]["price"])})

    def _audit(self, agreement):
        sig = verify_agreement(agreement, self.jang_key)
        within = check_terms(agreement["terms"], self.mandate,
                             now=datetime.fromisoformat(agreement["agreedAt"].replace("Z", "+00:00")))["ok"]
        return sig, within

    def handle_purchase_order(self, msg):
        body = msg["body"]
        agreement = self.agreements.get(body.get("agreement_id"))
        expected = _usd_to_cents(agreement["terms"]["price"]) if agreement else None
        if agreement is None or body["unit_cents"] != expected:
            self.api.reply(msg, "order_rejected", {"order_id": body["order_id"],
                                                   "reason": "order does not match a signed agreement"})
            return
        super().handle_purchase_order(msg)


@role("jang_buyer")
class JangBuyer(JangParty, Buyer):
    def on_start(self):
        self._jang_setup()
        self.paid: set[str] = set()
        self.misbehaved = False
        super().on_start()

    def close_quotes(self):
        if not self.quotes:
            self.api.observe("buyer_gave_up", self.name, {"reason": "no quotes arrived"})
            return
        seller, ask = min(self.quotes.items(), key=lambda kv: (kv[1], kv[0]))
        item_id = self.quote_items[seller]
        self.active = {"seller": seller, "ask": ask}
        if not self._provision("buyer", item_id):
            return
        self.api.send(seller, "jang_open", {"buyer_agent_id": self.jang_agent_id,
                                            "item_id": item_id})

    def handle_quote_response(self, msg):
        if not hasattr(self, "quote_items"):
            self.quote_items = {}
        self.quotes[msg["sender"]] = msg["body"]["unit_cents"]
        self.quote_items[msg["sender"]] = msg["body"].get("item_id")

    def handle_jang_turn(self, msg):
        self._take_turn(msg["body"]["session_id"], msg["sender"], msg["body"]["offer_id"])

    def _maybe_misbehave(self, session_id, view):
        if self.config.get("misbehave") != "offer_outside_mandate" or self.misbehaved:
            return False
        self.misbehaved = True
        bad = dict((view.get("standingOffer") or {}).get("terms") or {})
        for dim in view["itemSpec"]["dimensions"]:
            bad.setdefault(dim["key"], dim["min"])
        cap = self.mandate["constraints"]["price"]["max"]
        step = next(d["step"] for d in view["itemSpec"]["dimensions"] if d["key"] == "price")
        bad["price"] = cap + step  # one step over the owner's ceiling
        self.api.observe("adapter_fault_injected", session_id,
                         {"fault": "offer_outside_mandate", "dimension": "price"})
        after = self._guard("offer", session_id, lambda: self.jang.offer(
            session_id, bad, "fixture: deliberately outside the buyer's mandate"))
        if after is not None:
            self.api.observe("jang_offer_posted", session_id, {
                "offer_id": after["standingOffer"]["offerId"],
                "round": after["standingOffer"]["round"], "terms": after["standingOffer"]["terms"],
                "alternatives": 0, "strategy": "fixture-misbehave"})
            return False
        return True  # refused: decide again from JANG's current state

    def _agreed(self, session_id, counterparty, agreement):
        self.api.send(counterparty, "jang_agreed",
                      {"session_id": session_id, "agreement_id": agreement["agreementId"]})

    def handle_invoice(self, msg):
        body = msg["body"]
        agreement = body.get("agreement") or {}
        aid = agreement.get("agreementId", "?")
        if aid in self.paid:
            self.api.observe("duplicate_recognized", aid, {"kind": "invoice"})
            return
        sig = verify_agreement(agreement, self.jang_key)
        if self.config.get("trust_invoice"):
            # Negative control: a buyer that skips JANG's signature and trusts
            # whatever agreement the seller presents.
            self.api.observe("adapter_fault_injected", aid, {"fault": "signature_check_skipped"})
            sig = True
        within = False
        try:
            agreed_at = datetime.fromisoformat(agreement["agreedAt"].replace("Z", "+00:00"))
            within = check_terms(agreement["terms"], self.mandate, now=agreed_at)["ok"]
        except (KeyError, TypeError, ValueError):
            pass
        price_cents = _usd_to_cents(agreement.get("terms", {}).get("price", -1))
        consistent = body.get("unit_cents") == price_cents
        if not (sig and within and consistent):
            reason = ("signature_invalid" if not sig else
                      "outside_own_mandate" if not within else "invoice_mismatch")
            self.api.observe("agreement_rejected", aid, {
                "signature_ok": sig, "within_mandate": within, "verified_by": "buyer",
                "reason": reason})
            self.api.reply(msg, "invoice_rejected", {"agreement_id": aid, "reason": reason})
            return
        self.api.observe("agreement_verified", aid, {
            "signature_ok": "skipped" if self.config.get("trust_invoice") else True,
            "within_mandate": True, "verified_by": "buyer"})
        self.paid.add(aid)
        qty = body.get("quantity", 1)
        total = body["unit_cents"] * qty
        if self.api.balance() < total:
            self.api.observe("buyer_gave_up", self.name, {"reason": "insufficient balance"})
            return
        order_id = f"order-{self.name}-{self.round}"
        self.api.escrow_hold(total, ref=order_id)
        self.api.send(msg["sender"], "purchase_order", {
            "order_id": order_id, "agreement_id": aid, "sku": self.config["sku"],
            "quantity": qty, "unit_cents": body["unit_cents"]})

    def handle_order_rejected(self, msg):
        order_id = msg["body"]["order_id"]
        self.api.escrow_refund(order_id)
        self.api.observe("order_refunded", order_id, {"reason": msg["body"].get("reason", "")})


# -- validators ----------------------------------------------------------


def _reachability(trace):
    down = trace.find("jang_unreachable")
    if down:
        return _missing("jang_reachable",
                        f"JANG was not reachable ({down[0].detail.get('call')}:"
                        f" {down[0].detail.get('error')}); nothing about JANG was judged")
    errors = trace.find("adapter_error")
    if errors:
        return _missing("jang_reachable", f"adapter error during {errors[0].detail.get('call')}")
    prov = trace.find("jang_provisioned")
    if not prov:
        return _missing("jang_reachable", "no JANG agent was provisioned")
    return None


def _inconclusive(names, why):
    return [_missing(n, why) for n in names]


def _stage_reachable(trace):
    prov = trace.find("jang_provisioned")
    return _check("jang_reachable", len(prov) == 2, [e.event_id for e in prov],
                  "expected a JANG agent for both buyer and seller",
                  "JANG health, registration and mandate lodging answered for both roles")


def _stage_discovered(trace):
    cards = trace.find("card_registered")
    looks = [e for e in trace.find("message_sent", kind="quote_response")]
    leaked = [e for e in cards if MANDATE_KEYS & set((e.detail.get("facts") or {}))]
    return _check("discovered", bool(cards) and bool(looks) and not leaked,
                  [e.event_id for e in cards + looks],
                  "the buyer did not discover the seller through the registry, or the card exposed a limit",
                  "the buyer found the seller through the Town registry and received a quote")


def _stage_mandates(trace):
    prov = trace.find("jang_provisioned")
    roles = {e.detail.get("role") for e in prov}
    signers = sorted({e.detail.get("signer") for e in prov})
    return _check("mandates_lodged", roles == {"buyer", "seller"}, [e.event_id for e in prov],
                  "JANG did not accept an owner-signed mandate for both roles",
                  f"JANG accepted both owner-signed mandates (signer: {', '.join(signers)})")


def _stage_negotiated(trace, item_dims):
    offers = trace.find("jang_offer_posted")
    signed = trace.find("jang_agreement_signed")
    alternating = all(a.observer != b.observer for a, b in zip(offers, offers[1:]))
    covers = bool(signed) and len(signed[0].detail.get("terms", {})) == item_dims
    return _check("negotiated", len(offers) >= 2 and alternating and len(signed) == 1 and covers,
                  [e.event_id for e in offers + signed],
                  "offers did not alternate into exactly one agreement covering every term",
                  f"{len(offers)} alternating offers closed in one agreement on all {item_dims} terms")


def _stage_verified(trace):
    ok = trace.find("agreement_verified")
    bad = trace.find("agreement_rejected")
    sides = {e.detail.get("verified_by") for e in ok}
    return _check("agreement_verified", sides == {"buyer", "seller"} and not bad,
                  [e.event_id for e in ok + bad],
                  "both sides must verify JANG's signature and audit the terms before acting",
                  "buyer and seller each verified JANG's signature with the trusted key and"
                  " found the terms inside their own mandate")


def _stage_duplicate_turn(trace):
    dup = trace.find("message_duplicated", kind="jang_turn")
    replays = trace.find("jang_turn_replay")
    offers = trace.find("jang_offer_posted")
    unique = len({e.detail.get("offer_id") for e in offers}) == len(offers)
    if not dup:
        return _missing("duplicate_turn_absorbed", "no jang_turn was duplicated in this run")
    return _check("duplicate_turn_absorbed", bool(replays) and unique,
                  [e.event_id for e in dup + replays],
                  "a duplicated turn signal produced an extra move",
                  "the duplicated turn signal was recognised from JANG's session state; no extra offer")


def _stage_settled(trace, qty):
    signed = trace.find("jang_agreement_signed")
    held = trace.find("escrow_held")
    released = trace.find("escrow_released")
    dup = trace.find("duplicate_recognized", kind="delivery")
    if not signed:
        return _missing("settled_once", "no agreement to settle")
    expected = _usd_to_cents(signed[0].detail["terms"]["price"]) * qty
    amounts = [e.detail.get("cents", e.detail.get("amount_cents")) for e in held]
    ok = len(held) == 1 and len(released) == 1 and amounts == [expected] and bool(dup)
    return _check("settled_once", ok, [e.event_id for e in held + released + dup],
                  f"expected one escrow of {expected} cents released once, with the duplicate"
                  " delivery recognised",
                  f"escrow of {expected} cents (agreed price x {qty}) released exactly once;"
                  " the duplicated delivery was recognised")


def _stage_not_shared(trace):
    sent = trace.find("message_sent")
    problems = []
    for e in sent:
        kind = e.detail.get("kind")
        body = e.detail.get("body") or {}
        allowed = MESSAGE_KEYS.get(kind)
        if allowed is None or not set(body) <= allowed:
            problems.append(e)
            continue
        stack = [body]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if MANDATE_KEYS & set(node):
                    problems.append(e)
                    break
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return _check("mandate_not_shared", not problems and bool(sent),
                  [e.event_id for e in (problems or sent)],
                  "an inter-agent message carried a key outside the protocol or a mandate field",
                  f"{len(sent)} inter-agent messages carried only protocol keys; no mandate field")


def _dims(spec) -> int:
    for a in spec.agents:
        item = (a.config or {}).get("item")
        if isinstance(item, dict):
            return len(item.get("dimensions", []))
    return 0


def _qty(spec) -> int:
    for a in spec.agents:
        if a.role == "jang_seller":
            return int((a.config or {}).get("quantity", 1))
    return 1


@validator("jang_market")
def jang_market(spec, trace):
    names = ["jang_reachable", "discovered", "mandates_lodged", "negotiated",
             "agreement_verified", "duplicate_turn_absorbed", "settled_once",
             "reputation_consistent", "mandate_not_shared"]
    down = _reachability(trace)
    if down:
        return [down] + _inconclusive(names[1:], down.note)
    return [_stage_reachable(trace), _stage_discovered(trace), _stage_mandates(trace),
            _stage_negotiated(trace, _dims(spec)), _stage_verified(trace),
            _stage_duplicate_turn(trace), _stage_settled(trace, _qty(spec)),
            reputation_consistent(trace), _stage_not_shared(trace)]


@validator("jang_mandate_violation")
def jang_mandate_violation(spec, trace):
    names = ["jang_reachable", "violation_refused", "recovered_in_bounds", "mandate_not_shared"]
    down = _reachability(trace)
    if down:
        return [down] + _inconclusive(names[1:], down.note)
    injected = trace.find("adapter_fault_injected", fault="offer_outside_mandate")
    refused = trace.find("jang_refused", code="MANDATE_VIOLATION")
    posted_bad = trace.find("jang_offer_posted", strategy="fixture-misbehave")
    refusal_ok = (bool(injected) and bool(refused) and not posted_bad
                  and all(e.detail.get("status") == 422 for e in refused)
                  and all(trace.index(i) < trace.index(r) for i, r in zip(injected, refused)))
    signed = trace.find("jang_agreement_signed")
    verified = trace.find("agreement_verified")
    held = trace.find("escrow_held")
    recovered = len(signed) == 1 and {e.detail.get("verified_by") for e in verified} == {"buyer", "seller"} \
        and len(held) == 1
    return [
        _stage_reachable(trace),
        _check("violation_refused", refusal_ok, [e.event_id for e in injected + refused + posted_bad],
               "JANG did not refuse the package outside the buyer's own mandate",
               "JANG refused the out-of-mandate offer with 422 MANDATE_VIOLATION; the refusal"
               " recorded only the dimension, never the limit"),
        _check("recovered_in_bounds", recovered, [e.event_id for e in signed + verified + held],
               "after the refusal the agents did not close and settle an in-bounds agreement",
               "after the refusal the buyer re-decided from JANG's state and an in-bounds"
               " agreement was verified and settled"),
        _stage_not_shared(trace),
    ]


@validator("jang_tampered_agreement")
def jang_tampered_agreement(spec, trace):
    names = ["jang_reachable", "negotiated", "tamper_detected", "no_payment_on_tamper",
             "mandate_not_shared"]
    down = _reachability(trace)
    if down:
        return [down] + _inconclusive(names[1:], down.note)
    injected = trace.find("adapter_fault_injected", fault="tampered_agreement")
    rejected = trace.find("agreement_rejected", verified_by="buyer")
    sig_caught = [e for e in rejected if e.detail.get("reason") == "signature_invalid"
                  and e.detail.get("within_mandate") is True]
    money = (trace.find("escrow_held") + trace.find("payment_settled")
             + trace.find("message_sent", kind="delivery"))
    return [
        _stage_reachable(trace),
        _stage_negotiated(trace, _dims(spec)),
        _check("tamper_detected", bool(injected) and bool(sig_caught),
               [e.event_id for e in injected + rejected],
               "the buyer accepted an agreement whose terms were altered after JANG signed it",
               "the altered agreement still fit the buyer's mandate, so only JANG's signature"
               " exposed it; the buyer rejected it"),
        _check("no_payment_on_tamper", bool(injected) and not money,
               [e.event_id for e in injected + money],
               "money moved or goods shipped against a tampered agreement",
               "no escrow, payment or delivery followed the tampered agreement"),
        _stage_not_shared(trace),
    ]


@validator("jang_no_zopa")
def jang_no_zopa(spec, trace):
    names = ["jang_reachable", "negotiation_attempted", "no_deal_no_money", "mandate_not_shared"]
    down = _reachability(trace)
    if down:
        return [down] + _inconclusive(names[1:], down.note)
    offers = trace.find("jang_offer_posted")
    ended = trace.find("jang_session_ended")
    signed = trace.find("jang_agreement_signed")
    money = trace.find("escrow_held") + trace.find("payment_settled")
    return [
        _stage_reachable(trace),
        _check("negotiation_attempted", len(offers) >= 2, [e.event_id for e in offers],
               "the agents did not negotiate", f"{len(offers)} offers were exchanged"),
        _check("no_deal_no_money", bool(ended) and not signed and not money,
               [e.event_id for e in ended + signed + money],
               "a deal was signed or money moved although no package fits both mandates",
               "no package fits both mandates: the session ended without an agreement and no"
               " money moved"),
        _stage_not_shared(trace),
    ]
