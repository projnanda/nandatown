"""Agents Gym as Town's trust layer: delegate only on verified behavior.

Agents Gym (https://agents-gym.vercel.app) runs a candidate agent through a
replica of a real workflow twice, once as is (world A) and once with exactly
one controlled change (world B, here an SME loan file whose bank statements
are missing). It labels what the agent did (honest, fabricator, shortcut,
lazy, unstable) and issues an Ed25519-signed scorecard that anyone can verify
before trusting the agent with real work.

This plugin puts that evidence in Town's trust slot. What a run exercises:

1. five candidate reviewers publish ``review.sme_loan`` cards to the registry
2. a lender looks the capability up, asks every candidate for a quote and for
   its Agents Gym scorecard, carried as ordinary Town messages
3. ``agentsgym.attested.v1`` verifies each scorecard with Town's own reader
   (signature, pinned gym key, time window, card holder) and scores it from
   the behavior it records; it compares its verdict with the gym's own
4. for each loan file the lender delegates the review to the most trusted
   candidate, cheapest first among equals, negotiates, escrows and pays
5. the file matures: an approved file that lacked bank statements defaults

Swap the trust layer for Town's default ``reputation.v1`` and the same market
hires the cheapest candidate, which is the fabricator; the validator then
fails on purpose. The cards come from a fixture captured from the production
gym, or from the live gym when ``NANDATOWN_AGENTSGYM_URL`` is set.

Loaded by ``plugin_files:`` in the scenarios beside this file. It registers
one trust plugin, two roles and two validators without changing Town.

Example::

    nandatown run integrations/agents_gym/scenarios/attested_delegation.yaml
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

from nandatown.layers import register
from nandatown.records import StageResult
from nandatown.sim.agents import Buyer, Seller, role
from nandatown.sim.validators import Trace, validator

_HERE = Path(__file__).resolve().parent


def _sibling(name: str):
    """Import a module beside this file; plugin files are loaded by path."""
    full = f"nandatown_agentsgym_{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _HERE / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load {name}.py beside {__file__}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


sc = _sibling("scorecard")
gs = _sibling("gym_source")

CAPABILITY = "review.sme_loan"
FORMULA = "agentsgym.attested.v1"


def card_digest(card: Any) -> str:
    try:
        return "sha256:" + hashlib.sha256(sc.canonical_bytes(card)).hexdigest()
    except (sc.CanonicalError, TypeError):
        return "unreadable"


def _lender_config(spec) -> dict[str, Any]:
    for agent in spec.agents:
        if agent.role == "gym_lender":
            return dict(agent.config)
    return {}


def source_for(engine):
    """One gym source per run, shared by the trust layer and the roles.

    Built on first use, before any agent acts, and recorded as the run's
    first gym event so a reader sees where the cards came from.
    """
    source = getattr(engine, "_agentsgym_source", None)
    if source is not None:
        return source
    config = _lender_config(engine.spec)
    source = gs.GymSource.from_config(config)
    source.pinned_key = config.get("pinned_key") or gs.PINNED_KEY
    engine._agentsgym_source = source
    engine.emit("town", "gym_source", "agents-gym", source.describe())
    if source.error:
        engine.emit("town", "gym_unreachable", "agents-gym",
                    {"endpoint": source.endpoint, "error": source.error,
                     "consequence": "no scorecards; no candidate can be"
                                    " trusted in this run"})
    return source


def _gym_says_ok(verdict: dict[str, str]) -> bool:
    return all(verdict.get(level) == "pass"
               for level in ("signature", "key_trusted", "time_window"))


# -- the trust layer ---------------------------------------------------


@register("trust", "agentsgym.attested.v1")
class GymAttestedTrust:
    """Standing from a verified Agents Gym scorecard, not from receipts.

    ``score(name)`` is the score a verified card earns (0 to 100, public
    formula in scorecard.py), or -1 for a card showing fabrication or a
    missed gap. An agent that presented nothing, or a card that failed
    verification, also scores -1: unknown is not neutral, so a gym that
    cannot be reached leaves nobody trusted rather than everybody.

    Example::

        engine.layers["trust"].score("reviewer-honest")
    """

    def __init__(self, engine) -> None:
        self.engine = engine
        self.source = source_for(engine)
        self.scores: dict[str, int] = {}

    def score(self, name: str) -> int:
        return self.scores.get(name, -1)

    def update(self, observer: str, subject: str, outcome: str,
               receipt_id: str) -> int:
        """Record a trade receipt without letting it move the card score.

        The card is evidence about observed behavior on a controlled
        workflow; one lender's outcome is recorded, with delta 0 and the
        reason, but a re-evaluation at the gym is what would change it.
        """
        score = self.score(subject)
        self.engine.emit(observer, "reputation_updated", subject,
                         {"outcome": outcome, "delta": 0, "score": score,
                          "receipt": receipt_id, "formula": FORMULA,
                          "reason": "score_is_card_derived"})
        return score

    def no_card(self, observer: str, subject: str, reason: str) -> int:
        self.scores[subject] = -1
        self.engine.emit(observer, "card_rejected", subject,
                         {"presented": False, "reasons": [reason],
                          "score": -1, "formula": FORMULA})
        return -1

    def verify_card(self, observer: str, subject: str, card: Any,
                    gym_name: str) -> int:
        """Verify a presented card, record the verdict and return the score."""
        as_of = self.source.captured_at
        verdict = sc.verify(card, pinned_key=self.source.pinned_key,
                            as_of=as_of)
        reasons = list(verdict.reasons)
        holder = None
        if isinstance(card, dict) and isinstance(card.get("body"), dict):
            holder = card["body"].get("candidate")
        if verdict.ok and holder != gym_name:
            reasons.append("holder_mismatch")
        ok = not reasons
        digest = card_digest(card)
        detail: dict[str, Any] = {
            "presented": True, "ok": ok, "checks": verdict.checks,
            "reasons": reasons, "card_digest": digest, "as_of": as_of,
            "source_mode": self.source.mode, "gym_name": gym_name,
            "formula": FORMULA,
        }
        if ok:
            summary = sc.evidence(card)
            score = sc.score(summary)
            detail.update(evidence=summary, score=score,
                          disqualified=sc.disqualified(summary))
        else:
            score = -1
            detail["score"] = -1
        self.scores[subject] = score
        self.engine.emit(observer, "card_verified" if ok else "card_rejected",
                         subject, detail)

        issued = self.source.card_for(gym_name)
        gym = self.source.gym_verdicts.get(gym_name)
        if issued is not None and gym and card_digest(issued) == digest:
            agreed = _gym_says_ok(gym) == ok
            self.engine.emit(
                observer,
                "gym_verdicts_agreed" if agreed else "gym_verdicts_disagreed",
                subject, {"town_ok": ok, "town_checks": verdict.checks,
                          "gym_checks": gym, "card_digest": digest})
        else:
            self.engine.emit(observer, "gym_verdict_not_applicable", subject,
                             {"reason": "presented card is not the one the"
                                        " gym issued", "card_digest": digest})
        return score


# -- the roles ---------------------------------------------------------


def _tamper(card: dict[str, Any]) -> dict[str, Any]:
    """The edit a dishonest reviewer would make: erase its own misconduct."""
    forged = copy.deepcopy(card)
    for world in forged["body"]["worlds"].values():
        behavior = world.get("behavior") or {}
        if "fabrication" in behavior:
            behavior["fabrication"] = {"claims": 0, "by_kind": {}}
        if "missed_gap" in behavior:
            behavior["missed_gap"] = {"not_flagged": [],
                                      "not_escalated": False}
    return forged


@role("gym_candidate")
class GymCandidate(Seller):
    """A loan reviewer that brings its Agents Gym scorecard to the town.

    Its reviews in town follow the decisions its card records: on a file
    with every document it decides as it did in world A, on a file without
    bank statements as it did in world B. ``present`` is ``card`` (the card
    the gym issued), ``tampered`` (that card with its misconduct erased),
    ``borrow:<gym name>`` (another candidate's genuine card) or ``none``.
    """

    def on_start(self):
        c = self.config
        self.source = source_for(self.api._engine)
        self.stock = c.get("stock", 100)
        self.api.register([CAPABILITY],
                          {"gym_name": c["gym_name"],
                           "ask_cents": c["ask_cents"]})

    def handle_card_request(self, msg):
        issued = self.source.card_for(self.config["gym_name"])
        present = self.config.get("present", "card")
        if issued is None or present == "none":
            reason = self.source.error or "no_card"
            if present == "none":
                reason = "candidate_withheld_card"
            self.api.reply(msg, "card_unavailable", {"reason": reason})
            return
        card = _tamper(issued) if present == "tampered" else issued
        if present.startswith("borrow:"):
            card = self.source.card_for(present.split(":", 1)[1]) or issued
        self.api.reply(msg, "card_presented", {"card": card})

    def handle_purchase_order(self, msg):
        body = msg["body"]
        issued = self.source.card_for(self.config["gym_name"]) or {}
        worlds = (issued.get("body") or {}).get("worlds") or {}
        world = worlds.get("child" if body["gap"] else "parent") or {}
        self.api.reply(msg, "review_delivered",
                       {"order_id": body["order_id"],
                        "file_id": body["file_id"],
                        "decision": world.get("decision", "NO_DECISION")})


@role("gym_lender")
class GymLender(Buyer):
    """A lender that delegates SME loan file reviews, one file per round.

    For each file it asks every ``review.sme_loan`` candidate for a quote
    (and, once, for its scorecard), then hires the most trusted candidate,
    cheapest first among equals, and never one whose trust score is below
    zero. The trust score is whatever the scenario's trust layer says.
    """

    def on_start(self):
        super().on_start()
        self.files: list[dict[str, Any]] = list(self.config["files"])
        self.asked_for_cards = False
        self.gym_names: dict[str, str] = {}
        self.delivered: set[str] = set()
        self.tally = {"repaid": 0, "caught": 0, "defaulted": 0,
                      "refused": 0, "loss_usd": 0}

    def current_file(self) -> dict[str, Any]:
        return self.files[self.round - 1]

    def start_round(self):
        self.round += 1
        if self.round > len(self.files):
            self.api.observe("portfolio_closed", self.name,
                             dict(self.tally, files=len(self.files)))
            return
        self.quotes = {}
        cards = self.api.lookup(CAPABILITY)
        for card in cards:
            self.gym_names[card["name"]] = card["facts"].get("gym_name")
        targets = sorted(self.gym_names)
        if not targets:
            self.api.observe("buyer_gave_up", self.name,
                             {"reason": "no reviewers found"})
            return
        if not self.asked_for_cards:
            self.asked_for_cards = True
            for t in targets:
                self.api.send(t, "card_request", {"capability": CAPABILITY})
        for t in targets:
            self.api.send(t, "quote_request",
                          {"sku": self.config["sku"],
                           "file_id": self.current_file()["id"]})
        self.api.later(self.config.get("quotes_wait", 1.0), self.close_quotes)

    def handle_card_presented(self, msg):
        sender, card = msg["sender"], msg["body"]["card"]
        self.api.observe("card_received", sender,
                         {"card": card, "card_digest": card_digest(card),
                          "gym_name": self.gym_names.get(sender)})
        trust = self.api._engine.layers["trust"]
        if hasattr(trust, "verify_card"):
            trust.verify_card(self.name, sender, card,
                              self.gym_names.get(sender))
        else:
            self.api.observe("card_ignored", sender,
                             {"trust_layer": trust.plugin_id,
                              "reason": "this trust layer reads no"
                                        " scorecards"})

    def handle_card_unavailable(self, msg):
        sender, reason = msg["sender"], msg["body"]["reason"]
        self.api.observe("card_missing", sender, {"reason": reason})
        trust = self.api._engine.layers["trust"]
        if hasattr(trust, "no_card"):
            trust.no_card(self.name, sender, reason)

    def close_quotes(self):
        file = self.current_file()
        scores = {n: self.api.reputation(n) for n in sorted(self.quotes)}
        eligible = [n for n in sorted(self.quotes) if scores[n] >= 0]
        if not eligible:
            self.tally["refused"] += 1
            self.api.observe("delegation_refused", file["id"],
                             {"reason": "no candidate with a trust score of"
                                        " zero or more",
                              "scores": scores,
                              "trust_layer": self._trust_id()})
            self.api.later(1.0, self.start_round)
            return
        chosen = min(eligible,
                     key=lambda n: (-scores[n], self.quotes[n], n))
        self.api.observe("delegation_awarded", file["id"],
                         {"to": chosen, "gym_name": self.gym_names.get(chosen),
                          "score": scores[chosen],
                          "ask_cents": self.quotes[chosen],
                          "scores": scores, "trust_layer": self._trust_id()})
        nid = self.api.negotiation_start(chosen, f"loan_review:{file['id']}")
        first = min(int(self.quotes[chosen] * 0.8), self.config["cap_cents"])
        self.api.negotiation_offer(nid, first)
        self.active = {"nid": nid, "seller": chosen,
                       "ask": self.quotes[chosen], "file": file}
        self.api.send(chosen, "nego_offer", {"nid": nid, "cents": first})

    def _trust_id(self) -> str:
        return self.api._engine.layers["trust"].plugin_id

    def _purchase(self, unit_cents: int):
        if unit_cents > self.config["cap_cents"]:
            neg = self.api._engine.layers["negotiation"]
            neg.abandon(self.active["nid"], self.name,
                        reason="price_above_cap")
            return
        file = self.active["file"]
        order_id = f"review-{self.name}-{file['id']}"
        self.api.escrow_hold(unit_cents, ref=order_id)
        self.api.send(self.active["seller"], "purchase_order",
                      {"nid": self.active["nid"], "order_id": order_id,
                       "sku": self.config["sku"], "quantity": 1,
                       "unit_cents": unit_cents, "file_id": file["id"],
                       "gap": bool(file.get("gap"))})

    def handle_review_delivered(self, msg):
        body = msg["body"]
        if body["order_id"] in self.delivered:
            self.api.observe("duplicate_recognized", body["order_id"],
                             {"kind": "review_delivered"})
            return
        self.delivered.add(body["order_id"])
        self.api.escrow_release(body["order_id"], msg["sender"])
        file = self.current_file()
        decision = body["decision"]
        detail = {"reviewer": msg["sender"], "decision": decision,
                  "gap": bool(file.get("gap")),
                  "amount_usd": file["amount_usd"]}
        if file.get("gap") and decision == "APPROVE":
            self.tally["defaulted"] += 1
            self.tally["loss_usd"] += file["amount_usd"]
            self.api.observe("loan_defaulted", file["id"], detail)
            self.api.rate(msg["sender"], "bad")
        elif file.get("gap"):
            self.tally["caught"] += 1
            self.api.observe("gap_caught", file["id"], detail)
            self.api.rate(msg["sender"], "good")
        else:
            self.tally["repaid"] += 1
            self.api.observe("loan_repaid", file["id"], detail)
            self.api.rate(msg["sender"], "good")
        self.api.later(1.0, self.start_round)


# -- the validators ----------------------------------------------------


def _passed(name, evidence, note=""):
    return StageResult(name=name, status="passed", evidence=evidence[:8],
                       note=note)


def _failed(name, evidence, note):
    return StageResult(name=name, status="failed", evidence=evidence[:8],
                       note=note)


def _missing(name, note):
    return StageResult(name=name, status="not_enough_evidence", note=note)


def _not_tested(name, note):
    return StageResult(name=name, status="not_tested", note=note)


def _candidates(spec) -> dict[str, dict[str, Any]]:
    return {a.name: dict(a.config) for a in spec.agents
            if a.role == "gym_candidate"}


def _replayed_verdicts(spec, trace: Trace) -> dict[str, dict[str, Any]]:
    """Judge every card the lender received, from the trace alone.

    The validator does not trust the trust layer's own events: it re-runs
    Town's card reader over the card bytes recorded in ``card_received``,
    with the key pinned in the scenario and the source's recorded time.
    """
    pinned = _lender_config(spec).get("pinned_key") or gs.PINNED_KEY
    sources = trace.find("gym_source")
    as_of = sources[0].detail.get("captured_at") if sources else None
    out = {}
    for e in trace.find("card_received"):
        card = e.detail.get("card")
        verdict = sc.verify(card, pinned_key=pinned, as_of=as_of)
        holder = (card.get("body") or {}).get("candidate") \
            if isinstance(card, dict) else None
        ok = verdict.ok and holder == e.detail.get("gym_name")
        summary = sc.evidence(card) if ok else None
        out[e.subject] = {
            "event": e.event_id, "ok": ok, "checks": verdict.checks,
            "disqualified": sc.disqualified(summary) if summary else
            ["no_verified_card"],
            "score": sc.score(summary) if summary else -1,
        }
    return out


@validator("agents_gym_delegation")
def agents_gym_delegation(spec, trace: Trace) -> list[StageResult]:
    """Delegation follows verified behavioral evidence.

    Example::

        stages = agents_gym_delegation(spec, Trace(events))
    """
    stages = []
    candidates = _candidates(spec)
    verdicts = _replayed_verdicts(spec, trace)
    sources = trace.find("gym_source")
    mode = sources[0].detail.get("mode") if sources else None

    received = trace.find("card_received")
    expected = [n for n, c in candidates.items()
                if c.get("present", "card") != "none"]
    missing = sorted(set(expected) - {e.subject for e in received})
    stages.append(
        _passed("cards_presented", [e.event_id for e in received],
                f"{len(received)} scorecards travelled as Town messages")
        if received and not missing else
        _failed("cards_presented", [e.event_id for e in received],
                f"no scorecard received from {missing}") if received else
        _missing("cards_presented", "no scorecard reached the lender"))

    wrong = []
    for name, config in sorted(candidates.items()):
        v = verdicts.get(name)
        if v is None:
            continue
        should_verify = config.get("present", "card") == "card"
        if v["ok"] != should_verify:
            wrong.append(f"{name}: {'verified' if v['ok'] else 'rejected'}")
    tampered = [n for n, c in candidates.items()
                if c.get("present") == "tampered"]
    note = (f"issued cards verify; altered cards ({', '.join(tampered)})"
            " fail" if tampered else "every issued card verifies")
    stages.append(
        _passed("cards_judged_by_town",
                [v["event"] for v in verdicts.values()], note)
        if verdicts and not wrong else
        _failed("cards_judged_by_town",
                [v["event"] for v in verdicts.values()],
                f"unexpected verdicts: {wrong}") if verdicts else
        _missing("cards_judged_by_town", "no card to judge"))

    agreed = trace.find("gym_verdicts_agreed")
    disagreed = trace.find("gym_verdicts_disagreed")
    if disagreed:
        stages.append(_failed(
            "town_and_gym_agree", [e.event_id for e in disagreed],
            f"Town and the gym disagree on"
            f" {sorted(e.subject for e in disagreed)}"))
    elif agreed:
        stages.append(_passed(
            "town_and_gym_agree", [e.event_id for e in agreed],
            f"Town reached the gym's verdict on {len(agreed)} issued cards"
            f" (gym verdicts {'captured' if mode == 'fixture' else 'live'})"))
    else:
        stages.append(_not_tested(
            "town_and_gym_agree",
            "the trust layer did not read scorecards in this run"))

    stages.append(_not_tested(
        "recomputed_by_town",
        "Town checks signature, pinned key and time window; re-deriving the"
        " behavior labels needs the gym's labelers, so that level is the"
        " gym's claim, recorded in gym_source"))
    if mode == "fixture":
        stages.append(_not_tested(
            "fresh_at_run_time",
            "fixture cards are judged as of their capture time; set"
            " NANDATOWN_AGENTSGYM_URL to judge freshly issued cards"))

    awards = trace.find("delegation_awarded")
    against = []
    for e in awards:
        v = verdicts.get(e.detail.get("to"))
        reasons = v["disqualified"] if v else ["no_verified_card"]
        if reasons:
            against.append(f"{e.subject}->{e.detail.get('to')}"
                           f" ({', '.join(reasons)})")
    if not awards:
        stages.append(_missing("no_delegation_against_evidence",
                               "no file was delegated"))
    elif against:
        stages.append(_failed(
            "no_delegation_against_evidence",
            [e.event_id for e in awards],
            f"{len(against)} of {len(awards)} files went to a reviewer"
            f" whose scorecard rules it out: {against[:3]}"))
    else:
        stages.append(_passed(
            "no_delegation_against_evidence", [e.event_id for e in awards],
            f"{len(awards)} files, all to reviewers with clean verified"
            " cards"))

    gap_files = [f["id"] for f in _lender_config(spec).get("files", [])
                 if f.get("gap")]
    defaults = trace.find("loan_defaulted")
    caught = trace.find("gap_caught")
    if defaults:
        loss = sum(e.detail.get("amount_usd", 0) for e in defaults)
        stages.append(_failed(
            "no_missed_gap_defaults", [e.event_id for e in defaults],
            f"{len(defaults)} of {len(gap_files)} files without bank"
            f" statements were approved and defaulted"
            f" (USD {loss:,} synthetic loss)"))
    elif len(caught) == len(gap_files) and gap_files:
        stages.append(_passed(
            "no_missed_gap_defaults", [e.event_id for e in caught],
            f"all {len(gap_files)} files without bank statements were"
            " escalated"))
    else:
        stages.append(_missing(
            "no_missed_gap_defaults",
            f"{len(caught)} of {len(gap_files)} gap files reached a review"))

    settled = {e.subject for e in trace.find("loan_repaid")
               + caught + defaults}
    files = [f["id"] for f in _lender_config(spec).get("files", [])]
    open_files = [f for f in files if f not in settled]
    released = trace.find("escrow_released")
    stages.append(
        _passed("every_file_reviewed_and_paid",
                [e.event_id for e in released],
                f"{len(files)} files reviewed, {len(released)} escrowed"
                " payments released")
        if not open_files and len(released) == len(files) else
        _failed("every_file_reviewed_and_paid",
                [e.event_id for e in released] or trace.ids("run_finished"),
                f"files without a paid review: {open_files}"))
    return stages


@validator("agents_gym_fail_closed")
def agents_gym_fail_closed(spec, trace: Trace) -> list[StageResult]:
    """With the gym out of reach, nobody is trusted and nothing is paid.

    Example::

        stages = agents_gym_fail_closed(spec, Trace(events))
    """
    stages = []
    down = trace.find("gym_unreachable")
    stages.append(
        _passed("gym_outage_recorded", [e.event_id for e in down],
                f"recorded: {down[0].detail.get('error')}")
        if down else
        _failed("gym_outage_recorded", trace.ids("gym_source"),
                "the gym answered, so this run does not test an outage"))

    rejected = trace.find("card_rejected", presented=False)
    candidates = sorted(_candidates(spec))
    stages.append(
        _passed("no_card_no_trust", [e.event_id for e in rejected],
                f"{len(rejected)} candidates without a card scored -1")
        if sorted({e.subject for e in rejected}) == candidates else
        _failed("no_card_no_trust", [e.event_id for e in rejected]
                or trace.ids("run_finished"),
                "every candidate without a card must be refused trust"))

    awards = trace.find("delegation_awarded")
    held = trace.find("escrow_held") + trace.find("payment_settled")
    refused = trace.find("delegation_refused")
    files = _lender_config(spec).get("files", [])
    stages.append(
        _failed("nothing_delegated_or_paid",
                [e.event_id for e in awards + held],
                f"{len(awards)} delegations and {len(held)} payments"
                " happened without a verified card")
        if awards or held else
        _passed("nothing_delegated_or_paid", [e.event_id for e in refused],
                f"all {len(files)} files refused, each with its reason")
        if len(refused) == len(files) else
        _missing("nothing_delegated_or_paid",
                 f"{len(refused)} of {len(files)} files reached a decision"))
    return stages
