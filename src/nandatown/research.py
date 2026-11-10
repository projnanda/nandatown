"""Strict SharpeShooter research contracts and attributed evaluation.

The research evaluator is deliberately separate from the historical quote
evaluator.  A closed profile selector is the only place that maps a Track
profile to its message kinds, skills, capability, and evaluator release.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    fingerprint,
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
    return fingerprint(task.model_dump())


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
    return fingerprint(value)


def _exact_mapping(value: Any, fields: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == fields


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
    if (body.get("schema_version") != "sharpeshooter.town-research-result.v1"
            or body.get("request_id") != request_id):
        _reject("request-binding", "research result names another request")
    if not isinstance(request, dict) or set(request) != {
            "schema_version", "task", "task_digest"}:
        _reject("request-shape", "stored research request is invalid")
    try:
        task = ResearchTask.model_validate(request["task"])
    except Exception:
        _reject("request-shape", "stored research task is invalid")
    expected_task_digest = research_task_digest(task)
    project = body.get("project_result")
    if not _exact_mapping(project, _PROJECT_FIELDS):
        _reject("result-shape", "project result is invalid")
    if (request.get("task_digest") != expected_task_digest
            or project.get("task_digest") != expected_task_digest):
        _reject("task-digest", "project result is not bound to the task")
    if body.get("request_digest") != _digest(request):
        _reject("request-digest", "project result is not bound to the request")

    task_doc = task.model_dump()
    selected = next(candidate for candidate in task_doc["candidates"]
                    if candidate["candidate_id"]
                    == task_doc["selected_candidate_id"])
    selected_spec = selected["strategy_spec"]
    selected_hash = _digest(selected_spec)
    receipt = project["receipt"]
    if not isinstance(receipt, dict):
        _reject("receipt-shape", "receipt fields are invalid")
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
    actual_candidates = (
        [(item.get("candidate_id"), item.get("strategy_hash"))
         for item in candidate_results]
        if isinstance(candidate_results, list)
        and all(_exact_mapping(item, _CANDIDATE_FIELDS)
                for item in candidate_results)
        else None
    )
    if actual_candidates != expected_candidates:
        _reject("candidate-binding", "candidate results do not match the task")
    if (project.get("client_task_id") != task.client_task_id
            or project.get("title") != task.idea.title
            or project.get("hypothesis") != task.idea.hypothesis
            or project.get("sources_digest") != _digest(task_doc["idea"]["sources"])):
        _reject("task-binding", "project result changed task content")

    author = project.get("author_agent_id")
    reviewer = project.get("reviewer_agent_id")
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
            or type(project.get("budget_before")) is not int
            or project.get("budget_before") != 0
            or type(project.get("budget_after")) is not int
            or project.get("budget_after") != 1
            or type(project.get("family_oos_trial_number")) is not int
            or project.get("family_oos_trial_number") != 1):
        _reject("false-lifecycle", "project lifecycle controls are incomplete")
    if project.get("limitations") != LIMITATIONS:
        _reject("wrong-limitations", "project limitations differ from the frozen disclosure")
    if project.get("receipt_integrity_hash") != _digest(receipt):
        _reject("receipt-hash", "receipt integrity hash does not match")
    if (receipt.get("receipt_id") != project.get("receipt_id")
            or receipt.get("idea", {}).get("author_agent_id") != author
            or review.get("review_id") != project.get("review_id")):
        _reject("receipt-binding", "receipt is not correlated to the project result")

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
    request_id = requests[0].subject if len(requests) == 1 else None
    stages: list[StageResult] = []
    if len(requests) == 1:
        stages.append(_stage("accepted", "passed", [requests[0].event_id]))
    elif requests:
        stages.append(_stage("accepted", "failed",
                             [item.event_id for item in requests],
                             f"{len(requests)} research requests were accepted; expected one"))
    else:
        stages.append(_stage("accepted", "not_enough_evidence", [],
                             "no accepted research request"))

    claims = find("message_claimed", subject=request_id, claimant=seller) \
        if request_id else []
    stages.append(_stage("claimed", "passed" if claims else "not_enough_evidence",
                         [item.event_id for item in claims],
                         "" if claims else "the research request was never claimed"))
    seller_acks = find("ack_recorded", observer=seller, subject=request_id) \
        if request_id else []
    received = [item for item in seller_acks
                if item.detail.get("status") in ("received", "processed")]
    stages.append(_stage("received", "passed" if received else "not_enough_evidence",
                         [received[0].event_id] if received else [],
                         "" if received else "no acknowledged receipt by the provider"))
    applied = [item for item in seller_acks
               if item.detail.get("status") == "processed"
               and isinstance(item.detail.get("note"), dict)
               and item.detail["note"].get("applied") is True]
    if len(applied) == 1:
        stages.append(_stage("processed", "passed", [applied[0].event_id]))
    elif len(applied) > 1:
        stages.append(_stage("processed", "failed",
                             [item.event_id for item in applied],
                             f"provider attributed {len(applied)} applications; expected one"))
    else:
        stages.append(_stage("processed", "not_enough_evidence", [],
                             "no provider-attributed application record"))

    responses = find("message_accepted", kind=RESEARCH_RESPONSE_KIND)
    response_id = responses[0].subject if len(responses) == 1 else None
    response_claims = find("message_claimed", subject=response_id,
                           claimant=buyer) if response_id else []
    correlated = (len(responses) == 1 and request_id is not None
                  and responses[0].detail.get("request_id") == request_id)
    if correlated and response_claims:
        stages.append(_stage("response", "passed",
                             [responses[0].event_id, response_claims[0].event_id]))
    elif len(responses) > 1:
        stages.append(_stage("response", "failed",
                             [item.event_id for item in responses],
                             f"{len(responses)} distinct research results were accepted; expected one"))
    elif responses and not correlated:
        stages.append(_stage("response", "failed", [responses[0].event_id],
                             "research result does not name the accepted request"))
    else:
        stages.append(_stage("response", "not_enough_evidence", [],
                             "no correlated research result was accepted and claimed"))

    terminal = [item for item in find("ack_recorded", observer=buyer,
                                      subject=response_id)
                if item.detail.get("status") != "retryable"] \
        if response_id else []
    assertions = terminal[-1].detail.get("note", {}) if terminal else {}
    semantic = (
        ("task_bound", "task_bound"),
        ("receipt_integrity", "receipt_verified"),
        ("independent_review", "independent_review"),
        ("control_complete", "control_complete"),
    )
    for stage_name, field in semantic:
        value = assertions.get(field) if isinstance(assertions, dict) else None
        note = (f"requester-attributed {field}; Town records the assertion"
                " but does not independently prove it")
        if value is True:
            stages.append(_stage(stage_name, "passed",
                                 [terminal[-1].event_id], note))
        elif value is False:
            stages.append(_stage(stage_name, "failed",
                                 [terminal[-1].event_id], note))
        else:
            stages.append(_stage(stage_name, "not_enough_evidence", [], note))

    if profile.fault == "duplicate_delivery":
        offered = {item.detail.get("fence")
                   for item in find("duplicate_offered")}
        duplicates = [item for item in seller_acks
                      if item.detail.get("status") == "processed"
                      and item.detail.get("fence") in offered
                      and item.detail.get("note", {}).get("duplicate") is True]
        stages.append(_stage(
            "duplicate_recognized",
            "passed" if duplicates else "not_enough_evidence",
            [item.event_id for item in duplicates],
            "" if duplicates else "no provider-attributed recognition of the injected duplicate",
        ))
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
