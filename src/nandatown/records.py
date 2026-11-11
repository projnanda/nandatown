"""The five shared record types and canonical fingerprinting.

The five records that keep evidence understandable (doc section 12):
the test profile (the recipe), the run (the attempt), the intents (the
requested actions), the events (the attributed facts), and the evidence
bundle (the portable result, assembled in bundle.py from the other four
plus the evaluator result).
"""

from __future__ import annotations

from datetime import date
import hashlib
import ipaddress
import json
import math
import re
from typing import Any, Literal
import unicodedata
from urllib.parse import urlsplit

from pydantic import (
    BaseModel, ConfigDict, Field, field_validator, model_validator,
)


def canonical_json(obj: Any) -> str:
    """Serialize to canonical JSON: sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def strict_canonical_json(obj: Any) -> str:
    """Serialize a strict research-contract value to canonical JSON."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def fingerprint(obj: Any) -> str:
    """Stable content fingerprint of a JSON-serializable object."""
    digest = hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def strict_fingerprint(obj: Any) -> str:
    """Fingerprint strict JSON without admitting non-finite numbers."""
    digest = hashlib.sha256(
        strict_canonical_json(obj).encode("utf-8")).hexdigest()
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


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True,
                              hide_input_in_errors=True)

    @field_validator("*", check_fields=False)
    @classmethod
    def validate_task_strings(cls, value):
        if isinstance(value, str):
            _validate_task_text(value)
        return value


_SECRET_TEXT = re.compile(
    r"(?:ss_[0-9a-f]{12}_[A-Za-z0-9_-]{32,}|"
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"\bBearer\s+[A-Za-z0-9._~+/-]+=*|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    re.IGNORECASE,
)
_PARAMETER_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DENIED_PARAMETER_KEYS = frozenset({
    "approval", "approved", "approval_status", "approval_digest",
    "approved_digest", "approved_task_digest", "task_digest", "verdict",
    "verdict_policy", "confirm_oos", "oos", "oos_confirmation",
    "oos_budget", "oos_limit", "oos_trial", "budget", "split",
    "actor_id", "agent_id", "author", "author_agent_id", "reviewer",
    "reviewer_id", "reviewer_agent_id", "operator", "operator_id",
    "identity", "identities", "role", "review", "review_content",
    "review_assessment", "review_recommendation", "assessment", "runtime",
    "agent_runtime", "executable", "command", "cwd", "env",
    "environment", "dataset", "dataset_version", "is_dataset",
    "oos_dataset", "is_root", "oos_root", "db", "database", "db_path",
    "capture", "capture_path", "path", "project_root", "state_dir",
    "journal", "town_url", "run_id", "request_id", "message_id",
    "evidence", "evidence_policy", "token", "access_token",
    "refresh_token", "secret", "credential", "credentials",
    "authorization",
})
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_EXACT_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HOST_LABEL = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_URI_PATH = re.compile(
    r"^(?:[A-Za-z0-9._~!$&'()*+,;=:@/\-]|%[0-9A-Fa-f]{2})*$")
_URI_QUERY_FRAGMENT = re.compile(
    r"^(?:[A-Za-z0-9._~!$&'()*+,;=:@/?\-]|%[0-9A-Fa-f]{2})*$")


def _validate_task_text(value: str, *, max_length: int | None = None) -> None:
    if max_length is not None and len(value) > max_length:
        raise ValueError("task string exceeds its character limit")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("task string is not NFC normalized")
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
           or unicodedata.category(char) == "Cf" for char in value):
        raise ValueError("task string contains a forbidden control character")
    if _SECRET_TEXT.search(value):
        raise ValueError("task string contains credential-like content")


def _validate_parameters(value: Any, depth: int = 0) -> None:
    if isinstance(value, dict):
        if depth > 4:
            raise ValueError("strategy parameters exceed maximum depth")
        if len(value) > 32:
            raise ValueError("strategy parameter object exceeds maximum size")
        for key, child in value.items():
            if type(key) is not str or not _PARAMETER_KEY.fullmatch(key):
                raise ValueError("strategy parameter key is invalid")
            _validate_task_text(key)
            if key in _DENIED_PARAMETER_KEYS:
                raise ValueError("strategy parameter key grants denied authority")
            _validate_parameters(child, depth + 1)
        return
    if isinstance(value, list):
        if depth > 4:
            raise ValueError("strategy parameters exceed maximum depth")
        if len(value) > 32:
            raise ValueError("strategy parameter array exceeds maximum size")
        for child in value:
            _validate_parameters(child, depth + 1)
        return
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > 1_000_000_000_000:
            raise ValueError("strategy parameter number exceeds maximum magnitude")
        return
    if type(value) is float:
        if not math.isfinite(value) or abs(value) > 1_000_000_000_000:
            raise ValueError("strategy parameter number is invalid")
        return
    if type(value) is str:
        _validate_task_text(value, max_length=256)
        return
    raise ValueError("strategy parameter has an unsupported JSON type")


def _json_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_json_depth(child) for child in value.values()),
                       default=0)
    if isinstance(value, list):
        return 1 + max((_json_depth(child) for child in value), default=0)
    return 0


class ResearchSource(_StrictFrozenModel):
    uri: str = Field(min_length=1, max_length=2048)
    label: str = Field(min_length=1, max_length=128)
    accessed_on: str

    @field_validator("uri")
    @classmethod
    def validate_https_uri(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            # Accessing hostname/port also validates bracket and port syntax.
            host = parsed.hostname
            _ = parsed.port
        except ValueError:
            raise ValueError("source URI is invalid") from None
        if (parsed.scheme != "https" or not parsed.netloc or not host
                or parsed.username is not None or parsed.password is not None
                or any(char.isspace() for char in value)
                or "\\" in value or _BAD_PERCENT_ESCAPE.search(value)
                or _URI_PATH.fullmatch(parsed.path) is None
                or _URI_QUERY_FRAGMENT.fullmatch(parsed.query) is None
                or _URI_QUERY_FRAGMENT.fullmatch(parsed.fragment) is None):
            raise ValueError("source URI must be HTTPS without userinfo")
        try:
            ipaddress.ip_address(host)
            return value
        except ValueError:
            pass
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("source URI hostname is invalid") from None
        if (len(ascii_host) > 253 or ascii_host.endswith(".")
                or not all(_HOST_LABEL.fullmatch(label)
                           for label in ascii_host.split("."))):
            raise ValueError("source URI hostname is invalid")
        return value

    @field_validator("accessed_on")
    @classmethod
    def validate_access_date(cls, value: str) -> str:
        try:
            if not _EXACT_DATE.fullmatch(value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("source access date is invalid") from None
        return value


class ResearchIdea(_StrictFrozenModel):
    title: str = Field(min_length=1, max_length=160)
    hypothesis: str = Field(min_length=1, max_length=1000)
    rationale: str = Field(min_length=1, max_length=2000)
    sources: list[ResearchSource] = Field(min_length=1, max_length=8)


class ResearchStrategySpec(_StrictFrozenModel):
    name: str = Field(min_length=1, max_length=128)
    universe: str = Field(min_length=1, max_length=512)
    signal: str = Field(min_length=1, max_length=512)
    entry_rule: str = Field(min_length=1, max_length=512)
    exit_rule: str = Field(min_length=1, max_length=512)
    position_sizing: str = Field(min_length=1, max_length=512)
    rebalance: str = Field(min_length=1, max_length=512)
    parameters: dict[str, Any]

    @field_validator("parameters", mode="before")
    @classmethod
    def validate_parameter_contract(cls, value):
        if type(value) is not dict:
            raise ValueError("strategy parameters must be an object")
        _validate_parameters(value)
        return value

    @model_validator(mode="after")
    def validate_canonical_size(self):
        if len(strict_canonical_json(
                self.model_dump()).encode("utf-8")) > 6_144:
            raise ValueError("strategy specification exceeds byte limit")
        return self


class ResearchCandidate(_StrictFrozenModel):
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    strategy_spec: ResearchStrategySpec


class ResearchTask(_StrictFrozenModel):
    schema_version: Literal["sharpeshooter.research-task.v1"]
    client_task_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    idea: ResearchIdea
    candidates: list[ResearchCandidate] = Field(min_length=2, max_length=8)
    selected_candidate_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")

    @model_validator(mode="after")
    def validate_candidate_identity(self):
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate ids must be unique")
        if self.selected_candidate_id not in ids:
            raise ValueError("selected candidate must exist")
        specs = [strict_canonical_json(candidate.strategy_spec.model_dump())
                 for candidate in self.candidates]
        if len(specs) != len(set(specs)):
            raise ValueError("candidate strategy specifications must be unique")
        document = self.model_dump()
        if _json_depth(document) > 8:
            raise ValueError("research task exceeds maximum JSON depth")
        if len(strict_canonical_json(document).encode("utf-8")) > 65_536:
            raise ValueError("research task exceeds byte limit")
        return self


Fault = Literal[
    "none", "drop_wakeup", "duplicate_delivery", "lost_ack",
    "crash_after_claim", "context_truncation", "tool_error",
    "crash_amnesia", "crash_amnesia_fresh_ids"
]


class TestProfile(BaseModel):
    """The recipe: roles, task, conditions, evaluator."""

    model_config = ConfigDict(frozen=True)

    name: str
    task: QuoteTask | ResearchTask
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
