"""The pinned stage evaluator.

Each stage is a separate claim with a separate failure boundary.
Acceptance, claiming, receipt, processing, response, and semantic
correctness are always judged separately; an HTTP success response never
becomes proof that the agent understood or completed the task. Missing
evidence stays missing: it is reported as Not enough evidence, never
inferred.
"""

from __future__ import annotations

import time

from .records import (
    EvidenceResult,
    StageResult,
    TestProfile,
    TownEvent,
    canonical_json,
    fingerprint,
    json_type,
)

EVALUATOR_VERSION = "0.5.0"
LEGACY_EVALUATOR_VERSION = "0.2.0"
CORRELATION_EVALUATOR_VERSION = "0.3.0"
# The rules each Track evaluator version applies. A recorded bundle replays
# under the rules of the version it recorded, looked up here by that
# version, so releasing a new version adds a row and changes no earlier
# one. Deciding them by comparison with EVALUATOR_VERSION instead would
# quietly hand every older bundle the previous rules the moment the
# current version moved on.
#
# 0.2.0 took the first accepted quote response; it neither counted
# responses nor checked which request a response named. 0.3.0 added those
# checks ("correlation") but read any truthy acknowledgement flag as a yes
# and judged only the first accepted request. 0.4.0 reads a flag only when
# it is a boolean ("boolean_flags") and judges every accepted request
# ("every_request"). 0.5.0 recognises the injected duplicate only from an
# acknowledgement of that delivery, bound by its fence ("bound_duplicate"),
# and lets only the buyer's terminal acknowledgement, any status but
# retryable, decide `correct` ("terminal_verdict").
EVALUATOR_RULES: dict[str, frozenset[str]] = {
    LEGACY_EVALUATOR_VERSION: frozenset(),
    CORRELATION_EVALUATOR_VERSION: frozenset({"correlation"}),
    "0.4.0": frozenset({"correlation", "boolean_flags", "every_request"}),
    "0.5.0": frozenset({"correlation", "boolean_flags", "every_request",
                        "bound_duplicate", "terminal_verdict"}),
}
EVALUATOR_VERSIONS = tuple(EVALUATOR_RULES)
assert EVALUATOR_VERSION in EVALUATOR_RULES

REQUEST_KIND = "quote_request"
RESPONSE_KIND = "quote_response"
# The quote.read skill: a quote_response carries the request id. The town
# records the body's request_id on message_accepted: verbatim while it is a
# short string, otherwise as a bounded digest (JSON type, JSON text length,
# fingerprint of the full value) under CORRELATION_DIGEST_FIELD.
CORRELATION_FIELD = "request_id"
CORRELATION_DIGEST_FIELD = "request_id_digest"
JSON_TYPES = ("string", "number", "boolean", "null", "array", "object")
# A stage note shows at most this many characters of a recorded value.
NOTE_VALUE_CHARS = 80


def _passed(name: str, evidence: list[str], note: str = "") -> StageResult:
    return StageResult(name=name, status="passed", evidence=evidence, note=note)


def _failed(name: str, evidence: list[str], note: str) -> StageResult:
    return StageResult(name=name, status="failed", evidence=evidence, note=note)


def _missing(name: str, note: str) -> StageResult:
    return StageResult(name=name, status="not_enough_evidence", evidence=[],
                       note=note)


def _response_mismatch(responses: list[TownEvent],
                       requests: list[TownEvent],
                       name_first: bool = False) -> tuple[str, str] | None:
    """Why the accepted quote responses cannot stand as the one answer to
    the accepted request, as (status, note). None when they can, or when
    there is nothing to judge yet (that stays missing).

    Each distinct message identity is accepted once; an idempotent resend
    of the same identity and content is a replay, not a second response.
    """
    request_id = requests[0].subject if requests else None
    if len(responses) > 1 and len(requests) > 1:
        # Not the one exchange the profile expects, but not a seller that
        # answered one request twice either: say what was accepted.
        return "failed", (f"{len(requests)} quote requests and"
                          f" {len(responses)} distinct quote responses were"
                          " accepted; the profile expects exactly one of each"
                          " (an idempotent resend of one identity is not"
                          " counted)")
    if len(responses) > 1:
        return "failed", (f"{len(responses)} distinct quote responses were"
                          " accepted, expected one (an idempotent resend of"
                          " one identity is not counted)")
    if not responses or request_id is None:
        return None
    detail = responses[0].detail
    if CORRELATION_FIELD in detail:
        named = detail[CORRELATION_FIELD]
        if isinstance(named, str) and named == request_id:
            return None
        shape, shown = ("string" if isinstance(named, str) else "other",
                        _show_value(named))
    elif CORRELATION_DIGEST_FIELD in detail:
        digest = detail[CORRELATION_DIGEST_FIELD]
        if (isinstance(digest, dict) and digest.get("type") == "string"
                and digest.get("fingerprint") == fingerprint(request_id)):
            return None
        shape, shown = _show_digest(digest)
    else:
        return "not_enough_evidence", (
            "the quote response carries no request_id, so it is not shown"
            " to answer the accepted request")
    accepted = _show_value(request_id)
    if shape == "string":
        # With several accepted requests, say which one this is measured
        # against: naming it "the accepted request" would read as a claim
        # that the one the response named is not accepted.
        # name_first is the 0.4.0 wording. A 0.3.0 bundle has to replay
        # to the text it recorded, byte for byte, or verify reports a
        # mismatch over a result nobody changed.
        against = ("the first accepted request"
                   if name_first and len(requests) > 1
                   else "the accepted request")
        return "failed", (f"the quote response names request {shown}, not"
                          f" {against} {accepted}")
    if shape == "malformed":
        return "failed", (f"the quote response's recorded request_id digest"
                          f" {shown} is malformed and names no request; the"
                          f" accepted request is {accepted}")
    return "failed", (f"the quote response's request_id is {shown}, which is"
                      " not a string and names no request; the accepted"
                      f" request is {accepted}")


def _names_request(response: TownEvent, request_id: str) -> bool:
    """Whether this accepted response says it answers this request.

    The same correlation the response stage judges, asked of one
    request: verbatim when the town recorded the request_id itself, and
    by fingerprint when it recorded a bounded digest instead.
    """
    detail = response.detail
    if CORRELATION_FIELD in detail:
        named = detail[CORRELATION_FIELD]
        return isinstance(named, str) and named == request_id
    digest = detail.get(CORRELATION_DIGEST_FIELD)
    return (isinstance(digest, dict) and digest.get("type") == "string"
            and digest.get("fingerprint") == fingerprint(request_id))


def _name_requests(ids: list[str]) -> str:
    """Name the requests a stage did not reach, without unbounded text."""
    shown = [_show_value(i) for i in ids[:3]]
    rest = len(ids) - len(shown)
    return ", ".join(shown) + (f" and {rest} more" if rest else "")


def _asserted(note: object, field: str) -> bool | None:
    """The boolean a participant asserted for field, or None for no
    assertion this evaluator can read.

    A flag counts only when it is a boolean. Anything else says nothing
    about the work: read as truthiness, the string "false" and the
    number 1 both mean yes, which is how a participant could deny doing
    something and be recorded as having done it.
    """
    if not isinstance(note, dict):
        return None
    value = note.get(field)
    return value if isinstance(value, bool) else None


def _counted(note: object, field: str) -> int | None:
    """The count a participant reported for field, or None.

    A count is an integer. A string or a container is not a report this
    evaluator can read, and comparing one with >= raises rather than
    answering; a boolean is not a count either, though True would pass
    for one. The same rule as _asserted, for the fields that are counts.
    """
    if not isinstance(note, dict):
        return None
    value = note.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _unreadable_flags(acks: list[TownEvent], field: str) -> list[TownEvent]:
    """Acknowledgements that state field as something other than a
    boolean. Their authors meant to say something; the note says what
    was recorded so an operator can see what to fix."""
    return [a for a in acks
            if isinstance(a.detail.get("note"), dict)
            and field in a.detail["note"]
            and not isinstance(a.detail["note"][field], bool)]


def _flag_note(acks: list[TownEvent], field: str, what: str) -> str:
    shown = _show_value(acks[0].detail["note"][field])
    return (f"{what} records {field} as {shown}, which is not a boolean"
            " and states nothing about the work")


def _show_value(value: object) -> str:
    """A recorded value for a stage note: its JSON text when short (JSON
    null for null), otherwise its first NOTE_VALUE_CHARS characters, its
    length and its fingerprint. Notes stay small whatever was recorded."""
    text = canonical_json(value)
    if not isinstance(value, str):
        text = ("JSON null" if value is None
                else f"a JSON {json_type(value)} {text}")
    if len(text) <= NOTE_VALUE_CHARS:
        return text
    return (f"{text[:NOTE_VALUE_CHARS]}… ({len(canonical_json(value))}"
            f" JSON characters, {fingerprint(value)[:23]}…)")


def _show_digest(digest: object) -> tuple[str, str]:
    """(shape, text) for a recorded request_id digest; shape is string,
    other or malformed."""
    if not (isinstance(digest, dict) and digest.get("type") in JSON_TYPES
            and type(digest.get("json_length")) is int
            and 0 <= digest["json_length"] < 10 ** 15
            and isinstance(digest.get("fingerprint"), str)):
        return "malformed", _show_value(digest)
    if digest["type"] == "null":
        return "other", "JSON null"
    return ("string" if digest["type"] == "string" else "other",
            f"a JSON {digest['type']} of {digest['json_length']} JSON"
            f" characters ({digest['fingerprint'][:23]}…)")


def evaluate(profile: TestProfile, run_id: str, events: list[TownEvent],
             version: str = EVALUATOR_VERSION) -> EvidenceResult:
    if version not in EVALUATOR_RULES:
        raise ValueError(f"unsupported Track evaluator version {version!r}")
    rules = EVALUATOR_RULES[version]
    # Rules without "boolean_flags" read truthiness, and a bundle they
    # recorded still replays that way.
    strict_flags = "boolean_flags" in rules
    # Rules without "every_request" judged only the first accepted request,
    # so a second one that nobody answered did not affect the verdict.
    judges_every_request = "every_request" in rules
    seller = next((n for n, r in profile.roles.items() if r == "seller"), "seller")
    buyer = next((n for n, r in profile.roles.items() if r == "buyer"), "buyer")

    def find(ekind: str, **conds) -> list[TownEvent]:
        out = []
        for e in events:
            if e.kind != ekind:
                continue
            if "observer" in conds and e.observer != conds["observer"]:
                continue
            if "subject" in conds and e.subject != conds["subject"]:
                continue
            ok = True
            for key, val in conds.items():
                if key in ("observer", "subject"):
                    continue
                if e.detail.get(key) != val:
                    ok = False
                    break
            if ok:
                out.append(e)
        return out

    accepted_req = find("message_accepted", kind=REQUEST_KIND)
    request_id = accepted_req[0].subject if accepted_req else None

    stages: list[StageResult] = []

    # accepted: the town committed the request before reporting success.
    if accepted_req:
        stages.append(_passed("accepted", [accepted_req[0].event_id]))
    else:
        stages.append(_missing("accepted", "no accepted quote request"))

    # claimed: a seller claimed the request under a lease.
    claims = find("message_claimed", subject=request_id) if request_id else []
    if claims:
        stages.append(_passed("claimed", [c.event_id for c in claims]))
    else:
        stages.append(_missing("claimed", "the request was never claimed"))

    # received: the seller acknowledged the request through a valid fence.
    seller_acks = (find("ack_recorded", observer=seller, subject=request_id)
                   if request_id else [])
    received = [a for a in seller_acks
                if a.detail.get("status") in ("received", "processed")]
    if received:
        stages.append(_passed("received", [received[0].event_id]))
    else:
        stages.append(_missing("received",
                               "no acknowledged receipt by the seller"))

    # processed: the seller applied the task exactly once on its own side.
    processed = [a for a in seller_acks if a.detail.get("status") == "processed"]
    applied = [a for a in processed
               if (_asserted(a.detail.get("note"), "applied") is True
                   if strict_flags
                   else a.detail.get("note", {}).get("applied"))]
    unreadable = (_unreadable_flags(processed, "applied") if strict_flags
                  else [])
    if not processed:
        stages.append(_missing("processed", "no processed acknowledgement"))
    elif len(applied) > 1:
        stages.append(_failed("processed", [a.event_id for a in applied],
                              f"applied {len(applied)} times, expected once"))
    elif unreadable:
        # Exactly once needs every application claim read, not just one.
        # A readable yes beside an unreadable claim could be one
        # application or two, and passing it would pick the answer the
        # evidence does not give.
        what = ("a processed acknowledgement" if not applied else
                "another processed acknowledgement")
        stages.append(_missing(
            "processed",
            _flag_note(unreadable, "applied", what)
            + ("" if not applied else
               ", so application exactly once is not established")))
    elif len(applied) == 1:
        stages.append(_passed("processed", [applied[0].event_id]))
    else:
        stages.append(_missing("processed",
                               "processed acknowledgements carry no"
                               " application record"))

    # response: the quote response was accepted and reached the buyer.
    accepted_resp = find("message_accepted", kind=RESPONSE_KIND)
    response_id = accepted_resp[0].subject if accepted_resp else None
    buyer_claims = (find("message_claimed", subject=response_id,
                         claimant=buyer) if response_id else [])
    mismatch = (None if "correlation" not in rules
                else _response_mismatch(accepted_resp, accepted_req,
                                        name_first=judges_every_request))
    if mismatch is not None and mismatch[0] == "failed":
        # Several requests are part of why several responses fail.
        cited = (accepted_req if len(accepted_req) > 1
                 and len(accepted_resp) > 1 else [])
        stages.append(_failed("response",
                              [e.event_id for e in cited + accepted_resp],
                              mismatch[1]))
    elif mismatch is not None:
        stages.append(_missing("response", mismatch[1]))
    elif accepted_resp and buyer_claims:
        stages.append(_passed("response", [accepted_resp[0].event_id,
                                           buyer_claims[0].event_id]))
    else:
        stages.append(_missing("response",
                               "no quote response accepted and claimed by"
                               " the buyer"))

    # correct: the buyer's own assertion about the total.
    buyer_acks = (find("ack_recorded", observer=buyer, subject=response_id)
                  if response_id else [])
    if mismatch is not None:
        # The assertion may concern any of the responses in question.
        buyer_acks = [a for r in accepted_resp
                      for a in find("ack_recorded", observer=buyer,
                                    subject=r.subject)]
    provisional_assertions: list[TownEvent] = []
    terminal_verdict = "terminal_verdict" in rules
    if terminal_verdict:
        # A retryable acknowledgement hands the response back to the
        # buyer's inbox: the buyer has not settled, and what it asserted
        # then is provisional. Only an acknowledgement that settles the
        # response, the same line the runner draws, says what the buyer
        # concluded. Earlier rules took the first assertion of any kind,
        # so a provisional "correct" outweighed a terminal "wrong".
        provisional_assertions = [
            a for a in buyer_acks if a.detail.get("status") == "retryable"
            and isinstance(a.detail.get("note"), dict)
            and "correct" in a.detail["note"]]
        buyer_acks = [a for a in buyer_acks
                      if a.detail.get("status") != "retryable"]
    if strict_flags:
        verdict_acks = [a for a in buyer_acks
                        if isinstance(a.detail.get("note"), dict)
                        and "correct" in a.detail["note"]]
    else:
        # Kept exactly as 0.2.0 and 0.3.0 had it, raise included: a
        # historical bundle replays to what those rules did, and a note
        # that is not an object raised there.
        verdict_acks = [a for a in buyer_acks
                        if "correct" in a.detail.get("note", {})]
    unreadable_verdicts = (_unreadable_flags(verdict_acks, "correct")
                           if strict_flags else [])
    if strict_flags:
        verdict_acks = [a for a in verdict_acks
                        if _asserted(a.detail.get("note"), "correct")
                        is not None]
    # Order must not decide. Where a mismatch already fails the stage the
    # assertions cannot rescue it, so only that case is exempt.
    conflicting = (terminal_verdict
                   and (mismatch is None or mismatch[0] != "failed")
                   and len({_asserted(a.detail["note"], "correct")
                            for a in verdict_acks}) > 1)
    if not verdict_acks and unreadable_verdicts:
        stages.append(_missing(
            "correct",
            _flag_note(unreadable_verdicts, "correct",
                       "the buyer's acknowledgement")))
    elif conflicting:
        # The coordinator settles a response at its first terminal
        # acknowledgement, so only a crafted record carries two that
        # disagree. Picking one would be choosing the answer.
        stages.append(_missing(
            "correct", "the buyer's terminal acknowledgements disagree about"
                       " whether the total is correct"))
    elif verdict_acks and mismatch is not None and mismatch[0] == "failed":
        stages.append(_failed(
            "correct", [a.event_id for a in verdict_acks],
            "the buyer's assertion cannot establish the answer to the"
            f" accepted request: {mismatch[1]}"))
    elif (verdict_acks and mismatch is not None
          and verdict_acks[0].detail["note"]["correct"]):
        stages.append(_missing(
            "correct", "the buyer's assertion concerns a response not shown"
                       f" to answer the accepted request: {mismatch[1]}"))
    elif verdict_acks:
        note = verdict_acks[0].detail["note"]
        if note["correct"]:
            stages.append(_passed("correct", [verdict_acks[0].event_id]))
        else:
            stages.append(_failed(
                "correct", [verdict_acks[0].event_id],
                f"buyer observed total {note.get('total_cents')} against"
                f" expected {profile.task.expected_total_cents}"))
    elif provisional_assertions and buyer_acks:
        stages.append(_missing(
            "correct", "the buyer's terminal acknowledgement asserts nothing"
                       " about correctness; its earlier provisional assertion"
                       " does not decide"))
    elif provisional_assertions:
        stages.append(_missing(
            "correct", "the buyer's only correctness assertion was"
                       " provisional (acknowledged retryable), and it never"
                       " settled the response"))
    else:
        stages.append(_missing("correct", "the buyer made no correctness"
                                          " assertion"))

    # Fault checks apply only when the profile names the fault.
    fault = profile.fault
    if fault == "crash_after_claim":
        ended_early = (find("claim_expired", subject=request_id)
                       + find("stale_fence_rejected", subject=request_id)
                       if request_id else [])
        reclaimed = [c for c in claims if c.detail.get("attempt", 1) >= 2]
        restarts = find("participant_restarted")
        if ended_early and reclaimed:
            evidence = ([e.event_id for e in ended_early]
                        + [reclaimed[0].event_id]
                        + [r.event_id for r in restarts])
            stages.append(_passed("recovered_after_restart", evidence))
        else:
            stages.append(_missing("recovered_after_restart",
                                   "no lease end followed by redelivery"))
        fences = (find("stale_fence_rejected", subject=request_id)
                  if request_id else [])
        if fences:
            stages.append(_passed("stale_fence_rejected",
                                  [f.event_id for f in fences]))
        else:
            stages.append(_missing("stale_fence_rejected",
                                   "no stale fence was rejected"))
    elif fault == "duplicate_delivery":
        offered = find("duplicate_offered")
        recognized = [a for a in seller_acks
                      if (_asserted(a.detail.get("note"), "duplicate") is True
                          if strict_flags
                          else a.detail.get("note", {}).get("duplicate"))]
        missing_note = "no duplicate offer recognized exactly once"
        if "bound_duplicate" in rules:
            # A lease lost before the offer brings a redelivery the seller
            # also acknowledges as a duplicate, often with the application
            # it performed. That is not the injected delivery, so only an
            # acknowledgement under an offer's own fence recognises it. It
            # must also settle the offer as processed: the runner counts
            # nothing less as the duplicate handled, the protocol asks for
            # it, and a retryable acknowledgement is provisional.
            by_fence = {o.detail.get("fence"): o for o in offered
                        if o.subject == request_id
                        and isinstance(o.detail.get("fence"), str)}

            def bound(ack: TownEvent) -> bool:
                fence = ack.detail.get("fence")
                return (isinstance(fence, str) and fence in by_fence
                        and ack.detail.get("status") == "processed")

            unbound = [a for a in recognized if not bound(a)]
            recognized = [a for a in recognized if bound(a)]
            offered = ([by_fence[recognized[0].detail["fence"]]]
                       if recognized else list(by_fence.values()))
            if not by_fence:
                missing_note = ("the town never offered the injected"
                                " duplicate delivery")
            elif unbound and not recognized:
                missing_note = ("the injected duplicate delivery was never"
                                " acknowledged as processed; the"
                                " acknowledgement marked duplicate answered"
                                " another delivery, or did not settle the"
                                " offer")
        # Recognising a duplicate means the one application was not
        # repeated, which an unreadable application claim leaves open.
        if offered and recognized and len(applied) == 1 and not unreadable:
            stages.append(_passed("duplicate_recognized",
                                  [offered[0].event_id,
                                   recognized[0].event_id]))
        else:
            stages.append(_missing("duplicate_recognized", missing_note))
    elif fault == "drop_wakeup":
        suppressed = find("notify_suppressed")
        if suppressed and claims:
            stages.append(_passed("wakeup_loss_tolerated",
                                  [suppressed[0].event_id,
                                   claims[0].event_id]))
        else:
            stages.append(_missing("wakeup_loss_tolerated",
                                   "no suppressed wake-up followed by a"
                                   " claim"))
    elif fault == "lost_ack":
        dropped = find("ack_dropped")
        if dropped and processed:
            stages.append(_passed("ack_retry_survived",
                                  [dropped[0].event_id,
                                   processed[0].event_id]))
        else:
            stages.append(_missing("ack_retry_survived",
                                   "no dropped acknowledgement followed by"
                                   " a recorded retry"))
    elif fault == "tool_error":
        errored = [e for e in events if e.kind == "ack_recorded"
                   and ((_counted(e.detail.get("note"), "tool_errors") or 0) >= 1
                        if strict_flags
                        else e.detail.get("note", {})
                        .get("tool_errors", 0) >= 1)]
        if errored and processed:
            stages.append(_passed(
                "tool_error_survived",
                [e.event_id for e in errored[:4]],
                "a lost tool result was noticed, retried, and the task"
                " still completed"))
        else:
            stages.append(_missing(
                "tool_error_survived",
                "no participant reported recovering from a lost tool"
                " result"))
    elif fault == "context_truncation":
        truncated = [e for e in events if e.kind == "ack_recorded"
                     and ((_counted(e.detail.get("note"),
                                    "context_truncations") or 0) >= 1
                          if strict_flags
                          else e.detail.get("note", {})
                          .get("context_truncations", 0) >= 1)]
        if truncated and processed:
            stages.append(_passed(
                "truncation_survived",
                [e.event_id for e in truncated[:4]],
                "the agents reported losing context and still completed"))
        else:
            stages.append(_missing(
                "truncation_survived",
                "no participant reported a context truncation"))

    # Every accepted request, not only the first. A buyer that asked twice
    # and was answered once has not had its exchange completed, and saying
    # so is the difference between reporting on the run and reporting on
    # the part of it that went well. The stages above already judged the
    # first request; these downgrade one that passed there while another
    # accepted request never reached it. A stage that already found
    # something more specific keeps that finding.
    if judges_every_request and len(accepted_req) > 1:
        by_stage: dict[str, list[str]] = {
            "claimed": [], "received": [], "processed": [], "response": []}
        misaddressed: list[tuple[str, object]] = []
        for event in accepted_req:
            other = event.subject
            answered = [r for r in accepted_resp if _names_request(r, other)]
            recipient = event.detail.get("to")
            if recipient not in (None, seller):
                # Not the seller's to handle, so its handling stages say
                # nothing about it. It is still a request this run accepted
                # and nobody answered, and a verdict that passes over it is
                # a verdict about part of the run.
                if not answered:
                    misaddressed.append((other, recipient))
                continue
            acks = find("ack_recorded", observer=seller, subject=other)
            if not find("message_claimed", subject=other):
                by_stage["claimed"].append(other)
            if not [a for a in acks
                    if a.detail.get("status") in ("received", "processed")]:
                by_stage["received"].append(other)
            if not [a for a in acks
                    if a.detail.get("status") == "processed"
                    and _asserted(a.detail.get("note"), "applied") is True]:
                by_stage["processed"].append(other)
            if not answered:
                by_stage["response"].append(other)
        unfinished = {
            "claimed": "was never claimed",
            "received": "was never acknowledged by the seller",
            "processed": "has no application record",
            "response": "was never answered",
        }
        notes: dict[str, list[str]] = {}
        for name, names in by_stage.items():
            if names:
                notes.setdefault(name, []).append(
                    f"accepted request {_name_requests(names)}"
                    f" {unfinished[name]}")
        if misaddressed:
            shown = "; ".join(
                f"{_show_value(rid)} to {_show_value(to)}"
                for rid, to in misaddressed[:3])
            rest = len(misaddressed) - min(len(misaddressed), 3)
            notes.setdefault("response", []).append(
                f"accepted request {shown}"
                + (f" and {rest} more" if rest else "")
                + " was addressed to someone other than the seller and"
                  " never answered")
        for index, existing in enumerate(stages):
            said = notes.get(existing.name)
            if not said or existing.status != "passed":
                continue
            stages[index] = _missing(existing.name, "; ".join(said))

    verified = find("portable_identity_verified")
    if verified:
        agent_ids = sorted({e.detail.get("agent_id", "?")
                            for e in verified})
        stages.append(StageResult(
            name="portable_identity", status="passed",
            evidence=[e.event_id for e in verified[:4]],
            note="run grants verified against pinned controller keys"
                 f" for {', '.join(agent_ids)}"))
    else:
        stages.append(StageResult(
            name="portable_identity", status="not_tested",
            note="this run used short-lived join tokens; rerun with"
                 " --identity for grant-based portable identity"))

    cascade_unreached(stages)
    return EvidenceResult(run_id=run_id, evaluator_version=version,
                          stages=stages, verdict=stage_verdict(stages),
                          evaluated_at=time.time())


def cascade_unreached(stages: list[StageResult]) -> None:
    """After the first failed or errored stage, an inconclusive later
    stage was simply never reached: report it not_tested, never let a
    broken boundary masquerade as many independent inconclusives."""
    broken = False
    for stage in stages:
        if broken and stage.status == "not_enough_evidence":
            stage.status = "not_tested"
            stage.note = ("not reached: an earlier boundary broke"
                          + (f" ({stage.note})" if stage.note else ""))
        if stage.status in ("failed", "error"):
            broken = True


def stage_verdict(stages: list[StageResult]) -> str:
    """ERROR means the town malfunctioned; it outranks blaming the
    subject. Missing evidence never becomes a pass."""
    applicable = [s for s in stages if s.status != "not_tested"]
    if any(s.status == "error" for s in applicable):
        return "error"
    if any(s.status == "failed" for s in applicable):
        return "failed"
    if applicable and all(s.status == "passed" for s in applicable):
        return "passed"
    return "incomplete"
