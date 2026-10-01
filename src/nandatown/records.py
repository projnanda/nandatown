"""The five shared record types and canonical fingerprinting.

The five records that keep evidence understandable (doc section 12):
the test profile (the recipe), the run (the attempt), the intents (the
requested actions), the events (the attributed facts), and the evidence
bundle (the portable result, assembled in bundle.py from the other four
plus the evaluator result).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


def canonical_json(obj: Any) -> str:
    """Serialize to canonical JSON: sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(obj: Any) -> str:
    """Stable content fingerprint of a JSON-serializable object."""
    digest = hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def json_type(value: Any) -> str:
    """The JSON type name of a decoded JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


class QuoteTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["quote"]
    sku: str
    quantity: int
    unit_price_cents: int
    expected_total_cents: int


Fault = Literal[
    "none", "drop_wakeup", "duplicate_delivery", "lost_ack",
    "crash_after_claim", "context_truncation", "tool_error",
    "crash_amnesia", "crash_amnesia_fresh_ids"
]


class TestProfile(BaseModel):
    """The recipe: roles, task, conditions, evaluator."""

    model_config = ConfigDict(frozen=True)

    name: str
    task: QuoteTask
    roles: dict[str, str]
    capabilities: dict[str, list[str]]
    fault: Fault
    lease_seconds: float
    evaluator: str
    runtimes: dict[str, str] = {}


class RunRecord(BaseModel):
    """The attempt: participants, versions, configuration."""

    run_id: str
    profile_name: str
    profile_fingerprint: str
    created_at: float
    participants: list[dict[str, Any]]
    releases: dict[str, str]
    config: dict[str, Any] = {}


class Intent(BaseModel):
    """One requested action: send, claim, or acknowledge."""

    intent_id: str
    run_id: str
    at: float
    actor: str
    action: str
    payload: dict[str, Any]


class TownEvent(BaseModel):
    """One time-stamped attributed fact: what one observer saw."""

    event_id: str
    run_id: str
    at: float
    observer: str
    kind: str
    subject: str
    detail: dict[str, Any] = {}


class AgentMessage(BaseModel):
    """One point-to-point work request or response between agents."""

    message_id: str
    conversation: str | None = None
    sender: str
    to: str
    kind: str
    body: dict[str, Any]
    content_fingerprint: str
    expires_at: float | None = None


class ReleaseRef(BaseModel):
    """The exact release of a skill, scenario, plugin, or package."""

    kind: Literal["skill", "scenario", "plugin", "package", "profile",
                  "service"]
    name: str
    version: str
    content_fingerprint: str


class EvidenceRecord(BaseModel):
    """One observer's result about one exact subject at one time."""

    record_id: str
    observer: str
    subject: str
    capability: str
    test: str
    result: Literal["passed", "failed", "not_enough_evidence"]
    at: float
    evidence: list[str] = []


StageStatus = Literal["passed", "failed", "not_enough_evidence",
                      "not_tested", "error"]


class StageResult(BaseModel):
    name: str
    status: StageStatus
    evidence: list[str] = []
    note: str = ""


class EvidenceResult(BaseModel):
    """The evaluator's output: one scoped observation, never a certificate."""

    run_id: str
    evaluator_version: str
    stages: list[StageResult]
    verdict: Literal["passed", "failed", "incomplete", "error"]
    evaluated_at: float
