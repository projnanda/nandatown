"""Strict SharpeShooter research contracts and attributed evaluation.

The research evaluator is deliberately separate from the historical quote
evaluator.  A closed profile selector is the only place that maps a Track
profile to its message kinds, skills, capability, and evaluator release.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
import time
from typing import Any

from .evaluator import EVALUATOR_VERSION, evaluate
from .records import (
    EvidenceResult,
    QuoteTask,
    ResearchTask,
    StageResult,
    TestProfile,
    TownEvent,
    strict_canonical_json,
    strict_fingerprint,
)

RESEARCH_EVALUATOR_VERSION = "sharpeshooter-research-0.1.0"
RESEARCH_REQUEST_KIND = "research_request"
RESEARCH_RESPONSE_KIND = "research_result"
RESEARCH_CAPABILITY = "research.reviewed-receipt"
RESEARCH_REQUEST_ID = "research-1"

LIMITATIONS = [
    "The task is bounded and structured; it is not unrestricted prose research.",
    "Task content controls provenance and tested specifications, but the deterministic evaluator does not understand the economic thesis.",
    "Synthetic metrics do not validate strategy performance.",
    "Town proves transport and attributed requester checks; SharpeShooter proves its workflow/receipt; cross-system evidence correlates them.",
    "A receipt hash is tamper-evidence, not an independently signed truth attestation.",
    "The run does not prove real-agent reasoning, A2A/MCP conformance, hostile-code sandboxing, production readiness, or arbitrary interoperability.",
    "A `started` journal fails closed after interruption; v1 does not promise automatic project-level resumption.",
]

_PROJECT_FIELDS = {
    "schema_version", "client_task_id", "task_digest", "title", "hypothesis",
    "sources_digest", "agent_runtime", "idea_id", "family_id",
    "candidate_results", "selected_candidate_id", "selected_is_strategy_hash",
    "oos_run_id", "oos_strategy_hash", "family_oos_trial_number",
    "budget_before", "budget_after", "author_agent_id", "reviewer_agent_id",
    "review_id", "review_recommendation", "receipt_id", "receipt",
    "receipt_integrity_hash", "receipt_verification",
    "receipt_consumed_by_scout", "operator_approval", "operator_verdict",
    "synthetic_metrics", "limitations",
}
_CANDIDATE_FIELDS = {"candidate_id", "is_run_id", "strategy_hash"}
_RECEIPT_FIELDS = {
    "schema_version", "receipt_id", "issued_at", "idea", "strategy",
    "evaluation", "review", "provenance",
}
_RECEIPT_IDEA_FIELDS = {
    "idea_id", "family_id", "operator_id", "author_agent_id",
}
_RECEIPT_STRATEGY_FIELDS = {
    "version_id", "strategy_hash", "frozen_spec", "frozen_at",
}
_RECEIPT_EVALUATION_FIELDS = {
    "run_id", "split", "family_oos_trial_number", "dataset_version",
    "dataset_split_hash", "evaluator_name", "evaluator_version",
    "synthetic_metrics", "metrics", "checks",
}
_RECEIPT_METRIC_FIELDS = {
    "annualized_return", "annualized_volatility", "sharpe_ratio",
    "max_drawdown", "observations",
}
_RECEIPT_CHECK_FIELDS = {
    "opened_split", "dataset_version", "acceptance", "dataset_split_hash",
    "date_min", "date_max", "observations", "synthetic_metrics",
}
_RECEIPT_REVIEW_FIELDS = {
    "review_id", "reviewer_agent_id", "reviewer_operator_id",
    "separation_rule", "assessment", "concerns", "recommendation",
}
_RECEIPT_PROVENANCE_FIELDS = {
    "registration_event_id", "approval_event_id", "test_event_id",
    "review_event_id",
}
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROJECT_ID = re.compile(r"^[0-9a-f]{32}$")
_SLUG = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_RFC3339_Z = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_REVIEW_ASSESSMENT = (
    "Independent review of the bounded deterministic research task.")
_REVIEW_CONCERNS = ["Synthetic metrics are not performance evidence."]
_MAX_PROJECT_RESULT_BYTES = 65_536
_MAX_RECEIPT_BYTES = 32_768
_MAX_TOWN_RESULT_BYTES = 98_304


@dataclass(frozen=True)
class TrackContract:
    """All profile-specific runner semantics in one immutable descriptor."""

    request_kind: str
    response_kind: str
    evaluator_version: str
    skills: tuple[str, ...]
    seller_capability: str


_QUOTE_CONTRACT = TrackContract(
    request_kind="quote_request",
    response_kind="quote_response",
    evaluator_version=EVALUATOR_VERSION,
    skills=("town-protocol", "quote.read", "quote.request"),
    seller_capability="quote.read",
)
_RESEARCH_CONTRACT = TrackContract(
    request_kind=RESEARCH_REQUEST_KIND,
    response_kind=RESEARCH_RESPONSE_KIND,
    evaluator_version=RESEARCH_EVALUATOR_VERSION,
    skills=("town-protocol", "research.request", "research.reviewed-receipt"),
    seller_capability=RESEARCH_CAPABILITY,
)


def contract_for_profile(profile: TestProfile) -> TrackContract:
    """Select one supported task/evaluator pair; reject crossed pairs."""
    pair = (type(profile.task), profile.evaluator)
    contracts = {
        (QuoteTask, "stage-evaluator"): _QUOTE_CONTRACT,
        (ResearchTask, RESEARCH_EVALUATOR_VERSION): _RESEARCH_CONTRACT,
    }
    try:
        return contracts[pair]
    except KeyError:
        raise ValueError("unsupported profile/evaluator pairing") from None


def research_task_digest(task: ResearchTask) -> str:
    return strict_fingerprint(task.model_dump())


def build_research_request(task: ResearchTask) -> dict[str, Any]:
    return {
        "schema_version": "sharpeshooter.research-request.v1",
        "task": task.model_dump(),
        "task_digest": research_task_digest(task),
    }


class ResearchResultError(ValueError):
    """A bounded requester validation failure safe to acknowledge."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _reject(code: str, message: str) -> None:
    raise ResearchResultError(code, message)


def _digest(value: Any) -> str:
    return strict_fingerprint(value)


def _exact_mapping(value: Any, fields: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == fields


def _matches(value: Any, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _strict_int(value: Any, expected: int | None = None) -> bool:
    return (type(value) is int
            and (expected is None or value == expected))


def _valid_utc_timestamp(value: Any) -> bool:
    if not _matches(value, _RFC3339_Z):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _canonical_bytes(value: Any) -> bytes:
    return strict_canonical_json(value).encode("utf-8")


def validate_research_result(
        body: Any, *, request_id: str, request: dict[str, Any]
        ) -> dict[str, bool]:
    """Validate the bounded Town projection and return requester assertions.

    The mailbox has already decoded JSON.  Canonical fingerprints bind the
    decoded value, but this helper makes no claim about duplicate keys on the
    original wire representation.
    """
    outer_fields = {
        "schema_version", "request_id", "request_digest", "project_result",
    }
    if not isinstance(body, dict) or set(body) != outer_fields:
        _reject("result-shape", "research result fields are invalid")
    try:
        body_bytes = _canonical_bytes(body)
    except (TypeError, ValueError, UnicodeError):
        _reject("result-shape", "research result is not canonical JSON")
    if len(body_bytes) > _MAX_TOWN_RESULT_BYTES:
        _reject("result-size", "research result exceeds its byte limit")
    if (body.get("schema_version") != "sharpeshooter.town-research-result.v1"
            or body.get("request_id") != request_id
            or request_id != RESEARCH_REQUEST_ID):
        _reject("request-binding", "research result names another request")
    if not isinstance(request, dict) or set(request) != {
            "schema_version", "task", "task_digest"}:
        _reject("request-shape", "stored research request is invalid")
    if request.get("schema_version") != "sharpeshooter.research-request.v1" \
            or not _matches(request.get("task_digest"), _DIGEST):
        _reject("request-shape", "stored research request is invalid")
    try:
        task = ResearchTask.model_validate(request["task"])
        request_digest = _digest(request)
    except Exception:
        _reject("request-shape", "stored research task is invalid")
    expected_task_digest = research_task_digest(task)
    project = body.get("project_result")
    if not _exact_mapping(project, _PROJECT_FIELDS):
        _reject("result-shape", "project result is invalid")
    try:
        project_bytes = _canonical_bytes(project)
    except (TypeError, ValueError, UnicodeError):
        _reject("result-shape", "project result is not canonical JSON")
    if len(project_bytes) > _MAX_PROJECT_RESULT_BYTES:
        _reject("result-size", "project result exceeds its byte limit")
    if (request.get("task_digest") != expected_task_digest
            or project.get("task_digest") != expected_task_digest):
        _reject("task-digest", "project result is not bound to the task")
    if (not _matches(body.get("request_digest"), _DIGEST)
            or body.get("request_digest") != request_digest):
        _reject("request-digest", "project result is not bound to the request")
    if (project.get("schema_version") != "sharpeshooter.research-result.v1"
            or project.get("agent_runtime") != "fake"
            or project.get("synthetic_metrics") is not True):
        _reject("result-contract", "project result literals are invalid")

    task_doc = task.model_dump()
    selected = next(candidate for candidate in task_doc["candidates"]
                    if candidate["candidate_id"]
                    == task_doc["selected_candidate_id"])
    selected_spec = selected["strategy_spec"]
    selected_hash = _digest(selected_spec)
    receipt = project["receipt"]
    if not isinstance(receipt, dict):
        _reject("receipt-shape", "receipt fields are invalid")
    try:
        receipt_bytes = _canonical_bytes(receipt)
    except (TypeError, ValueError, UnicodeError):
        _reject("receipt-shape", "receipt is not canonical JSON")
    if len(receipt_bytes) > _MAX_RECEIPT_BYTES:
        _reject("receipt-size", "receipt exceeds its byte limit")
    if not isinstance(receipt.get("review"), dict):
        _reject("missing-review", "receipt has no independent review")
    if (not _exact_mapping(receipt, _RECEIPT_FIELDS)
            or not _exact_mapping(receipt.get("idea"), _RECEIPT_IDEA_FIELDS)
            or not _exact_mapping(receipt.get("strategy"),
                                  _RECEIPT_STRATEGY_FIELDS)
            or not _exact_mapping(receipt.get("evaluation"),
                                  _RECEIPT_EVALUATION_FIELDS)
            or not _exact_mapping(receipt.get("review"),
                                  _RECEIPT_REVIEW_FIELDS)
            or not _exact_mapping(receipt.get("provenance"),
                                  _RECEIPT_PROVENANCE_FIELDS)
            or not _exact_mapping(receipt["evaluation"].get("metrics"),
                                  _RECEIPT_METRIC_FIELDS)
            or not _exact_mapping(receipt["evaluation"].get("checks"),
                                  _RECEIPT_CHECK_FIELDS)):
        _reject("receipt-shape", "receipt fields are invalid")
    strategy = receipt["strategy"]
    if (project.get("selected_candidate_id")
            != task_doc["selected_candidate_id"]
            or project.get("selected_is_strategy_hash") != selected_hash
            or project.get("oos_strategy_hash") != selected_hash
            or not isinstance(strategy, dict)
            or strategy.get("frozen_spec") != selected_spec
            or strategy.get("strategy_hash") != selected_hash):
        _reject("selected-spec", "selected strategy is not the frozen task specification")

    expected_candidates = [
        (candidate["candidate_id"], _digest(candidate["strategy_spec"]))
        for candidate in task_doc["candidates"]
    ]
    candidate_results = project.get("candidate_results")
    candidates_valid = (
        type(candidate_results) is list
        and all(_exact_mapping(item, _CANDIDATE_FIELDS)
                and _matches(item.get("candidate_id"), _SLUG)
                and _matches(item.get("is_run_id"), _PROJECT_ID)
                and _matches(item.get("strategy_hash"), _DIGEST)
                for item in candidate_results)
        and len({item["is_run_id"] for item in candidate_results})
        == len(candidate_results)
    )
    actual_candidates = ([(item["candidate_id"], item["strategy_hash"])
                          for item in candidate_results]
                         if candidates_valid else None)
    if actual_candidates != expected_candidates:
        _reject("candidate-binding", "candidate results do not match the task")
    if project.get("oos_run_id") in {
            item["is_run_id"] for item in candidate_results}:
        _reject("run-separation", "OOS run must be distinct from IS runs")
    if (project.get("client_task_id") != task.client_task_id
            or project.get("title") != task.idea.title
            or project.get("hypothesis") != task.idea.hypothesis
            or project.get("sources_digest") != _digest(task_doc["idea"]["sources"])):
        _reject("task-binding", "project result changed task content")

    author = project.get("author_agent_id")
    reviewer = project.get("reviewer_agent_id")
    project_ids = (
        project.get("idea_id"), project.get("family_id"),
        project.get("oos_run_id"), author, reviewer,
        project.get("review_id"), project.get("receipt_id"),
    )
    if not all(_matches(value, _PROJECT_ID) for value in project_ids):
        _reject("result-contract", "project identifiers are invalid")
    if (not _matches(project.get("sources_digest"), _DIGEST)
            or not _matches(project.get("selected_is_strategy_hash"), _DIGEST)
            or not _matches(project.get("oos_strategy_hash"), _DIGEST)
            or not _matches(project.get("receipt_integrity_hash"), _DIGEST)):
        _reject("result-contract", "project digests are invalid")
    if not isinstance(author, str) or not isinstance(reviewer, str) \
            or author == reviewer:
        _reject("same-reviewer", "reviewer must be distinct from author")
    review = receipt["review"]
    if (project.get("review_recommendation") != "accept"
            or review.get("recommendation") != "accept"
            or review.get("reviewer_agent_id") != reviewer
            or review.get("separation_rule") != "distinct_agent"):
        _reject("missing-review", "receipt review is absent or non-accepting")

    if (project.get("receipt_consumed_by_scout") is not True
            or project.get("receipt_verification") != "VERIFIED"
            or project.get("operator_approval") != "approved"
            or project.get("operator_verdict") != "accept"
            or not _strict_int(project.get("budget_before"), 0)
            or not _strict_int(project.get("budget_after"), 1)
            or not _strict_int(project.get("family_oos_trial_number"), 1)):
        _reject("false-lifecycle", "project lifecycle controls are incomplete")
    if project.get("limitations") != LIMITATIONS:
        _reject("wrong-limitations", "project limitations differ from the frozen disclosure")
    idea = receipt["idea"]
    evaluation = receipt["evaluation"]
    metrics = evaluation["metrics"]
    checks = evaluation["checks"]
    provenance = receipt["provenance"]
    if (receipt.get("schema_version") != "1.0"
            or receipt.get("receipt_id") != project.get("receipt_id")
            or not _valid_utc_timestamp(receipt.get("issued_at"))
            or idea.get("idea_id") != project.get("idea_id")
            or idea.get("family_id") != project.get("family_id")
            or idea.get("author_agent_id") != author
            or not _matches(idea.get("operator_id"), _PROJECT_ID)
            or not _matches(strategy.get("version_id"), _PROJECT_ID)
            or not _valid_utc_timestamp(strategy.get("frozen_at"))
            or evaluation.get("run_id") != project.get("oos_run_id")
            or evaluation.get("split") != "oos"
            or not _strict_int(evaluation.get("family_oos_trial_number"), 1)
            or evaluation.get("dataset_version") != "hw8-deterministic-fixture"
            or evaluation.get("dataset_split_hash") != "sha256:" + "2" * 64
            or evaluation.get("evaluator_name")
            != "deterministic_synthetic_stub"
            or evaluation.get("evaluator_version") != "1"
            or evaluation.get("synthetic_metrics") is not True):
        _reject("receipt-binding", "receipt is not correlated to the project result")
    metric_names = (
        "annualized_return", "annualized_volatility", "sharpe_ratio",
        "max_drawdown",
    )
    if (any(type(metrics.get(name)) is not float
            or not math.isfinite(metrics[name]) for name in metric_names)
            or not _strict_int(metrics.get("observations"), 100)
            or checks.get("opened_split") != "oos"
            or checks.get("dataset_version") != evaluation["dataset_version"]
            or checks.get("acceptance") != "pending"
            or checks.get("dataset_split_hash")
            != evaluation["dataset_split_hash"]
            or checks.get("date_min") != "2019-01-02"
            or checks.get("date_max") != "2026-06-19"
            or not _strict_int(checks.get("observations"),
                               metrics["observations"])
            or checks.get("synthetic_metrics") is not True):
        _reject("receipt-binding", "receipt evaluation facts are invalid")
    if (review.get("review_id") != project.get("review_id")
            or review.get("reviewer_agent_id") != reviewer
            or review.get("reviewer_operator_id") != idea["operator_id"]
            or review.get("separation_rule") != "distinct_agent"
            or review.get("assessment") != _REVIEW_ASSESSMENT
            or review.get("concerns") != _REVIEW_CONCERNS
            or review.get("recommendation") != "accept"):
        _reject("missing-review", "receipt review is absent or non-accepting")
    provenance_values = list(provenance.values())
    if (not all(_strict_int(value) and value > 0
                for value in provenance_values)
            or len(set(provenance_values)) != len(provenance_values)):
        _reject("receipt-binding", "receipt provenance is invalid")
    if project.get("receipt_integrity_hash") != _digest(receipt):
        _reject("receipt-hash", "receipt integrity hash does not match")

    return {
        "task_bound": True,
        "receipt_verified": True,
        "independent_review": True,
        "control_complete": True,
    }


def _stage(name: str, status: str, evidence: list[str], note: str = "") -> StageResult:
    return StageResult(name=name, status=status, evidence=evidence, note=note)


def evaluate_research(profile: TestProfile, run_id: str,
                      events: list[TownEvent]) -> EvidenceResult:
    """Evaluate transport separately from requester's semantic assertions."""
    contract_for_profile(profile)
    seller = next((name for name, role in profile.roles.items()
                   if role == "seller"), "seller")
    buyer = next((name for name, role in profile.roles.items()
                  if role == "buyer"), "buyer")

    def find(event_kind: str, **conditions: Any) -> list[TownEvent]:
        found = []
        for event in events:
            if event.kind != event_kind:
                continue
            if any((event.observer if key == "observer"
                    else event.subject if key == "subject"
                    else event.detail.get(key)) != value
                   for key, value in conditions.items()):
                continue
            found.append(event)
        return found

    requests = find("message_accepted", kind=RESEARCH_REQUEST_KIND)
    directed_requests = [event for event in requests
                         if event.detail.get("sender") == buyer
                         and event.detail.get("to") == seller]
    request_id = (directed_requests[0].subject
                  if len(requests) == len(directed_requests) == 1 else None)
    stages: list[StageResult] = []
    if request_id is not None:
        stages.append(_stage("accepted", "passed", [requests[0].event_id]))
    else:
        stages.append(_stage(
            "accepted", "failed", [item.event_id for item in requests],
            "expected exactly one buyer-to-seller research request"))

    request_claims = (find("message_claimed", subject=request_id)
                      if request_id else [])
    claims = [item for item in request_claims
              if item.detail.get("claimant") == seller]
    claims_valid = len(claims) == len(request_claims) == 1
    stages.append(_stage(
        "claimed", "passed" if claims_valid else "failed",
        [item.event_id for item in request_claims],
        "" if claims_valid else
        "the research request lacks a seller-attributed claim"))

    request_acks = (find("ack_recorded", subject=request_id)
                    if request_id else [])
    seller_acks = [item for item in request_acks if item.observer == seller]
    received = [item for item in seller_acks
                if item.detail.get("status") in ("received", "processed")]
    received_valid = (len(received) == len(seller_acks)
                      == len(request_acks) == 1)
    stages.append(_stage(
        "received", "passed" if received_valid else "failed",
        [item.event_id for item in request_acks],
        "" if received_valid else
        "the request lacks a valid seller-attributed receipt"))
    applied = [item for item in seller_acks
               if item.detail.get("status") == "processed"
               and isinstance(item.detail.get("note"), dict)
               and item.detail["note"].get("applied") is True]
    if len(applied) == len(seller_acks) == len(request_acks) == 1:
        stages.append(_stage("processed", "passed", [applied[0].event_id]))
    elif len(applied) > 1:
        stages.append(_stage("processed", "failed",
                             [item.event_id for item in applied],
                             f"provider attributed {len(applied)} applications; expected one"))
    else:
        stages.append(_stage("processed", "failed", [],
                             "no provider-attributed application record"))

    responses = find("message_accepted", kind=RESEARCH_RESPONSE_KIND)
    directed_responses = [event for event in responses
                          if event.detail.get("sender") == seller
                          and event.detail.get("to") == buyer
                          and event.detail.get("request_id") == request_id]
    response_id = (directed_responses[0].subject
                   if len(responses) == len(directed_responses) == 1
                   else None)
    all_response_claims = (find("message_claimed", subject=response_id)
                           if response_id else [])
    response_claims = [item for item in all_response_claims
                       if item.detail.get("claimant") == buyer]
    response_valid = (response_id is not None
                      and len(response_claims)
                      == len(all_response_claims) == 1)
    if response_valid:
        stages.append(_stage("response", "passed",
                             [responses[0].event_id, response_claims[0].event_id]))
    else:
        stages.append(_stage(
            "response", "failed",
            [item.event_id for item in responses + all_response_claims],
            "expected one correlated seller-to-buyer result claimed by buyer"))

    response_acks = (find("ack_recorded", subject=response_id)
                     if response_id else [])
    terminal = [item for item in response_acks
                if item.observer == buyer
                and item.detail.get("status") == "processed"]
    expected_assertions = {
        "task_bound", "receipt_verified", "independent_review",
        "control_complete",
    }
    if profile.fault == "duplicate_delivery":
        expected_assertions.add("duplicate_recognized")
    assertions = (terminal[0].detail.get("note")
                  if len(terminal) == len(response_acks) == 1 else None)
    assertion_contract_valid = (
        isinstance(assertions, dict)
        and set(assertions) == expected_assertions
        and all(type(value) is bool for value in assertions.values())
    )
    semantic = [
        ("task_bound", "task_bound"),
        ("receipt_integrity", "receipt_verified"),
        ("independent_review", "independent_review"),
        ("control_complete", "control_complete"),
    ]
    if profile.fault == "duplicate_delivery":
        semantic.append(("duplicate_recognized", "duplicate_recognized"))
    for stage_name, field in semantic:
        value = assertions.get(field) if assertion_contract_valid else None
        note = (f"requester-attributed {field}; Town records the assertion"
                " but does not independently prove it")
        if value is True:
            stages.append(_stage(stage_name, "passed",
                                 [terminal[0].event_id], note))
        else:
            stages.append(_stage(stage_name, "failed",
                                 [item.event_id for item in response_acks],
                                 note))
    stages.append(_stage("portable_identity", "not_tested", [],
                         "portable identity was not exercised by this profile"))

    statuses = {stage.status for stage in stages}
    verdict = ("failed" if "failed" in statuses else
               "error" if "error" in statuses else
               "incomplete" if "not_enough_evidence" in statuses else
               "passed")
    return EvidenceResult(
        run_id=run_id,
        evaluator_version=RESEARCH_EVALUATOR_VERSION,
        stages=stages,
        verdict=verdict,
        evaluated_at=time.time(),
    )


def evaluate_profile(profile: TestProfile, run_id: str,
                     events: list[TownEvent]) -> EvidenceResult:
    contract = contract_for_profile(profile)
    if contract is _QUOTE_CONTRACT:
        return evaluate(profile, run_id, events)
    return evaluate_research(profile, run_id, events)
