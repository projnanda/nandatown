"""Path A: test one already-running external agent against an exact
NANDA journey, and name the first boundary that broke.

The agent is already running; the developer migrates nothing, uploads
nothing, and supplies no model key. Town acts as a deterministic
counterpart and observer. Every observation is recorded as an
attributed event; the evaluator derives the stage results purely from
those events, so the bundle replays and verifies like every other run.

Result semantics are honest by construction: a broken boundary makes
later stages not tested rather than a pile of inconclusives, and a
malfunction in Town's own driver is an ERROR attributed to Town, never
to the subject.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import sys
import time
import uuid
from contextlib import ExitStack
from typing import Any

import httpx

from . import __version__
from .bundle import attest_bundle, write_bundle
from .evaluator import cascade_unreached, stage_verdict
from .a2a_transport import (
    DEFAULT_MAX_RESPONSE_BYTES, a2a_client, effective_policy,
    validate_response_budget,
)
from .path_profiles import (
    DEFAULT_PATH_PROFILE, PATH_EVALUATOR, QUOTE_INTENT_EVALUATOR,
    QUOTE_INTENT_FIELDS, STRICT_PATH_EVALUATOR,
    STRICT_QUOTE_INTENT_EVALUATOR, PathProfile, get_path_profile,
)
from .records import (
    EvidenceResult,
    RunRecord,
    StageResult,
    TownEvent,
    canonical_json,
    fingerprint,
)
from .url_credentials import Scrubber, has_credentials, scrub

PATH_EVALUATOR_VERSION = "path-0.2"
STRICT_PATH_EVALUATOR_VERSION = "path-0.3"
STRICT_QUOTE_INTENT_EVALUATOR_VERSION = "path-quote-intent-0.2"

STRICT_PATH_EVALUATORS = {
    STRICT_PATH_EVALUATOR,
    STRICT_QUOTE_INTENT_EVALUATOR,
}
LEGACY_PATH_EVALUATORS = {
    PATH_EVALUATOR,
    QUOTE_INTENT_EVALUATOR,
}
QUOTE_INTENT_EVALUATORS = {
    QUOTE_INTENT_EVALUATOR,
    STRICT_QUOTE_INTENT_EVALUATOR,
}


def path_evaluator_version(profile: PathProfile) -> str:
    if profile.evaluator == STRICT_PATH_EVALUATOR:
        return STRICT_PATH_EVALUATOR_VERSION
    if profile.evaluator == STRICT_QUOTE_INTENT_EVALUATOR:
        return STRICT_QUOTE_INTENT_EVALUATOR_VERSION
    if profile.evaluator == QUOTE_INTENT_EVALUATOR:
        return "path-quote-intent-0.1"
    if profile.evaluator == PATH_EVALUATOR:
        return PATH_EVALUATOR_VERSION
    raise ValueError(f"unsupported path evaluator {profile.evaluator!r}")


def _strict_path_semantics(profile: PathProfile) -> bool:
    if profile.evaluator in STRICT_PATH_EVALUATORS:
        return True
    if profile.evaluator in LEGACY_PATH_EVALUATORS:
        return False
    raise ValueError(f"unsupported path evaluator {profile.evaluator!r}")


def _quote_intent_semantics(profile: PathProfile) -> bool:
    return profile.evaluator in QUOTE_INTENT_EVALUATORS


# Subject values echoed into an event detail (card name and version, task
# id, kind and state, fulfillment fields) sit at most three containers deep
# in an events.jsonl line (event, detail, quote). pydantic-core refuses to
# write a line nested past about 255 levels and to read one back past about
# 200, so a deeper echo left a partial bundle. 64 is far beyond any
# meaningful field and keeps every line well inside both limits.
MAX_ECHOED_NESTING = 64


def _nesting_exceeds(value: Any, limit: int) -> bool:
    """Whether value nests containers deeper than limit, without recursing."""
    containers = (dict, list)
    stack = [(value, 1)] if isinstance(value, containers) else []
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return True
        children = item.values() if isinstance(item, dict) else item
        stack.extend((child, depth + 1) for child in children
                     if isinstance(child, containers))
    return False


def _utf8_encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _unwritable(value: Any) -> str | None:
    """Why a value within the nesting bound cannot be written verbatim.

    Python's json decodes NaN, Infinity and -Infinity, which pydantic
    writes as null, so replay would judge a different value than the run
    did; and it cannot write a string or key holding a lone surrogate at
    all. An unencodable string outranks a non-finite number, as it leaves
    nothing to digest. Walks without recursing.
    """
    non_finite = False
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str) and not _utf8_encodable(key):
                    return "unencodable string"
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            if not _utf8_encodable(item):
                return "unencodable string"
        elif isinstance(item, float) and not math.isfinite(item):
            non_finite = True
    return "non-finite number" if non_finite else None


def _json_type(value: Any) -> str:
    # Markers are only made for containers, strings and floats.
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "string" if isinstance(value, str) else "number"


def _echoed(value: Any) -> Any:
    """A subject value as recorded in evidence: verbatim when writable.

    A value nested deeper than the bound, or holding a lone surrogate or a
    non-finite number, becomes a marker instead of crashing the bundle
    write or being rewritten by it. Like the value, the marker is truthy
    and never equals anything a stage expects (a task kind, a terminal
    state, a quote term), so the recorded evidence evaluates as the value
    would have. The card digest and the fulfillment's content_digest still
    cover the full value.
    """
    if _nesting_exceeds(value, MAX_ECHOED_NESTING):
        reason = "nesting too deep"
    else:
        reason = _unwritable(value)
        if reason is None:
            return value
    marker = {"unrecorded": reason, "type": _json_type(value)}
    if reason == "unencodable string":
        # No UTF-8 encoding exists to measure or digest.
        return marker
    try:
        # Canonical JSON spells non-finite numbers NaN, Infinity and
        # -Infinity, as in the card digest and content_digest.
        marker["json_length"] = len(canonical_json(value))
        marker["fingerprint"] = fingerprint(value)
    except (RecursionError, UnicodeEncodeError):
        # Decoded just under the interpreter limit, re-serializing can need
        # one frame more, and a lone surrogate nested past the bound has no
        # UTF-8 encoding. The output is still the subject's, not a Town
        # error: keep the marker without its digest. Frame use differs
        # between Python versions, so for the same input the digest may be
        # present on one and absent on another; replay reads the recorded
        # marker either way.
        marker.pop("json_length", None)
    return marker


def _quote_intent_errors(profile: PathProfile, detail: dict[str, Any]) -> list[str]:
    """Compare observed quote terms, never filling omissions from the request."""
    errors = []
    observed = detail.get("quote")
    if not isinstance(observed, dict):
        observed = {}
    expected = profile.expected["quote"]
    for field in QUOTE_INTENT_FIELDS:
        value = observed.get(field)
        wanted = expected[field]
        # JSON booleans and floats are not integer item counts.
        if type(value) is not type(wanted) or value != wanted:
            errors.append(f"{field}: expected {wanted!r}, observed {value!r}")
    total = detail.get("total_cents")
    maximum = profile.expected["max_total_cents"]
    if type(total) is not int or not 0 <= total <= maximum:
        errors.append(f"total_cents: expected integer in [0, {maximum}],"
                      f" observed {total!r}")
    return errors


def _semantic_fulfillment_stage(
        profile: PathProfile, fulfillment: TownEvent) -> StageResult:
    detail = fulfillment.detail
    observed_total = detail.get("total_cents")
    expected_request_id = fulfillment.subject
    observed_request_id = detail.get("request_id")
    request_ok = (isinstance(expected_request_id, str)
                  and bool(expected_request_id)
                  and isinstance(observed_request_id, str)
                  and observed_request_id == expected_request_id)
    quote_intent = _quote_intent_semantics(profile)
    errors = _quote_intent_errors(profile, detail) if quote_intent else []
    expected_total = profile.expected.get("total_cents")
    result_ok = not errors if quote_intent else observed_total == expected_total
    if result_ok and request_ok:
        return StageResult(
            name="semantic_result", status="passed",
            evidence=[fulfillment.event_id],
            note=(f"observed quote matches the item terms and budget;"
                  f" total_cents {observed_total}; no purchase or delivery"
                  " tested"
                  if quote_intent else
                  f"exactly one fulfillment, total {observed_total}"))
    note = ("quote does not match the selected profile: " + "; ".join(errors)
            if quote_intent else
            f"protocol passed but the result is wrong:"
            f" expected total {expected_total}, observed {observed_total}")
    if not request_ok:
        note += (f"; expected request_id {expected_request_id!r},"
                 f" observed request_id {observed_request_id!r}")
    return StageResult(
        name="semantic_result", status="failed",
        evidence=[fulfillment.event_id], note=note)


STAGE_ORDER = ["resolution", "agent_card_retrieval",
               "descriptor_consistency", "protocol_invocation",
               "semantic_result", "duplicate_request"]


class _Recorder:
    """Every observation and intent of a Path run, as recorded.

    Recording is where URL credentials are labelled, so nothing that
    reaches the bundle can carry them: not the subject, not a resolution
    hop, and not an error message that quotes the URL. Only the locators
    the operator supplied are registered, and only their exact credentials
    are replaced, so what an agent itself says is recorded as it said it.
    Evaluation reads these records too, so it judges exactly what a replay
    will.
    """

    def __init__(self, run_id: str, scrubber: Scrubber | None = None):
        self.run_id = run_id
        self.events: list[TownEvent] = []
        self.intents: list[dict[str, Any]] = []
        self.scrubber = scrubber or Scrubber()

    def withhold_credentials_of(self, url: object) -> None:
        self.scrubber.register(url)

    def emit(self, observer: str, kind: str, subject: str,
             detail: dict[str, Any] | None = None) -> None:
        self.events.append(TownEvent(
            event_id=f"ev-{len(self.events) + 1}", run_id=self.run_id,
            at=time.time(), observer=observer, kind=kind,
            subject=scrub(subject, self.scrubber),
            detail=scrub(detail or {}, self.scrubber)))

    def intend(self, actor: str, action: str,
               payload: dict[str, Any]) -> None:
        self.intents.append({
            "intent_id": f"in-{len(self.intents) + 1}",
            "run_id": self.run_id, "at": time.time(), "actor": actor,
            "action": action, "payload": scrub(payload, self.scrubber)})


def _is_endpoint_url(value: object) -> bool:
    """Whether httpx parses value as an absolute http(s) URL with a host.

    Any host and any TCP port (1-65535, or none for the scheme default) is
    accepted: loopback, LAN and remote agents are all valid subjects. httpx
    parses a port outside that range, which then fails only on connect.
    Raw whitespace is refused because httpx would drop it or percent-encode
    it into a different endpoint.

    Parsing a URL is not the same as being able to read its parts. httpx
    decodes a punycode hostname only when the host is asked for, and a
    malformed A-label such as ``xn--`` raises there instead. So the parts
    are read inside the guard, and a hostname that will not decode makes
    the URL unusable rather than a crash. httpx decodes only a hostname
    that begins with ``xn--``, so ``localhost.xn--a`` is still accepted
    here and fails later, as a subject that cannot be reached.
    """
    if not isinstance(value, str) or any(ch.isspace() for ch in value):
        return False
    try:
        parsed = httpx.URL(value)
        return (parsed.scheme in ("http", "https") and bool(parsed.host)
                and (parsed.port is None or 1 <= parsed.port <= 65535))
    except (httpx.InvalidURL, UnicodeError):
        return False


def _subject_label(subject_url: str | None,
                   agent_name: str | None) -> str | None:
    """The locator that names the subject in the run record and events.

    This is ``subject_url or agent_name`` with a whitespace-only locator
    counted as empty and a non-string one as absent: verify rejects a blank
    or non-string participant name, receipts a blank subject, and events
    need a string subject. Resolution refuses a blank or non-string URL and
    agent name, even one an index lists, so a run that records its subject
    as "?" never gets past resolution.
    """
    def usable(locator: object) -> str | None:
        if not isinstance(locator, str):
            return None
        if not locator.strip():
            return ""
        return locator

    return usable(subject_url) or usable(agent_name)


def _resolve(recorder: _Recorder, url: str | None, index_file: str | None,
             agent_name: str | None) -> tuple[str | None, str | None]:
    """Returns (subject_url, pinned_card_digest_from_index).

    A locator Town cannot use fails resolution here, so it is never charged
    to the agent's card retrieval.
    """
    if index_file:
        recorder.intend("town-requester", "resolve",
                        {"index": index_file, "agent": agent_name})
        subject = _subject_label(None, agent_name) or "?"

        def fail(reason: str) -> tuple[None, None]:
            recorder.emit("town-requester", "resolution_failed", subject,
                          {"reason": reason})
            return None, None

        if not isinstance(agent_name, str) or not agent_name.strip():
            # An index may list a blank name, which would let a passing
            # run and its receipt leave the subject unnamed.
            return fail("blank agent name: expected a non-blank name to"
                        " look up in the pinned index")
        try:
            # JSON is UTF-8 whatever the locale, so a non-ASCII agent name
            # resolves the same on every machine.
            with open(index_file, encoding="utf-8") as f:
                index = json.load(f)
        except (OSError, ValueError, RecursionError) as exc:
            # Includes invalid JSON, bytes that are not UTF-8 and nesting
            # past the recursion limit.
            return fail(f"index unreadable: {exc}")
        # The index is operator-supplied fixture JSON: check its shape
        # before trusting it, and name the problem without echoing values.
        if not isinstance(index, dict):
            return fail("malformed index: top level must be a JSON object")
        agents = index.get("agents")
        if agents is not None and not isinstance(agents, dict):
            return fail('malformed index: "agents" must be a JSON object')
        entry = (agents or {}).get(agent_name or "")
        if entry is not None and not isinstance(entry, dict):
            return fail("malformed index: the entry for this agent must be"
                        " a JSON object")
        if not entry or "url" not in entry:
            return fail("missing card pointer: the pinned index has no"
                        " entry for this agent")
        if not isinstance(entry["url"], str) or not entry["url"]:
            return fail('malformed index: the entry "url" must be a'
                        " non-empty string")
        if not _is_endpoint_url(entry["url"]):
            return fail('malformed index: the entry "url" must be an'
                        " absolute http(s) URL")
        digest = entry.get("card_digest")
        if digest is not None and (not isinstance(digest, str)
                                   or not digest):
            # An empty pin would silently leave descriptor consistency
            # untested instead of checking it.
            return fail('malformed index: the entry "card_digest" must be'
                        " a non-empty string")
        # The entry's credentials, if any, came from the operator's index
        # and are withheld from everything recorded from here on.
        recorder.withhold_credentials_of(entry["url"])
        recorder.emit("town-requester", "resolution_hop", subject,
                      {"kind": "pinned-index", "index": index_file,
                       "url": entry["url"]})
        return entry["url"], entry.get("card_digest")
    recorder.intend("town-requester", "resolve", {"url": url})
    subject = _subject_label(url, None) or "?"
    if not _is_endpoint_url(url):
        recorder.emit("town-requester", "resolution_failed", subject,
                      {"reason": "invalid endpoint URL: expected an"
                                 " absolute http(s) URL"})
        return None, None
    recorder.emit("town-requester", "resolution_hop", subject,
                  {"kind": "direct", "url": url})
    return url, None


def run_path_test(subject_url: str | None, out_dir: str,
                  profile_ref: str | None = None,
                  pin_card_digest: str | None = None,
                  index_file: str | None = None,
                  agent_name: str | None = None,
                  http: httpx.Client | None = None
                  ) -> tuple[str, EvidenceResult]:
    from .a2a_adapter import (
        artifact_text, artifact_texts, fetch_card, send_message,
    )

    if subject_url and index_file:
        # The index entry chooses the endpoint; the evidence must not name
        # a different URL as the subject.
        raise ValueError("give either a subject URL or an index file, not"
                         " both")
    if index_file and not agent_name:
        raise ValueError("an index file needs an agent name to choose its"
                         " entry")
    profile = get_path_profile(profile_ref or DEFAULT_PATH_PROFILE)
    strict_semantics = _strict_path_semantics(profile)
    run_id = "path-" + uuid.uuid4().hex[:12]
    nonce = uuid.uuid4().hex[:10]
    recorder = _Recorder(run_id)
    recorder.withhold_credentials_of(subject_url)
    timeout = profile.limits.get("timeout_seconds", 15.0)
    response_budget = validate_response_budget(profile.limits.get(
        "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES))

    url, index_digest = _resolve(recorder, subject_url, index_file,
                                 agent_name)
    pinned = pin_card_digest or index_digest

    with ExitStack() as clients:
        card_ok = False
        descriptor_mismatch = False
        if url is not None:
            recorder.intend("town-requester", "fetch_card", {"url": url})
            try:
                client = clients.enter_context(a2a_client(url, http, timeout))
                card = fetch_card(url, http=client,
                                  max_response_bytes=response_budget,
                                  timeout_seconds=timeout)
                observed_digest = fingerprint(card)
                recorder.emit("town-requester", "card_retrieved", url,
                              {"digest": observed_digest,
                               "name": _echoed(card.get("name")),
                               "version": _echoed(card.get("version"))})
                card_ok = True
                if pinned:
                    recorder.emit("town-requester", "descriptor_expected",
                                  url, {"digest": pinned})
                    descriptor_mismatch = pinned != observed_digest
            except (ValueError, httpx.HTTPError) as exc:
                recorder.emit("town-requester", "card_fetch_failed", url,
                              {"reason": str(exc)})

        if card_ok and not descriptor_mismatch:
            order_id = f"order-{nonce}"
            request_body = dict(profile.request, request_id=order_id,
                                nonce=nonce)
            for attempt in (1, 2):
                if attempt == 2 and profile.controlled_condition \
                        != "duplicate_request":
                    break
                recorder.intend("town-requester", "message_send",
                                {"attempt": attempt, "body": request_body})
                try:
                    task = send_message(url, json.dumps(request_body),
                                        http=client,
                                        max_response_bytes=response_budget,
                                        timeout_seconds=timeout)
                    status = task.get("status", {})
                    if strict_semantics:
                        state = (status.get("state")
                                 if isinstance(status, dict) else None)
                    else:
                        state = status.get("state")
                    exchange_detail = {
                        "attempt": attempt,
                        "ok": True,
                        "task_id": _echoed(task.get("id")),
                        "kind": _echoed(task.get("kind")),
                        "state": _echoed(state),
                    }
                    terminal_outputs = None
                    if strict_semantics:
                        terminal_outputs = artifact_texts(task)
                        exchange_detail["terminal_output_count"] = len(
                            terminal_outputs)
                    recorder.emit("town-requester", "protocol_exchange",
                                  order_id, exchange_detail)
                    if strict_semantics:
                        if task.get("kind") != "task" or state != "completed":
                            break
                        expected_outputs = profile.expected.get(
                            "terminal_fulfillments", 1)
                        if len(terminal_outputs) != expected_outputs:
                            recorder.emit(
                                "town-requester", "fulfillment_unparseable",
                                order_id,
                                {"attempt": attempt,
                                 "terminal_output_count": len(
                                     terminal_outputs),
                                 "reason":
                                     "expected exactly one terminal text"
                                     " output, observed"
                                     f" {len(terminal_outputs)}"})
                            break
                        text = terminal_outputs[0]
                    else:
                        text = artifact_text(task)
                    try:
                        fulfillment = json.loads(text)
                        content_digest = (fingerprint(fulfillment)
                                          if isinstance(fulfillment, dict)
                                          else None)
                    except (ValueError, TypeError, RecursionError) as exc:
                        # Output that cannot be decoded or digested (past
                        # the recursion limit, oversized integers, lone
                        # surrogates) is the subject's, never a Town error.
                        # Only decoding is guarded, so a Town recording
                        # fault is not blamed on the subject. Legacy
                        # profiles keep their recorded behavior.
                        if not strict_semantics and not isinstance(
                                exc, json.JSONDecodeError):
                            raise
                        # Withheld before the preview is cut: a cut can
                        # leave credentials without the "@" that ends them.
                        shown = scrub(text, recorder.scrubber)
                        preview = (shown[:200] if isinstance(shown, str)
                                   else repr(shown)[:200])
                        recorder.emit("town-requester",
                                      "fulfillment_unparseable", order_id,
                                      {"attempt": attempt,
                                       "text": _echoed(preview)})
                        if strict_semantics:
                            break
                        continue
                    if not isinstance(fulfillment, dict):
                        recorder.emit("town-requester", "fulfillment_unparseable",
                                      order_id, {"attempt": attempt,
                                                 "reason": "quote is not a JSON object"})
                        if strict_semantics:
                            break
                        continue
                    detail = {
                        "attempt": attempt,
                        "total_cents": _echoed(fulfillment.get("total_cents")),
                        "request_id": _echoed(fulfillment.get("request_id")),
                        "content_digest": content_digest}
                    if _quote_intent_semantics(profile):
                        detail["quote"] = {
                            field: _echoed(fulfillment[field])
                            for field in QUOTE_INTENT_FIELDS
                            if field in fulfillment}
                    recorder.emit(
                        "town-requester", "fulfillment_observed",
                        order_id, detail)
                except (ValueError, httpx.HTTPError) as exc:
                    recorder.emit("town-requester", "protocol_exchange",
                                  order_id,
                                  {"attempt": attempt, "ok": False,
                                   "reason": str(exc)})
                    break
                except Exception as exc:
                    # Town's own driver misbehaved. That is Town's fault
                    # and must never read as a subject failure.
                    recorder.emit("town", "town_driver_error", order_id,
                                  {"attempt": attempt,
                                   "reason": f"{type(exc).__name__}: {exc}"})
                    break

    result = evaluate_path(profile, run_id, recorder.events)

    # Quoted per argument: a shell must not split a URL at "&" or a path at
    # a space and silently rerun a different subject or profile.
    rerun_inputs: dict[str, str] = {}
    rerun_argv = ["nandatown", "test-agent"]
    if index_file:
        rerun_argv += ["--index", index_file, "--agent-name", str(agent_name)]
    else:
        if has_credentials(subject_url):
            # The credentials are not recorded, so the rerun cannot contain
            # them; it names what the operator has to supply, as a Track
            # rerun does for a command it did not record.
            rerun_argv += ["--url", "<operator-supplied-url>"]
            rerun_inputs["url"] = (
                "the endpoint URL with its credentials (credentials not"
                " recorded; the endpoint was"
                f" {recorder.scrubber.labeller.label(subject_url)})")
        else:
            rerun_argv += ["--url", str(subject_url)]
    rerun_argv += ["--path-profile", profile.ref]
    if pin_card_digest:
        rerun_argv += ["--pin-card-digest", pin_card_digest]
    rerun = shlex.join(rerun_argv)

    run_record = RunRecord(
        run_id=run_id,
        profile_name=profile.ref,
        profile_fingerprint=profile.fingerprint(),
        created_at=time.time(),
        participants=[
            {"name": "town-requester", "role": "requester"},
            {"name": scrub(_subject_label(subject_url, agent_name) or "?",
                           recorder.scrubber),
             "role": "subject"},
        ],
        releases={"nandatown": __version__,
                  "evaluator": path_evaluator_version(profile),
                  "python": sys.version.split()[0]},
        config={"mode": "path",
                "subject": scrub(_subject_label(subject_url, agent_name),
                                 recorder.scrubber),
                "profile": profile.ref,
                "pinned_card_digest": pinned,
                "nonce": nonce,
                "a2a_transport_policy": effective_policy(
                    response_budget, timeout, injected=http is not None,
                    profile_budget="max_response_bytes" in profile.limits),
                "rerun_command": rerun,
                **({"rerun_required_inputs": rerun_inputs}
                   if rerun_inputs else {})},
    )
    bundle_dir = os.path.join(out_dir, run_id)
    write_bundle(bundle_dir, profile, run_record, recorder.intents,
                 recorder.events, result, mode="path")
    attest_bundle(bundle_dir)
    return bundle_dir, result


def evaluate_path(profile: PathProfile, run_id: str,
                  events: list[TownEvent]) -> EvidenceResult:
    """Stage results derived purely from the recorded observations, so
    any holder of the bundle can replay this judgment."""

    strict_semantics = _strict_path_semantics(profile)

    def find(kind: str, **conds) -> list[TownEvent]:
        out = []
        for e in events:
            if e.kind != kind:
                continue
            if all(e.detail.get(k) == v for k, v in conds.items()):
                out.append(e)
        return out

    stages: list[StageResult] = []

    hops = find("resolution_hop")
    failed_resolution = find("resolution_failed")
    if hops:
        stages.append(StageResult(name="resolution", status="passed",
                                  evidence=[hops[0].event_id]))
    elif failed_resolution:
        stages.append(StageResult(
            name="resolution", status="failed",
            evidence=[failed_resolution[0].event_id],
            note=failed_resolution[0].detail.get("reason", "")))
    else:
        stages.append(StageResult(name="resolution",
                                  status="not_enough_evidence",
                                  note="no resolution was attempted"))

    cards = find("card_retrieved")
    card_failures = find("card_fetch_failed")
    if cards:
        stages.append(StageResult(
            name="agent_card_retrieval", status="passed",
            evidence=[cards[0].event_id],
            note=f"observed card {cards[0].detail['digest'][:23]}"))
    elif card_failures:
        stages.append(StageResult(
            name="agent_card_retrieval", status="failed",
            evidence=[card_failures[0].event_id],
            note=card_failures[0].detail.get("reason", "")))
    else:
        stages.append(StageResult(name="agent_card_retrieval",
                                  status="not_enough_evidence",
                                  note="retrieval was never reached"))

    expected_descriptor = find("descriptor_expected")
    if expected_descriptor and cards:
        expected_digest = expected_descriptor[0].detail["digest"]
        observed_digest = cards[0].detail["digest"]
        if expected_digest == observed_digest:
            stages.append(StageResult(
                name="descriptor_consistency", status="passed",
                evidence=[expected_descriptor[0].event_id,
                          cards[0].event_id]))
        else:
            stages.append(StageResult(
                name="descriptor_consistency", status="failed",
                evidence=[expected_descriptor[0].event_id,
                          cards[0].event_id],
                note=f"expected card {expected_digest[:23]}, observed"
                     f" {observed_digest[:23]}; republish or align the"
                     " runtime AgentCard"))
    elif cards:
        stages.append(StageResult(
            name="descriptor_consistency", status="not_tested",
            note="no pinned digest to compare; observed card digest is"
                 " in the evidence"))
    else:
        stages.append(StageResult(name="descriptor_consistency",
                                  status="not_enough_evidence",
                                  note="no card to compare"))

    driver_errors = find("town_driver_error")
    first_exchange = find("protocol_exchange", attempt=1)
    if driver_errors:
        stages.append(StageResult(
            name="protocol_invocation", status="error",
            evidence=[driver_errors[0].event_id],
            note="Town's own driver malfunctioned: "
                 + driver_errors[0].detail.get("reason", "")
                 + "; this run is an error, not an agent failure"))
    elif strict_semantics and len(first_exchange) > 1:
        stages.append(StageResult(
            name="protocol_invocation", status="failed",
            evidence=[event.event_id for event in first_exchange],
            note="expected exactly one protocol exchange for attempt 1,"
                 f" observed {len(first_exchange)}"))
    elif first_exchange and first_exchange[0].detail.get("ok"):
        detail = first_exchange[0].detail
        if detail.get("kind") != "task" or not detail.get("state"):
            stages.append(StageResult(
                name="protocol_invocation", status="failed",
                evidence=[first_exchange[0].event_id],
                note="the response was not a well-formed task"))
        elif strict_semantics and detail.get("state") != "completed":
            stages.append(StageResult(
                name="protocol_invocation", status="failed",
                evidence=[first_exchange[0].event_id],
                note="expected successful terminal task state 'completed',"
                     f" observed {detail.get('state')!r}"))
        else:
            stages.append(StageResult(
                name="protocol_invocation", status="passed",
                evidence=[first_exchange[0].event_id],
                note=f"task {detail.get('task_id')} state"
                     f" {detail.get('state')}"))
    elif first_exchange:
        stages.append(StageResult(
            name="protocol_invocation", status="failed",
            evidence=[first_exchange[0].event_id],
            note=first_exchange[0].detail.get("reason", "")))
    else:
        stages.append(StageResult(name="protocol_invocation",
                                  status="not_enough_evidence",
                                  note="invocation was never reached"))

    first_fulfillment = find("fulfillment_observed", attempt=1)
    first_bad = find("fulfillment_unparseable", attempt=1)
    first_outcomes = first_fulfillment + first_bad
    expected_outputs = profile.expected.get("terminal_fulfillments", 1)
    if strict_semantics:
        successful_exchange = (
            len(first_exchange) == 1
            and first_exchange[0].detail.get("ok")
            and first_exchange[0].detail.get("kind") == "task"
            and first_exchange[0].detail.get("state") == "completed"
        )
        if not successful_exchange:
            stages.append(StageResult(
                name="semantic_result", status="not_enough_evidence",
                note="no successful terminal task output was observed"))
        else:
            output_count = first_exchange[0].detail.get(
                "terminal_output_count")
            if type(output_count) is not int \
                    or output_count != expected_outputs:
                evidence = [first_exchange[0].event_id]
                if first_bad:
                    evidence.append(first_bad[0].event_id)
                stages.append(StageResult(
                    name="semantic_result", status="failed",
                    evidence=evidence,
                    note="expected exactly one terminal text output,"
                         f" observed {output_count!r}"))
            elif len(first_outcomes) != output_count:
                stages.append(StageResult(
                    name="semantic_result", status="failed",
                    evidence=[event.event_id for event in first_outcomes],
                    note=f"recorded {output_count} terminal text output but"
                         f" observed {len(first_outcomes)} fulfillment"
                         " evaluation events"))
            elif first_fulfillment:
                stages.append(_semantic_fulfillment_stage(
                    profile, first_fulfillment[0]))
            elif first_bad:
                stages.append(StageResult(
                    name="semantic_result", status="failed",
                    evidence=[first_bad[0].event_id],
                    note="the fulfillment artifact is not parseable"))
            else:
                stages.append(StageResult(
                    name="semantic_result", status="not_enough_evidence",
                    note="the terminal output was not evaluated"))
    elif first_fulfillment:
        stages.append(_semantic_fulfillment_stage(
            profile, first_fulfillment[0]))
    elif first_bad:
        stages.append(StageResult(
            name="semantic_result", status="failed",
            evidence=[first_bad[0].event_id],
            note="the fulfillment artifact is not parseable"))
    else:
        stages.append(StageResult(name="semantic_result",
                                  status="not_enough_evidence",
                                  note="no fulfillment was observed"))

    second = find("fulfillment_observed", attempt=2)
    second_exchange = find("protocol_exchange", attempt=2)
    second_bad = find("fulfillment_unparseable", attempt=2)
    second_outcomes = second + second_bad
    if strict_semantics and len(first_fulfillment) == 1 \
            and len(second_exchange) > 1:
        stages.append(StageResult(
            name="duplicate_request", status="failed",
            evidence=[event.event_id for event in second_exchange],
            note="expected exactly one protocol exchange for attempt 2,"
                 f" observed {len(second_exchange)}"))
    elif strict_semantics and len(first_fulfillment) == 1 \
            and len(second_exchange) == 1:
        detail = second_exchange[0].detail
        if not detail.get("ok"):
            stages.append(StageResult(
                name="duplicate_request", status="failed",
                evidence=[second_exchange[0].event_id],
                note="the duplicate request failed: "
                     + detail.get("reason", "protocol invocation failed")))
        elif detail.get("kind") != "task" or detail.get("state") != "completed":
            stages.append(StageResult(
                name="duplicate_request", status="failed",
                evidence=[second_exchange[0].event_id],
                note="expected duplicate request to return successful terminal"
                     " task state 'completed', observed"
                     f" {detail.get('state')!r}"))
        elif type(detail.get("terminal_output_count")) is not int \
                or detail.get("terminal_output_count") != expected_outputs:
            evidence = [second_exchange[0].event_id]
            if second_bad:
                evidence.append(second_bad[0].event_id)
            stages.append(StageResult(
                name="duplicate_request", status="failed",
                evidence=evidence,
                note="expected exactly one terminal text output from the"
                     " duplicate request, observed"
                     f" {detail.get('terminal_output_count')!r}"))
        elif len(second_outcomes) != detail.get("terminal_output_count"):
            stages.append(StageResult(
                name="duplicate_request", status="failed",
                evidence=[event.event_id for event in second_outcomes],
                note="recorded one terminal text output from the duplicate"
                     f" request but observed {len(second_outcomes)}"
                     " fulfillment evaluation events"))
        elif second:
            same = (second[0].detail.get("content_digest")
                    == first_fulfillment[0].detail.get("content_digest"))
            if same:
                stages.append(StageResult(
                    name="duplicate_request", status="passed",
                    evidence=[second[0].event_id],
                    note="the same logical order was delivered twice; no"
                         " second distinct fulfillment appeared"))
            else:
                stages.append(StageResult(
                    name="duplicate_request", status="failed",
                    evidence=[first_fulfillment[0].event_id,
                              second[0].event_id],
                    note="idempotency defect: the duplicate produced a"
                         " second distinct fulfillment"))
        elif second_bad:
            stages.append(StageResult(
                name="duplicate_request", status="failed",
                evidence=[second_bad[0].event_id],
                note="the duplicate fulfillment artifact is not parseable"))
        else:
            stages.append(StageResult(
                name="duplicate_request", status="not_enough_evidence",
                note="the duplicate terminal output was not evaluated"))
    elif not strict_semantics and first_fulfillment and second:
        same = (second[0].detail.get("content_digest")
                == first_fulfillment[0].detail.get("content_digest"))
        if same:
            stages.append(StageResult(
                name="duplicate_request", status="passed",
                evidence=[second[0].event_id],
                note="the same logical order was delivered twice; no"
                     " second distinct fulfillment appeared"))
        else:
            stages.append(StageResult(
                name="duplicate_request", status="failed",
                evidence=[first_fulfillment[0].event_id,
                          second[0].event_id],
                note="idempotency defect: the duplicate produced a"
                     " second distinct fulfillment"))
    else:
        stages.append(StageResult(
            name="duplicate_request", status="not_enough_evidence",
            note="the controlled condition was never reached"))

    cascade_unreached(stages)
    return EvidenceResult(run_id=run_id,
                          evaluator_version=path_evaluator_version(profile),
                          stages=stages, verdict=stage_verdict(stages),
                          evaluated_at=time.time())
