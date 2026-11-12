"""Referral in Town: hire research agents through Referral, trust only checked work.

Referral (https://github.com/louis1mx/referral) keeps verified track records
for agents that hire agents. Before a job the worker locks its forecast;
after it delivers, Referral's Checker verifies every cited quote against a
permitted, dated corpus and returns accept or reject_and_rematch. Referral
runs as its own MCP server process; this file talks to it over stdio.

What this file registers (load it with ``plugin_files:``):

- trust plugin ``referral.v1``: Town's trust layer backed by Referral's
  Checker. A trade receipt moves a score only when the trust layer relayed a
  Referral verdict for that buyer and worker; the checked verdict, not the
  buyer's claim, sets the sign. A receipt with no checked job counts 0.
- roles ``referral_service`` (the separately running Referral server, as a
  declared participant), ``research_buyer`` and ``research_worker``.
- validators ``referral_fabricated_citation``, ``referral_unlocked_forecast``
  and ``referral_fail_closed``.

Every scenario carries the same agents. Only ``layers.trust`` decides whether
deliveries go through Referral: with ``referral.v1`` the buyer hires through
Referral's Matcher and pays only for checked work; with Town's default
``reputation.v1`` the same buyer accepts deliveries on arrival, as Town's
reference buyer does, and rates them good.

Nothing secret enters the trace: Referral agent keys stay in agent memory and
are never put in events, intents or message bodies.
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_stdio import McpError, StdioMcpClient  # noqa: E402

from nandatown.layers import register  # noqa: E402
from nandatown.sim.agents import SimAgent, role  # noqa: E402
from nandatown.sim.validators import (  # noqa: E402
    StageResult, Trace, _check, _event_ids, _failed, _missing, validator)

CAPABILITY = "research.competitor_facts"
ENV_COMMAND = "NANDATOWN_REFERRAL_CMD"
DEFAULT_COMMAND = ["referral", "serve", "--history", "none"]
REQUIRED_TOOLS = {"register_agent", "request_match", "hire", "get_assignment",
                  "read_document", "submit_prediction", "submit_delivery",
                  "get_record", "get_credential", "verify_credential"}

_SESSIONS: list[StdioMcpClient] = []


@atexit.register
def _close_sessions() -> None:
    for client in _SESSIONS:
        client.close()
    _SESSIONS.clear()


# -- the Referral session, owned by the engine -----------------------------


class ReferralSession:
    """One MCP session to one Referral server for the length of a run.

    Calls are synchronous: the Lab waits for each answer, so the trace order
    is the order Referral saw the calls in. Each call is recorded as a
    ``referral_call`` intent naming the actor and tool, never the arguments,
    because those carry the actor's Referral key.
    """

    def __init__(self, engine, owner: str, command: list[str]):
        self.engine = engine
        self.owner = owner
        self.command = command
        self.client: StdioMcpClient | None = None
        self.down_reason = ""

    @property
    def up(self) -> bool:
        return self.client is not None and not self.down_reason

    def connect(self) -> bool:
        client = StdioMcpClient(self.command)
        try:
            tools = client.start()
        except McpError as exc:
            client.close()
            self._down(f"connect failed: {exc}")
            return False
        _SESSIONS.append(client)
        self.client = client
        missing = sorted(REQUIRED_TOOLS - set(tools))
        self.engine.emit(self.owner, "referral_connected", "referral", {
            "server_name": client.server_info.get("name", ""),
            "protocol_version": client.protocol_version,
            "transport": "mcp-stdio",
            "tools": tools,
            "missing_tools": missing,
        })
        if missing:
            self._down(f"server lacks tools {missing}")
            return False
        return True

    def call(self, actor: str, tool: str, **arguments: Any) -> dict[str, Any]:
        """A tool result, or ``{"error": ...}``. Referral reports refusals
        as ``error``; an unreachable server is recorded once and then every
        call answers ``{"error": "referral unavailable"}``."""
        self.engine.record_intent(actor, "referral_call", {"tool": tool})
        if not self.up:
            return {"error": "referral unavailable"}
        try:
            return self.client.call(tool, **arguments)
        except McpError as exc:
            self._down(f"{tool} failed: {exc}")
            return {"error": "referral unavailable"}

    def _down(self, reason: str) -> None:
        if self.down_reason:
            return
        self.down_reason = reason
        self.engine.emit(self.owner, "referral_unavailable", "referral",
                         {"reason": reason})


def session_of(engine) -> ReferralSession | None:
    return getattr(engine, "referral_session", None)


def checked_mode(engine) -> bool:
    """True when the trust layer routes deliveries through Referral."""
    return bool(getattr(engine.layers["trust"], "checks_with_referral", False))


# -- trust plugin ------------------------------------------------------------


@register("trust", "referral.v1")
class ReferralCheckedTrust:
    """Reputation from Referral-checked work: a receipt counts only with a
    relayed Referral verdict, and the verdict sets its sign.

    score(subject) = sum over receipts about subject of
                     +1 if the matching Referral job passed,
                     -1 if it failed or timed out,
                      0 if no checked job backs the receipt.
    """

    checks_with_referral = True

    def __init__(self, engine):
        self.engine = engine
        self.scores: dict[str, int] = {}
        # (buyer, worker) -> verdicts relayed but not yet matched to a receipt
        self.pending: dict[tuple[str, str], list[dict[str, str]]] = {}

    def score(self, name: str) -> int:
        return self.scores.get(name, 0)

    def relay_verdict(self, buyer: str, worker: str, job_id: str,
                      status: str) -> None:
        self.pending.setdefault((buyer, worker), []).append(
            {"job_id": job_id, "status": status})

    def update(self, observer: str, subject: str, outcome: str,
               receipt_id: str) -> int:
        queue = self.pending.get((observer, subject)) or []
        checked = queue.pop(0) if queue else None
        if checked is None:
            delta, reason, job, status = 0, "no_checked_referral_job", "", ""
        else:
            job, status = checked["job_id"], checked["status"]
            delta = 1 if status == "passed" else -1
            claimed = "good" if delta > 0 else "bad"
            reason = "counted" if claimed == outcome else "claim_overridden_by_check"
        self.scores[subject] = self.score(subject) + delta
        self.engine.emit(observer, "reputation_updated", subject, {
            "outcome": outcome, "delta": delta,
            "score": self.scores[subject], "receipt": receipt_id,
            "formula": "referral.v1", "referral_job": job,
            "checked_status": status, "reason": reason})
        return self.scores[subject]


# -- roles -------------------------------------------------------------------


@role("referral_service")
class ReferralService(SimAgent):
    """The Referral server, started as its own process and joined over MCP.

    ``config.command`` (a list) names the server; without it the command
    comes from ``NANDATOWN_REFERRAL_CMD`` or defaults to
    ``referral serve --history none``. ``--history none`` starts Referral
    with no simulated workers, so it can only match agents of this town.
    """

    def on_start(self):
        command = self.config.get("command")
        if not command:
            env = os.environ.get(ENV_COMMAND, "")
            command = shlex.split(env) if env else list(DEFAULT_COMMAND)
        session = ReferralSession(self.api._engine, self.name, list(command))
        self.api._engine.referral_session = session
        session.connect()


class _ReferralClientMixin:
    """A Town agent that is also a registered Referral agent."""

    referral_id = ""
    referral_key = ""

    def referral(self, tool: str, **arguments: Any) -> dict[str, Any]:
        session = session_of(self.api._engine)
        if session is None:
            return {"error": "no referral_service participant in this town"}
        return session.call(self.name, tool, **arguments)

    def authed(self, tool: str, **arguments: Any) -> dict[str, Any]:
        return self.referral(tool, agent_id=self.referral_id,
                             key=self.referral_key, **arguments)

    def join_referral(self, role_name: str, **extra: Any) -> bool:
        answer = self.referral("register_agent", name=self.name,
                               role=role_name, owner=f"town:{self.name}",
                               model="scripted-town-fixture", **extra)
        if "error" in answer:
            self.api.observe("referral_call_refused", self.name,
                             {"tool": "register_agent",
                              "error": answer["error"]})
            return False
        self.referral_id, self.referral_key = answer["agent_id"], answer["key"]
        self.api.observe("referral_registered", self.name,
                         {"role": role_name, "referral_id": self.referral_id,
                          "tier": answer.get("tier", "")})
        return True


_PATTERNS = {
    "hq_city": r"headquartered in ([^.]+)\.",
    "founded_year": r"founded in (\d{4})",
    "flagship_product": r"flagship product is the (.+?) warehouse robot",
    "latest_funding": r"announced an? (\$\d+M) funding round",
    "headcount": r"has (\d+) employees",
}
_DYNAMIC = {"latest_funding", "headcount"}

# What a worker that never opens the documents writes instead: plausible
# values and a quote that reads like the source but is not in it.
_FABRICATED = {
    "hq_city": "San Francisco",
    "founded_year": "2011",
    "flagship_product": "Atlas",
    "latest_funding": "$50M",
    "headcount": "300",
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@role("research_worker")
class ResearchWorker(_ReferralClientMixin, SimAgent):
    """Sells sourced competitor facts. ``behavior`` is scripted:

    - ``honest`` reads the cited document and quotes it verbatim;
    - ``fabricate`` never reads anything: it invents plausible values and
      quotes that do not appear in the cited document;
    - ``hasty`` is honest but first tries to deliver without locking a
      forecast, which Referral must refuse.
    """

    def on_start(self):
        c = self.config
        self.behavior = c.get("behavior", "honest")
        self.api.register([CAPABILITY],
                          {"ask_cents": c["ask_cents"],
                           "corpus": "referral-acme-competitors"})
        if checked_mode(self.api._engine):
            self.join_referral("worker", price_usd=c["ask_cents"] / 100)

    def handle_quote_request(self, msg):
        self.api.reply(msg, "quote_response",
                       {"unit_cents": self.config["ask_cents"]})

    def _facts(self, slots: list[dict[str, str]]) -> list[dict[str, str]] | None:
        facts = []
        for slot in slots:
            competitor, attribute = slot["competitor"], slot["attribute"]
            kind = "update-2026" if attribute in _DYNAMIC else "profile-2024"
            doc_id = f"{_slug(competitor)}-{kind}"
            if self.behavior == "fabricate":
                value = _FABRICATED[attribute]
                quote = (f"{competitor} lists its "
                         f"{attribute.replace('_', ' ')} as {value}.")
            else:
                doc = self.referral("read_document", doc_id=doc_id)
                if "error" in doc:
                    self.api.observe("worker_blocked", self.name,
                                     {"doc_id": doc_id, "error": doc["error"]})
                    return None
                match = re.search(_PATTERNS[attribute], doc["text"])
                if match is None:
                    return None
                value = match.group(1).strip()
                quote = next(s for s in re.split(r"(?<=\.)\s+", doc["text"])
                             if match.group(0) in s)
            facts.append({"competitor": competitor, "attribute": attribute,
                          "value": value, "doc_id": doc_id, "quote": quote})
        return facts

    def handle_research_order(self, msg):
        body = msg["body"]
        facts = self._facts(body["slots"])
        if facts is None:
            self.api.observe("worker_gave_up", self.name,
                             {"reason": "could not read the sources"})
            return
        if "job_id" not in body:  # an unchecked Town order
            self.api.reply(msg, "research_delivered",
                           {"order_id": body["order_id"],
                            "facts": [{k: f[k] for k in
                                       ("competitor", "attribute", "value",
                                        "doc_id")} for f in facts]})
            return
        job_id = body["job_id"]
        listed = self.authed("get_assignment")
        if job_id not in [a.get("job_id") for a in listed.get("assignments", [])]:
            self.api.observe("worker_gave_up", job_id,
                             {"reason": "job not assigned in Referral"})
            return
        if self.behavior == "hasty":
            early = self.authed("submit_delivery", job_id=job_id, facts=facts)
            self.api.observe("referral_call_refused" if "error" in early
                             else "referral_call_accepted", job_id,
                             {"tool": "submit_delivery",
                              "before_forecast": True,
                              "error": early.get("error", "")})
        p_success = 0.9 if self.behavior != "fabricate" else 0.95
        locked = self.authed("submit_prediction", job_id=job_id,
                             p_success=p_success,
                             cost_usd=self.config["ask_cents"] / 100,
                             finish_seconds=30.0)
        if "error" in locked:
            self.api.observe("referral_call_refused", job_id,
                             {"tool": "submit_prediction",
                              "error": locked["error"]})
            return
        self.api.observe("referral_prediction_locked", job_id,
                         {"p_success": p_success})
        delivered = self.authed("submit_delivery", job_id=job_id, facts=facts)
        if "error" in delivered:
            self.api.observe("referral_call_refused", job_id,
                             {"tool": "submit_delivery",
                              "error": delivered["error"]})
            return
        self.api.observe("referral_delivery_submitted", job_id,
                         {"status": delivered.get("status", "")})
        self.api.reply(msg, "research_delivered", {"job_id": job_id})


@role("research_buyer")
class ResearchBuyer(_ReferralClientMixin, SimAgent):
    """Buys sourced competitor facts through Town.

    Discovery, quotes, escrow and receipts are Town's. When the trust layer
    checks with Referral, matching, the contract and acceptance are
    Referral's: the buyer pays only after an ``accept`` and rematches after
    ``reject_and_rematch``. Otherwise it accepts what arrives, as Town's
    reference buyer accepts a delivery, and rates it good.
    """

    def on_start(self):
        self.quotes: dict[str, int] = {}
        self.excluded: list[str] = []
        self.hires = 0
        self.jobs: dict[str, str] = {}
        self.done = False
        self.api.register(["buy"])
        if checked_mode(self.api._engine):
            self.join_referral("buyer")
        self.api.later(0.5, self.ask_quotes)

    def ask_quotes(self):
        cards = self.api.lookup(CAPABILITY)
        if not cards:
            self.api.observe("buyer_gave_up", self.name,
                             {"reason": "no research workers found"})
            return
        for card in cards:
            self.api.send(card["name"], "quote_request",
                          {"capability": CAPABILITY})
        self.api.later(1.0, self.close_quotes)

    def handle_quote_response(self, msg):
        self.quotes[msg["sender"]] = msg["body"]["unit_cents"]

    def close_quotes(self):
        if not self.quotes:
            self.api.observe("buyer_gave_up", self.name,
                             {"reason": "no quotes arrived"})
            return
        if checked_mode(self.api._engine):
            self.hire_through_referral()
        else:
            self.order_directly()

    # -- unchecked path: Town's reference acceptance ----------------------

    def order_directly(self):
        worker, cents = min(self.quotes.items(), key=lambda kv: (kv[1], kv[0]))
        order_id = f"order-{self.name}-1"
        self.api.escrow_hold(min(cents, self.config["cap_cents"]), ref=order_id)
        self.jobs[order_id] = worker
        self.api.send(worker, "research_order",
                      {"order_id": order_id, "company": self.config["company"],
                       "slots": self.config["slots"]})

    # -- checked path: Referral --------------------------------------------

    def hire_through_referral(self):
        if self.hires >= self.config.get("max_hires", 3):
            self.api.observe("buyer_gave_up", self.name,
                             {"reason": "hire limit reached"})
            return
        if not self.referral_id:
            self.api.observe("buyer_gave_up", self.name,
                             {"reason": "not registered with Referral"})
            return
        match = self.authed(
            "request_match", company=self.config["company"],
            slots=self.config["slots"],
            budget_usd=self.config["cap_cents"] / 100,
            deadline_seconds=120.0, exclude_workers=list(self.excluded))
        candidate = (match.get("candidate") or {}).get("name")
        self.api.observe("referral_match", match.get("match_id", ""), {
            "action": match.get("action", ""), "candidate": candidate or "",
            "difficulty_band": match.get("difficulty_band", ""),
            "tier": (match.get("candidate") or {}).get("tier", ""),
            "excluded": list(self.excluded), "error": match.get("error", "")})
        if not candidate or candidate not in self.quotes:
            self.api.observe("buyer_gave_up", self.name, {
                "reason": "no eligible Referral match among quoted workers"})
            return
        hired = self.authed("hire", match_id=match["match_id"])
        if "error" in hired:
            self.api.observe("buyer_gave_up", self.name,
                             {"reason": f"hire refused: {hired['error']}"})
            return
        self.hires += 1
        job_id = hired["job_id"]
        self.jobs[job_id] = candidate
        self.api.observe("referral_hired", job_id, {"worker": candidate})
        self.api.escrow_hold(self.quotes[candidate], ref=job_id)
        self.api.send(candidate, "research_order",
                      {"job_id": job_id, "company": self.config["company"],
                       "slots": self.config["slots"]})

    def handle_research_delivered(self, msg):
        body = msg["body"]
        if "order_id" in body:
            ref = body["order_id"]
            worker = self.jobs.get(ref, msg["sender"])
            self.api.observe("delivery_accepted_unchecked", ref,
                             {"worker": worker, "facts": len(body["facts"])})
            self.api.escrow_release(ref, worker)
            self.api.rate(worker, "good")
            self.finish()
            return
        job_id = body["job_id"]
        worker = self.jobs.get(job_id)
        if worker is None or worker != msg["sender"]:
            self.api.observe("delivery_ignored", job_id,
                             {"reason": "not a job this buyer hired"})
            return
        record = self.authed("get_record", job_id=job_id)
        status = record.get("status", "")
        recommendation = record.get("recommendation", "")
        self.api.observe("referral_verdict", job_id, {
            "worker": worker, "status": status,
            "recommendation": recommendation,
            "locked_forecast": record.get("locked_forecast") is not None,
            "facts": [{"slot": f"{f.get('competitor')}/{f.get('attribute')}",
                       "doc_id": f.get("doc_id"), "check": f.get("check")}
                      for f in record.get("facts", [])],
            "failure_traced_to": record.get("failure_traced_to", []),
            "notary_entries": record.get("notary_entries", 0),
            "error": record.get("error", "")})
        trust = self.api._engine.layers["trust"]
        if status and hasattr(trust, "relay_verdict"):
            trust.relay_verdict(self.name, worker, job_id, status)
        if recommendation == "accept":
            self.api.escrow_release(job_id, worker)
            self.api.rate(worker, "good")
            self.check_credential(worker)
            self.finish()
        else:
            self.api.escrow_refund(job_id)
            self.api.rate(worker, "bad")
            self.excluded.append(worker)
            self.hire_through_referral()

    def check_credential(self, worker: str):
        """Fetch the hired worker's signed Referral credential and verify it
        twice: offline in Town against Referral's published key, and with
        Referral's own verify_credential."""
        credential = self.referral("get_credential",
                                   worker_id=f"agt-{_slug(worker)}")
        if "error" in credential:
            self.api.observe("referral_credential_checked", worker,
                             {"valid": False, "error": credential["error"]})
            return
        issuer = self.referral("get_issuer_key")
        town_ok, town_reason = verify_offline(credential, issuer.get("jwk") or {})
        verdict = self.referral("verify_credential", credential=credential)
        claims = credential.get("credentialSubject") or {}
        self.api.observe("referral_credential_checked", worker, {
            "valid": town_ok and bool(verdict.get("valid")),
            "verified_by_town": town_ok, "town_reason": town_reason,
            "verified_by_referral": bool(verdict.get("valid")),
            "subject_matches": claims.get("name") == worker,
            "tier": claims.get("tier", ""),
            "evidence": claims.get("evidence", "")})

    def finish(self):
        if self.done:
            return
        self.done = True
        ranking = [c["name"] for c in self.api.lookup(CAPABILITY)]
        self.api.observe("registry_ranking", CAPABILITY, {
            "ranking": ranking,
            "scores": {n: self.api.reputation(n) for n in ranking}})


def verify_offline(credential: dict, jwk: dict) -> tuple[bool, str]:
    """Town's own check of a Referral credential: Ed25519 over canonical
    JSON (sorted keys, no spaces) of everything but the proof, with the key
    Referral publishes. Imports nothing from Referral."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    proof = credential.get("proof")
    if not isinstance(proof, dict) or proof.get("verificationMethod") != jwk.get("kid"):
        return False, "proof missing or signed with another key"

    def b64u(text: str) -> bytes:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    unsigned = {k: v for k, v in credential.items() if k != "proof"}
    message = json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                         default=str).encode()
    try:
        Ed25519PublicKey.from_public_bytes(b64u(jwk["x"])).verify(
            b64u(proof["proofValue"]), message)
    except (InvalidSignature, KeyError, ValueError):
        return False, "signature does not match"
    return True, "ok"


# -- validators ----------------------------------------------------------------


def _workers(spec, *behaviors: str) -> list[str]:
    return [a.name for a in spec.agents if a.role == "research_worker"
            and a.config.get("behavior", "honest") in behaviors]


def _connected(trace: Trace) -> StageResult:
    connected = trace.find("referral_connected")
    ok = bool(connected) and not connected[0].detail.get("missing_tools")
    return _check("referral_connected", ok, _event_ids(connected),
                  "Referral must answer the MCP handshake and list every tool"
                  " the workflow calls")


def _forecasts_first(trace: Trace) -> StageResult:
    delivered = trace.find("referral_delivery_submitted")
    locked = {e.subject: trace.index(e)
              for e in trace.find("referral_prediction_locked")}
    verdicts = trace.find("referral_verdict")
    ok = bool(delivered) and all(
        e.subject in locked and locked[e.subject] < trace.index(e)
        for e in delivered) and all(v.detail.get("locked_forecast")
                                    for v in verdicts)
    return _check("forecast_locked_before_delivery", ok,
                  _event_ids(delivered + verdicts),
                  "every accepted Referral delivery needs a forecast locked"
                  " before it, and every verdict must show that forecast")


def _released_to(trace: Trace, names: list[str]) -> list:
    return [e for e in trace.find("escrow_released")
            if e.detail.get("to") in names]


def _reputation_matches_checks(trace: Trace, spec) -> StageResult:
    """No reputation credit without a passed Referral check, and Town's
    registry must rank checked work above fabricated work."""
    updates = trace.find("reputation_updated")
    verdict_of = {e.subject: e.detail.get("status")
                  for e in trace.find("referral_verdict")}
    bad = []
    for e in updates:
        delta = e.detail.get("delta")
        job = e.detail.get("referral_job", "")
        status = verdict_of.get(job) if job else None
        if type(delta) is not int:
            bad.append(e)
        elif delta > 0 and status != "passed":
            bad.append(e)
        elif delta < 0 and status not in ("failed", "timeout"):
            bad.append(e)
    ranking = trace.find("registry_ranking")
    order = ranking[-1].detail.get("ranking", []) if ranking else []
    fabricators = _workers(spec, "fabricate")
    ranked_ok = bool(order) and not (order and order[0] in fabricators)
    if bad:
        return _failed("reputation_matches_checks", _event_ids(bad), (
            "the trust layer moved a score without a matching Referral"
            " check, e.g. credit for a delivery whose citations were never"
            " verified"))
    if not ranked_ok:
        return _failed("reputation_matches_checks", _event_ids(ranking),
                       "Town's registry ranks a fabricating worker first")
    return _check("reputation_matches_checks", bool(updates),
                  _event_ids(updates + ranking),
                  "the buyer must file at least one receipt")


@validator("referral_fabricated_citation")
def referral_fabricated_citation(spec, trace: Trace) -> list[StageResult]:
    """The cheapest worker fabricates citations. Referral must catch it,
    the buyer must not pay it, must rematch to a worker whose delivery
    passes, and Town's shared reputation must follow the checks."""
    stages = [_connected(trace)]
    fabricators = _workers(spec, "fabricate")
    honest = _workers(spec, "honest", "hasty")

    quotes = [e for e in trace.find("message_sent")
              if e.detail.get("kind") == "quote_response"]
    quoted = {e.observer for e in quotes}
    stages.append(_check(
        "town_discovery", set(fabricators + honest) <= quoted,
        _event_ids(quotes),
        "the buyer must find every worker through Town's registry and get a"
        " quote from each"))

    stages.append(_forecasts_first(trace))

    verdicts = trace.find("referral_verdict")
    caught = [v for v in verdicts if v.detail.get("worker") in fabricators
              and v.detail.get("status") == "failed"
              and v.detail.get("recommendation") == "reject_and_rematch"
              and v.detail.get("facts")
              and all(str(f.get("check", "")).startswith("fail: quote does not appear")
                      for f in v.detail["facts"])]
    paid_fabricator = _released_to(trace, fabricators)
    refunded = [e for e in trace.find("escrow_refunded")
                if e.subject in {v.subject for v in caught}]
    if paid_fabricator:
        stages.append(_failed(
            "fabrication_rejected", _event_ids(paid_fabricator),
            "the buyer paid a worker whose citations are fabricated"))
    else:
        stages.append(_check(
            "fabrication_rejected", bool(caught) and bool(refunded),
            _event_ids(caught + refunded),
            "Referral's Checker must fail every fabricated fact because its"
            " quote is not in the cited document, recommend a rematch, and"
            " the buyer's escrow must be refunded"))

    rematch = [e for e in trace.find("referral_match")
               if set(fabricators) & set(e.detail.get("excluded", []))
               and e.detail.get("candidate") in honest]
    passed = [v for v in verdicts if v.detail.get("worker") in honest
              and v.detail.get("status") == "passed"
              and v.detail.get("recommendation") == "accept"
              and all(f.get("check") == "pass" for f in v.detail.get("facts", []))]
    paid_honest = _released_to(trace, honest)
    ordered = bool(caught and rematch and passed) and (
        trace.index(caught[0]) < trace.index(rematch[0])
        < trace.index(passed[0]))
    stages.append(_check(
        "rematched_and_accepted", ordered and bool(paid_honest),
        _event_ids(rematch + passed + paid_honest),
        "after the rejection the buyer must rematch with the fabricator"
        " excluded, and pay the worker whose delivery passes"))

    stages.append(_reputation_matches_checks(trace, spec))

    creds = [e for e in trace.find("referral_credential_checked")
             if e.subject in honest]
    stages.append(_check(
        "credential_verified", bool(creds) and all(
            e.detail.get("verified_by_town") and e.detail.get("verified_by_referral")
            and e.detail.get("subject_matches") for e in creds),
        _event_ids(creds),
        "Referral's signed credential for the hired worker must verify in"
        " Town against Referral's published key, and name that worker"))
    return stages


@validator("referral_unlocked_forecast")
def referral_unlocked_forecast(spec, trace: Trace) -> list[StageResult]:
    """A worker delivers before locking its forecast. Referral must refuse
    the delivery, then accept the same work once a forecast is locked."""
    stages = [_connected(trace)]
    hasty = _workers(spec, "hasty")
    refused = [e for e in trace.find("referral_call_refused")
               if e.detail.get("tool") == "submit_delivery"
               and e.detail.get("before_forecast")
               and "forecast" in str(e.detail.get("error", "")).lower()]
    accepted_early = [e for e in trace.find("referral_call_accepted")
                      if e.detail.get("before_forecast")]
    if accepted_early:
        stages.append(_failed("unlocked_delivery_refused",
                              _event_ids(accepted_early),
                              "Referral accepted a delivery with no locked"
                              " forecast"))
    else:
        stages.append(_check("unlocked_delivery_refused", bool(refused),
                             _event_ids(refused),
                             "the early delivery must be refused with a"
                             " reason naming the missing forecast"))
    stages.append(_forecasts_first(trace))
    verdicts = [v for v in trace.find("referral_verdict")
                if v.detail.get("worker") in hasty]
    # The refused attempt is notarized: register, match, contract,
    # delivery_rejected, prediction, delivery, verdict.
    logged = [v for v in verdicts if v.detail.get("status") == "passed"
              and int(v.detail.get("notary_entries") or 0) >= 5]
    stages.append(_check(
        "work_accepted_after_lock", bool(logged)
        and bool(_released_to(trace, hasty)), _event_ids(verdicts),
        "after locking a forecast the same work must pass, be paid, and the"
        " job's notary log must include the refused attempt"))
    stages.append(_reputation_matches_checks(trace, spec))
    return stages


@validator("referral_fail_closed")
def referral_fail_closed(spec, trace: Trace) -> list[StageResult]:
    """Referral is unreachable. Nothing may be paid or credited on the
    strength of checks that never ran, and the outage must be recorded."""
    stages = []
    down = trace.find("referral_unavailable")
    stages.append(_check("outage_recorded", bool(down), _event_ids(down),
                         "the failed connection must be recorded"))
    paid = trace.find("escrow_released")
    finished = trace.ids("run_finished")
    if paid:
        stages.append(_failed("no_unchecked_payment", _event_ids(paid),
                              "money moved although nothing was checked"))
    else:
        stages.append(_check("no_unchecked_payment", True, finished, ""))
    credited = [e for e in trace.find("reputation_updated")
                if type(e.detail.get("delta")) is int and e.detail["delta"] > 0]
    if credited:
        stages.append(_failed("no_unchecked_credit", _event_ids(credited),
                              "reputation rose without a check"))
    else:
        stages.append(_check("no_unchecked_credit", True, finished, ""))
    gave_up = [e for e in trace.find("buyer_gave_up")
               if "referral" in str(e.detail.get("reason", "")).lower()]
    stages.append(_check("buyer_stopped", bool(gave_up), _event_ids(gave_up),
                         "the buyer must stop and say Referral was the reason"))
    if not trace.find("participant_joined"):
        stages.append(_missing("participants", "no participants joined"))
    return stages
