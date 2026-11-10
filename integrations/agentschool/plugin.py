# SPDX-License-Identifier: Apache-2.0
"""AgentSchool as Town's trust layer: delegation gated on a credential.

AgentSchool (https://github.com/justinspar/agentschool) trains agents in a
skill, examines them on held-out work, and issues a signed, expiring credential
that other agents check before they delegate. Town's trust layer is the slot
that decides, so this plugin puts AgentSchool there: an agent's standing comes
from a credential it earned and can prove it holds, rather than from counted
receipts.

What a run exercises, end to end:

1. sellers publish capability cards to Town's registry
2. the buyer looks up the capability and challenges every candidate
3. each candidate returns its AgentSchool credential and a signature over the
   nonce, carried as ordinary Town messages over Town's transport and auth
4. the buyer verifies each one with Town's own reader of the credential format,
   and in live mode compares its verdict against AgentSchool's
5. the buyer negotiates, escrows and pays only verified holders
6. the buyer reports the outcome back to AgentSchool

Every verdict is written into the event log, so the result replays from the
bundle without AgentSchool present.

Loaded by ``plugin_files:`` in the scenarios beside this file, so it registers
one trust plugin, two roles and two validators without changing Town's own
modules.

Example::

    nandatown run integrations/agentschool/scenarios/credential_gating.yaml
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

from nandatown.records import StageResult, TownEvent
from nandatown.layers import register
from nandatown.sim.agents import Buyer, Seller, role
from nandatown.sim.validators import Trace, validator

_HERE = Path(__file__).resolve().parent


def _sibling(name: str):
    """Import a module next to this file without putting it on sys.path.

    This file is loaded by path, so it is not part of a package and cannot use
    a relative import. Loading the sibling explicitly keeps the two helper
    modules importable from the tests as well, under the same module names.
    """
    full = f"nandatown_agentschool_{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _HERE / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load {name}.py beside {__file__}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


cv = _sibling("credential_verify")
cs = _sibling("credential_source")


# -- the trust layer ---------------------------------------------------


@register("trust", "agentschool.v1")
class AgentSchoolTrust:
    """Standing from a verified AgentSchool credential, not from receipts.

    ``score(name)`` is the certified level of an agent whose presentation
    verified, and 0 for everyone else, so an agent nobody has checked and an
    agent that failed its check are both worth nothing until checked.

    Example::

        engine.layers["trust"].score("seller-certified")
    """

    def __init__(self, engine) -> None:
        self.engine = engine
        self.source = cs.source_from_env()
        self.nonces = cv.NonceLedger()
        self.description = self.source.describe()
        #: Town agent name -> the verdict Town reached on its presentation.
        self.verdicts: dict[str, Any] = {}
        #: Town agent name -> AgentSchool agent_id, as resolved by name.
        self.resolved: dict[str, str] = {}
        self._by_name: dict[str, dict[str, Any]] | None = None
        self.engine.emit("town", "credential_source", "agentschool",
                         self.description.as_detail())

    # -- the trust layer interface Town calls --------------------------

    def score(self, name: str) -> int:
        verdict = self.verdicts.get(name)
        if verdict is None or not verdict.ok:
            return 0
        level = verdict.claims.get("level")
        return int(level) if isinstance(level, int) else 0

    def update(self, observer: str, subject: str, outcome: str,
               receipt_id: str) -> int:
        """Record a trade receipt without letting it move a credential score.

        A credential is evidence about an exam, so one counterparty's receipt
        does not raise or lower it. The receipt is still recorded, with delta
        0 and the reason, and the buyer reports the same outcome to AgentSchool
        separately, where it can flag the agent for a re-test.
        """
        score = self.score(subject)
        self.engine.emit(observer, "reputation_updated", subject,
                         {"outcome": outcome, "delta": 0, "score": score,
                          "receipt": receipt_id,
                          "formula": "agentschool.v1",
                          "reason": "score_is_credential_derived"})
        return score

    # -- what the roles call -------------------------------------------

    def catalog(self, skill: str, min_level: int) -> dict[str, dict[str, Any]]:
        """What AgentSchool's own search returns for this skill, by name.

        This is recorded as evidence that discovery works, and it is not how
        the buyer finds sellers: the buyer uses Town's registry, because
        AgentSchool's search returns only currently valid credentials and so
        would never show the revoked and uncertified agents this check is
        about.
        """
        if self._by_name is None:
            found = self.source.certified(skill, min_level)
            self._by_name = {e["name"]: e for e in found
                             if isinstance(e.get("name"), str)}
            self.engine.emit(
                "town", "credential_catalog", skill,
                {"min_level": min_level, "mode": self.source.mode,
                 "names": sorted(self._by_name),
                 "count": len(self._by_name),
                 "note": "AgentSchool search_certified_agents returns only"
                         " valid, unexpired, unrevoked credentials"})
        return self._by_name

    def presentation_for(self, agentschool_name: str, *, skill: str,
                         min_level: int, audience: str) -> Any:
        """Fetch what this agent presents, or a refusal naming why not.

        The name is resolved to the identity AgentSchool knows the agent by,
        whatever the state of its credential, so a revoked credential comes
        back and is refused on its own merits rather than being invisible.
        """
        self.catalog(skill, min_level)
        agent_id = self.source.roster().get(agentschool_name)
        if agent_id is None:
            return cs.Presentation(agent_id=agentschool_name,
                                   absent_reason="unknown_to_agentschool")
        kwargs: dict[str, Any] = {"audience": audience}
        if self.source.mode == "live":
            kwargs["min_level"] = min_level
            # Town mints its own nonce so the freshness of the holder proof
            # rests on a value Town chose. Nonce timing is about the real
            # world, not the simulation's logical clock, so it uses wall time.
            kwargs["town_nonce"] = self.nonces.issue(
                agent_id, audience, now=time.time())
        return self.source.present(agent_id, **kwargs)

    def verify(self, observer: str, town_name: str, presentation: Any, *,
               skill: str, min_level: int) -> Any:
        """Verify a presentation, record the verdict, and return it.

        The nonce ledger is used only when the nonce was Town's own, which is
        live mode. A recorded proof from a fixture is verified for its holder
        signature and reported with freshness untested, because replaying a
        captured nonce cannot show that the agent answered now.
        """
        if presentation.credential_jwt is None:
            verdict = cv.Verdict(
                ok=False, checks={}, claims={},
                reasons=[presentation.absent_reason or "no_credential"])
            self.verdicts[town_name] = verdict
            self.engine.emit(observer, "credential_rejected", town_name,
                             dict(verdict.as_detail(),
                                  agentschool_agent_id=presentation.agent_id,
                                  presented=False))
            return verdict

        self.resolved[town_name] = presentation.agent_id
        # The ledger decides freshness only for a nonce Town minted. A nonce
        # AgentSchool minted is AgentSchool's to judge, and a nonce replayed
        # from a capture nobody can, so both leave freshness unchecked and the
        # validator reports it rather than passing it.
        ledger = self.nonces if presentation.nonce_origin == "town" else None
        verdict = cv.verify_presentation(
            presentation.credential_jwt,
            nonce=presentation.nonce, signature=presentation.signature,
            audience=presentation.audience, jwks=self.source.jwks,
            revocations_jwt=self.source.revocations_jwt,
            nonces=ledger, now=None,
            revocation_max_age=getattr(self.source, "revocation_max_age",
                                       None))

        detail = dict(verdict.as_detail(),
                      agentschool_agent_id=presentation.agent_id,
                      presented=True, min_level=min_level,
                      skill_required=skill,
                      nonce_origin=presentation.nonce_origin,
                      signer=presentation.signer)

        wrong_skill = verdict.claims.get("skill") != skill
        level = verdict.claims.get("level")
        under_level = not isinstance(level, int) or level < min_level
        if verdict.ok and wrong_skill:
            verdict = cv.Verdict(ok=False, checks=verdict.checks,
                                 claims=verdict.claims,
                                 reasons=[*verdict.reasons, "wrong_skill"])
            detail["reasons"] = verdict.reasons
        elif verdict.ok and under_level:
            verdict = cv.Verdict(ok=False, checks=verdict.checks,
                                 claims=verdict.claims,
                                 reasons=[*verdict.reasons, "below_min_level"])
            detail["reasons"] = verdict.reasons

        self.verdicts[town_name] = verdict
        kind = "credential_verified" if verdict.ok else "credential_rejected"
        self.engine.emit(observer, kind, town_name, detail)

        issuer = presentation.issuer_verdict
        if isinstance(issuer, dict):
            agreed = bool(issuer.get("verified")) == bool(verdict.ok)
            self.engine.emit(
                observer,
                "credential_verdicts_agreed" if agreed
                else "credential_verdicts_disagreed",
                town_name,
                {"town_ok": bool(verdict.ok),
                 "agentschool_verified": bool(issuer.get("verified")),
                 "town_checks": dict(verdict.checks),
                 "agentschool_checks": issuer.get("checks"),
                 "town_reasons": list(verdict.reasons),
                 "agentschool_reasons": issuer.get("reasons")})
        return verdict

    def report_outcome(self, observer: str, town_name: str, task_id: str,
                       success: bool, *, quality: float | None = None,
                       notes: str | None = None) -> None:
        """Send the job's result back to AgentSchool, where there is one."""
        agent_id = self.resolved.get(town_name)
        verdict = self.verdicts.get(town_name)
        jti = verdict.claims.get("jti") if verdict is not None else None
        if agent_id is None:
            # Nothing was delegated to an AgentSchool identity Town resolved,
            # which happens in the ungated control when an agent with no
            # credential wins the work. Record why rather than saying nothing.
            self.engine.emit(observer, "outcome_report_skipped", town_name,
                             {"task_id": task_id, "mode": self.source.mode,
                              "reason": "agent_not_resolved_to_agentschool"})
            return
        # AgentSchool refuses a task id it has already heard from this
        # reporter, and a seeded Lab run mints the same order id every time,
        # so the id Town reports outward is namespaced by the run. The order
        # id inside Town stays deterministic, which keeps the bundle
        # reproducible.
        reported_task = f"{self.engine.run_id}:{task_id}"[:128]
        try:
            accepted = self.source.report_outcome(
                agent_id, reported_task, success, quality=quality,
                credential_jti=jti if isinstance(jti, str) else None,
                notes=notes)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            self.engine.emit(observer, "outcome_report_failed", town_name,
                             {"task_id": task_id,
                              "error": f"{type(exc).__name__}: {exc}"})
            return
        if accepted is None:
            self.engine.emit(observer, "outcome_report_skipped", town_name,
                             {"task_id": task_id, "mode": self.source.mode,
                              "reason": "source_accepts_no_reports"})
            return
        self.engine.emit(observer, "outcome_reported", town_name,
                         {"task_id": task_id,
                          "reported_task_id": reported_task,
                          "success": bool(success),
                          "quality": quality, "credential_jti": jti,
                          "accepted": accepted.get("accepted"),
                          "weight_applied": accepted.get("weight_applied"),
                          "reason": accepted.get("reason")})


# -- roles -------------------------------------------------------------


@role("certified_seller")
class CertifiedSeller(Seller):
    """A seller that answers a credential challenge from AgentSchool.

    ``agentschool_name`` names the agent in AgentSchool whose credential this
    Town seller presents. A seller whose agent holds no usable credential
    answers the challenge with a refusal naming the reason, which is the case
    an uncertified agent produces rather than an error.

    Example::

        config: {agentschool_name: atlas, sku: widget, ask_cents: 1995}
    """

    def on_start(self) -> None:
        super().on_start()
        self.api.observe("capability_claimed", self.name,
                         {"agentschool_name": self.config["agentschool_name"],
                          "claimed_skill": self.config.get(
                              "claimed_skill", "data-driven-slides")})

    def handle_credential_challenge(self, msg: dict[str, Any]) -> None:
        body = msg["body"]
        trust = self.api._engine.layers["trust"]
        presentation = trust.presentation_for(
            self.config["agentschool_name"], skill=body["skill"],
            min_level=body["min_level"], audience=body["audience"])
        if presentation.credential_jwt is None:
            self.api.reply(msg, "credential_unavailable",
                           {"reason": presentation.absent_reason
                            or "no_credential",
                            "agentschool_agent_id": presentation.agent_id})
            return
        self.api.reply(msg, "credential_presentation", {
            "agentschool_agent_id": presentation.agent_id,
            "credential_jwt": presentation.credential_jwt,
            "nonce": presentation.nonce,
            "signature": presentation.signature,
            "audience": presentation.audience,
            "nonce_origin": presentation.nonce_origin,
            "signer": presentation.signer,
            "issuer_verdict": presentation.issuer_verdict,
        })


@role("gating_buyer")
class GatingBuyer(Buyer):
    """A buyer that demands a verified credential before it delegates.

    With ``gate: true`` only agents whose presentation verified at the
    required skill and level are asked for a quote. With ``gate: false`` the
    same run happens without the gate, which is the control: every challenge
    is still issued, verified and recorded, and the result is simply not acted
    on, so the only difference between the two runs is whether the verdict
    decides anything.

    Example::

        config: {sku: widget, require_skill: data-driven-slides, min_level: 1,
                 gate: true, quantity: 2, cap_cents: 1900}
    """

    def on_start(self) -> None:
        self.eligible: list[str] = []
        self.challenged: list[str] = []
        super().on_start()

    @property
    def _skill(self) -> str:
        return self.config.get("require_skill", "data-driven-slides")

    @property
    def _min_level(self) -> int:
        return int(self.config.get("min_level", 1))

    def start_round(self) -> None:
        self.round = getattr(self, "round", 0) + 1
        self.quotes = {}
        self.eligible = []
        cards = self.api.lookup(f"sell.{self.config['sku']}")
        self.challenged = [c["name"] for c in cards]
        if not self.challenged:
            self.api.observe("buyer_gave_up", self.name,
                             {"reason": "no sellers found"})
            return
        audience = self.config.get("audience", cs.AUDIENCE)
        for target in self.challenged:
            self.api.send(target, "credential_challenge",
                          {"skill": self._skill, "min_level": self._min_level,
                           "audience": audience})
        self.api.later(self.config.get("challenge_wait", 1.0),
                       self.close_challenges)

    def handle_credential_presentation(self, msg: dict[str, Any]) -> None:
        body = msg["body"]
        presentation = cs.Presentation(
            agent_id=body.get("agentschool_agent_id", ""),
            credential_jwt=body.get("credential_jwt"),
            nonce=body.get("nonce"), signature=body.get("signature"),
            audience=body.get("audience", cs.AUDIENCE),
            issuer_verdict=body.get("issuer_verdict"),
            nonce_origin=body.get("nonce_origin", "recorded"),
            signer=body.get("signer", "recorded"))
        verdict = self.api._engine.layers["trust"].verify(
            self.name, msg["sender"], presentation,
            skill=self._skill, min_level=self._min_level)
        if verdict.ok:
            self.eligible.append(msg["sender"])

    def handle_credential_unavailable(self, msg: dict[str, Any]) -> None:
        body = msg["body"]
        self.api._engine.layers["trust"].verify(
            self.name, msg["sender"],
            cs.Presentation(agent_id=body.get("agentschool_agent_id", ""),
                            absent_reason=body.get("reason")),
            skill=self._skill, min_level=self._min_level)

    def close_challenges(self) -> None:
        gate = bool(self.config.get("gate", True))
        targets = self.eligible if gate else list(self.challenged)
        self.api.observe("delegation_gate", self.name,
                         {"gate": gate, "challenged": sorted(self.challenged),
                          "verified": sorted(self.eligible),
                          "asked_for_quotes": sorted(targets)})
        if not targets:
            self.api.observe("buyer_gave_up", self.name,
                             {"reason": "no_verified_credential",
                              "challenged": sorted(self.challenged)})
            return
        for target in targets:
            self.api.send(target, "quote_request", {"sku": self.config["sku"]})
        self.api.later(self.config.get("quotes_wait", 1.0), self.close_quotes)

    def handle_delivery(self, msg: dict[str, Any]) -> None:
        order_id = msg["body"]["order_id"]
        already = order_id in self.released
        super().handle_delivery(msg)
        if already:
            return
        self.api._engine.layers["trust"].report_outcome(
            self.name, msg["sender"], order_id, True,
            quality=self.config.get("report_quality", 90),
            notes="nandatown delegation completed")


# -- validators --------------------------------------------------------


def _passed(name: str, evidence: list[str], note: str = "") -> StageResult:
    return StageResult(name=name, status="passed", evidence=evidence[:8],
                       note=note)


def _failed(name: str, evidence: list[str], note: str) -> StageResult:
    return StageResult(name=name, status="failed", evidence=evidence[:8],
                       note=note)


def _missing(name: str, note: str) -> StageResult:
    return StageResult(name=name, status="not_enough_evidence", note=note)


def _not_tested(name: str, note: str) -> StageResult:
    return StageResult(name=name, status="not_tested", note=note)


def _check(name: str, ok: bool, evidence: list[str], fail_note: str,
           pass_note: str = "") -> StageResult:
    if not evidence:
        return _missing(name, fail_note)
    return _passed(name, evidence, pass_note) if ok \
        else _failed(name, evidence, fail_note)


def _ids(events: list[TownEvent]) -> list[str]:
    return [e.event_id for e in events]


def _source(trace: Trace) -> tuple[StageResult, dict[str, Any]]:
    """The provenance stage, plus the recorded source description."""
    found = trace.find("credential_source")
    if not found:
        return (_missing("credential_source_recorded",
                         "the run recorded no AgentSchool source"), {})
    detail = found[0].detail if isinstance(found[0].detail, dict) else {}
    mode = detail.get("mode")
    kid = detail.get("issuer_kid")
    ok = mode in ("fixture", "live") and isinstance(kid, str) and bool(kid)
    note = (f"mode {mode}, issuer {kid}, skill {detail.get('skill')}"
            f" {detail.get('skill_version') or ''}".strip())
    return (_check("credential_source_recorded", ok, _ids(found[:1]),
                   "the source must name its mode and the issuer key it used",
                   note), detail)


def _expected(spec) -> dict[str, dict[str, Any]]:
    """Every seller the scenario declared, by Town name."""
    return {a.name: dict(a.config) for a in spec.agents
            if a.role in ("certified_seller", "seller")}


def _verified_names(trace: Trace) -> set[str]:
    return {e.subject for e in trace.find("credential_verified")
            if isinstance(e.detail, dict) and e.detail.get("ok") is True}


def _rejections(trace: Trace) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for event in trace.find("credential_rejected"):
        detail = event.detail if isinstance(event.detail, dict) else {}
        reasons = detail.get("reasons")
        out.setdefault(event.subject, []).extend(
            r for r in (reasons or []) if isinstance(r, str))
    return out


def _paid_to(trace: Trace) -> set[str]:
    paid = set()
    for event in trace.find("escrow_released") + trace.find("payment_settled"):
        detail = event.detail if isinstance(event.detail, dict) else {}
        for key in ("to",):
            if isinstance(detail.get(key), str):
                paid.add(detail[key])
    return paid


def _work_sent_to(trace: Trace) -> set[str]:
    out = set()
    for event in trace.find("message_sent", kind="purchase_order"):
        detail = event.detail if isinstance(event.detail, dict) else {}
        if isinstance(detail.get("to"), str):
            out.add(detail["to"])
    return out


def _reason_stage(name: str, trace: Trace, sellers: dict[str, dict[str, Any]],
                  marker: str, reasons: tuple[str, ...],
                  absent_note: str) -> StageResult:
    """Check every seller the scenario marked was rejected for one of ``reasons``."""
    subjects = [n for n, c in sellers.items() if c.get(marker)]
    if not subjects:
        return _not_tested(name, absent_note)
    rejections = _rejections(trace)
    events = [e for e in trace.find("credential_rejected")
              if e.subject in subjects]
    wanted = set(reasons)
    missing = [n for n in subjects if not wanted & set(rejections.get(n, []))]
    found = sorted({r for n in subjects for r in rejections.get(n, [])
                    if r in wanted})
    return _check(
        name, not missing, _ids(events),
        f"{', '.join(sorted(subjects))} must be rejected for one of"
        f" {sorted(wanted)}; none recorded for"
        f" {', '.join(sorted(missing))}",
        f"{', '.join(sorted(subjects))} rejected: {', '.join(found)}")


@validator("agentschool_credential_gating")
def agentschool_credential_gating(spec, trace: Trace) -> list[StageResult]:
    """Credential-gated delegation: only a verified holder is hired and paid.

    The stages are separate claims. Verifying a credential, refusing a revoked
    one and keeping work away from an unverified agent can each fail on their
    own, and what the selected source cannot establish is reported as untested
    rather than passed.
    """
    stages: list[StageResult] = []
    source_stage, source = _source(trace)
    stages.append(source_stage)
    mode = source.get("mode")
    cannot = set(source.get("cannot_establish") or [])
    sellers = _expected(spec)
    verified = _verified_names(trace)

    # 1. Town verified at least one credential at the required level.
    good = [e for e in trace.find("credential_verified")
            if isinstance(e.detail, dict) and e.detail.get("ok") is True
            and isinstance(e.detail.get("level"), int)
            and isinstance(e.detail.get("min_level"), int)
            and e.detail["level"] >= e.detail["min_level"]]
    stages.append(_check(
        "credential_verified", bool(good), _ids(good)
        or trace.ids("credential_rejected") or trace.ids("run_finished"),
        "no credential verified at the required skill and level",
        ", ".join(sorted({e.subject for e in good}))))

    # 2. The holder proved custody of the key the credential names.
    holder_ok = [e for e in good
                 if e.detail.get("checks", {}).get("holder_signature") is True]
    stages.append(_check(
        "holder_key_proved", bool(holder_ok), _ids(holder_ok) or _ids(good),
        "a verified credential must carry a holder signature over the nonce",
        "holder signature verified"))

    # 3. Freshness of that proof, which a replayed nonce cannot establish.
    if "nonce_freshness" in cannot:
        stages.append(_not_tested(
            "holder_proof_fresh",
            f"the {mode} source replays a recorded nonce, so this run cannot"
            " show the agent answered now; run live mode to test it"))
    else:
        fresh = [e for e in holder_ok
                 if e.detail.get("checks", {}).get("nonce_fresh") is True
                 and e.detail.get("checks", {}).get("nonce_unused") is True]
        stages.append(_check(
            "holder_proof_fresh", bool(fresh), _ids(fresh) or _ids(holder_ok),
            "the holder signature must be over a single-use nonce Town issued",
            "nonce was Town's own, within its window, and unspent"))

    # 4. A revoked credential is refused.
    stages.append(_reason_stage(
        "revoked_refused", trace, sellers, "expect_revoked", ("revoked",),
        "this scenario declares no seller with a revoked credential"))

    # 5. An agent with no credential is refused.
    stages.append(_reason_stage(
        "uncertified_refused", trace, sellers, "expect_uncertified",
        ("no_credential", "unknown_to_agentschool"),
        "this scenario declares no uncertified seller"))

    # 6. Nothing unverified received work or money.
    unverified = set(sellers) - verified
    got_work = sorted((_work_sent_to(trace) | _paid_to(trace)) & unverified)
    gate_events = trace.find("delegation_gate")
    stages.append(_check(
        "unverified_got_no_work", not got_work,
        _ids(gate_events) or trace.ids("run_finished"),
        "no unverified seller may receive a purchase order or payment;"
        f" {', '.join(got_work)} did",
        f"unverified: {', '.join(sorted(unverified)) or 'none'}"))

    # 7. The verified holder actually completed a paid trade.
    released = trace.find("escrow_released")
    to_verified = [e for e in released
                   if isinstance(e.detail, dict)
                   and e.detail.get("to") in verified]
    stages.append(_check(
        "certified_trade_completed", bool(to_verified),
        _ids(to_verified) or _ids(released) or trace.ids("run_finished"),
        "a verified holder must be paid for the delegated work",
        "escrow released to a verified holder"))

    # 8. Town's verdict and AgentSchool's own verdict agreed.
    agreed = trace.find("credential_verdicts_agreed")
    disagreed = trace.find("credential_verdicts_disagreed")
    if not agreed and not disagreed:
        stages.append(_not_tested(
            "verdicts_agreed",
            f"the {mode} source carries no AgentSchool verdict to compare"
            " against; run live mode to test it"))
    else:
        stages.append(_check(
            "verdicts_agreed", not disagreed,
            _ids(disagreed or agreed),
            "Town's verifier and AgentSchool disagreed on the same"
            " presentation",
            f"{len(agreed)} presentation(s) judged the same by both"))

    # 9. The outcome went back to AgentSchool.
    reported = trace.find("outcome_reported")
    skipped = trace.find("outcome_report_skipped")
    failed = trace.find("outcome_report_failed")
    skip_reasons = {e.detail.get("reason") for e in skipped
                    if isinstance(e.detail, dict)}
    if not reported and not failed and skipped:
        if skip_reasons == {"source_accepts_no_reports"}:
            stages.append(_not_tested(
                "outcome_reported",
                f"the {mode} source accepts no outcome reports; run live mode"
                " to test the feedback loop"))
        else:
            stages.append(_missing(
                "outcome_reported",
                "no outcome reached AgentSchool: "
                + ", ".join(sorted(r for r in skip_reasons if r))))
    else:
        accepted = [e for e in reported
                    if isinstance(e.detail, dict)
                    and e.detail.get("accepted") is True]
        refusals = {e.detail.get("reason") for e in reported
                    if isinstance(e.detail, dict)
                    and e.detail.get("accepted") is not True}
        if not accepted and not failed and refusals == {"daily_cap"}:
            # AgentSchool accepts three reports per reporter per agent per
            # day. Past that it refuses by policy, which this run cannot
            # work around and must not report as a broken feedback loop.
            stages.append(_missing(
                "outcome_reported",
                "AgentSchool refused the report under its own daily cap of"
                " three per reporter per agent; register another relying"
                " party or wait, then rerun"))
        else:
            stages.append(_check(
                "outcome_reported", bool(accepted) and not failed,
                _ids(accepted or reported or failed),
                "the buyer must report the delegated task's outcome to"
                " AgentSchool and have it accepted; refused: "
                + ", ".join(sorted(r for r in refusals if r)),
                "outcome accepted by AgentSchool"))
    return stages


@validator("agentschool_fail_closed")
def agentschool_fail_closed(spec, trace: Trace) -> list[StageResult]:
    """With no usable credential available, nothing is delegated at all.

    This is the boundary case. The only seller advertising the capability
    presents a credential AgentSchool has revoked, so the correct behavior is
    to hire nobody and record the refusal, not to fall back to hiring it
    anyway.
    """
    stages: list[StageResult] = []
    source_stage, _ = _source(trace)
    stages.append(source_stage)
    sellers = _expected(spec)

    stages.append(_reason_stage(
        "revoked_refused", trace, sellers, "expect_revoked", ("revoked",),
        "this scenario declares no seller with a revoked credential"))

    orders = _work_sent_to(trace)
    paid = _paid_to(trace)
    gate_events = trace.find("delegation_gate")
    stages.append(_check(
        "no_work_delegated", not orders and not paid,
        _ids(gate_events) or trace.ids("run_finished"),
        "no purchase order or payment may be issued when no credential"
        f" verified; orders to {sorted(orders)}, payments to {sorted(paid)}",
        "no orders and no payments"))

    refusals = [e for e in trace.find("buyer_gave_up")
                if isinstance(e.detail, dict)
                and e.detail.get("reason") == "no_verified_credential"]
    stages.append(_check(
        "refusal_recorded", bool(refusals),
        _ids(refusals) or trace.ids("buyer_gave_up")
        or trace.ids("run_finished"),
        "the buyer must record that it stopped for want of a verified"
        " credential",
        "buyer recorded no_verified_credential"))

    verified = _verified_names(trace)
    stages.append(_check(
        "nothing_verified", not verified,
        _ids(gate_events) or trace.ids("run_finished"),
        f"no credential should have verified; {sorted(verified)} did",
        "no credential verified, as expected"))
    return stages
