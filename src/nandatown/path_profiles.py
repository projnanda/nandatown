"""Exact versioned path profiles: executable integration contracts.

A path profile names what is being tested, the exact request, the
expected observable result, the controlled condition, and the limits.
It is frozen and fingerprinted; a result binds to the exact profile
version it ran under. These fixtures are Town-authored integration
tests, not universal commerce standards.
"""

from __future__ import annotations

import importlib.resources
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .records import fingerprint

PATH_EVALUATOR = "path-evaluator@0.1"
STRICT_PATH_EVALUATOR = "path-evaluator@0.2"
QUOTE_INTENT_EVALUATOR = "quote-intent-evaluator@0.1"
STRICT_QUOTE_INTENT_EVALUATOR = "quote-intent-evaluator@0.2"
ORDER_CONFLICT_EVALUATOR = "order-conflict-evaluator@0.1"
TRIAL_HIRE_EVALUATOR = "trial-hire-evaluator@0.1"
INVOICE_EXTRACTION_EVALUATOR = "invoice-extraction-evaluator@0.1"
QUOTE_INTENT_FIELDS = ("sku", "color", "quantity", "merchant_id", "currency")


def _path_request(name: str) -> dict[str, Any]:
    """A request body shipped as package data, too long to inline."""
    path = importlib.resources.files("nandatown") / "path_requests" / name
    return json.loads(path.read_text(encoding="utf-8"))


# What the trial-then-hire profiles leave unchecked. It is part of the
# profile, so it is fingerprinted and travels in every bundle.
TRIAL_HIRE_NOT_TESTED = [
    "one synchronous message/send with a long timeout; timeout_seconds is"
    " per HTTP phase, not a total deadline",
    "async tasks/get polling and streaming",
    "real payment",
    "the hire step and the hired agent's real-job outcome",
    "whether the recommended provider is actually the best one",
]

# What the invoice-extraction profile leaves unchecked. Field accuracy is
# deliberately out of scope: it is the hiring agent's job, and a public
# per-provider accuracy record would rank real providers in this repository.
INVOICE_EXTRACTION_NOT_TESTED = [
    "field accuracy: only the output's types and required keys are checked",
    "one synchronous message/send; timeout_seconds is per HTTP phase, not a"
    " total deadline",
    "async tasks/get polling and streaming",
    "real payment, and whether cost_usd is the provider's true spend",
    "documents other than the one pinned synthetic invoice",
]


class PathProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile_id: str
    version: str
    protocol: Literal["a2a"]
    capability: str
    request: dict[str, Any]
    expected: dict[str, Any]
    # duplicate_request resends the same order unchanged. conflicting_retry
    # resends its request_id with expected["retry_changes"] applied; the
    # subject must refuse it.
    controlled_condition: Literal["duplicate_request", "conflicting_retry"]
    limits: dict[str, float]
    evaluator: str

    @property
    def ref(self) -> str:
        return f"{self.profile_id}@{self.version}"

    def fingerprint(self) -> str:
        return fingerprint(self.model_dump())


PATH_PROFILES: dict[str, PathProfile] = {
    "a2a-quote-intent@0.1": PathProfile(
        profile_id="a2a-quote-intent",
        version="0.1",
        protocol="a2a",
        capability="quote",
        request={"sku": "widget", "quantity": 2, "unit_price_cents": 1995,
                 "color": "blue", "merchant_id": "town-reference", "currency": "USD"},
        expected={"max_total_cents": 3990,
                  "quote": {"sku": "widget", "quantity": 2, "color": "blue",
                            "merchant_id": "town-reference", "currency": "USD"}},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 15.0, "max_response_bytes": 1_048_576},
        evaluator=QUOTE_INTENT_EVALUATOR,
    ),
    "a2a-quote-intent@0.2": PathProfile(
        profile_id="a2a-quote-intent",
        version="0.2",
        protocol="a2a",
        capability="quote",
        request={"sku": "widget", "quantity": 2, "unit_price_cents": 1995,
                 "color": "blue", "merchant_id": "town-reference",
                 "currency": "USD"},
        expected={"max_total_cents": 3990, "terminal_fulfillments": 1,
                  "quote": {"sku": "widget", "quantity": 2,
                            "color": "blue",
                            "merchant_id": "town-reference",
                            "currency": "USD"}},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 15.0,
                "max_response_bytes": 1_048_576},
        evaluator=STRICT_QUOTE_INTENT_EVALUATOR,
    ),
    "a2a-capability-fulfillment@0.1": PathProfile(
        profile_id="a2a-capability-fulfillment",
        version="0.1",
        protocol="a2a",
        capability="quote",
        request={"sku": "widget", "quantity": 2,
                 "unit_price_cents": 1995},
        expected={"total_cents": 3990, "terminal_fulfillments": 1},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 15.0},
        evaluator=PATH_EVALUATOR,
    ),
    "a2a-capability-fulfillment@0.2": PathProfile(
        profile_id="a2a-capability-fulfillment",
        version="0.2",
        protocol="a2a",
        capability="quote",
        request={"sku": "widget", "quantity": 2,
                 "unit_price_cents": 1995},
        expected={"total_cents": 3990, "terminal_fulfillments": 1},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 15.0, "max_response_bytes": 1_048_576},
        evaluator=PATH_EVALUATOR,
    ),
    "a2a-capability-fulfillment@0.3": PathProfile(
        profile_id="a2a-capability-fulfillment",
        version="0.3",
        protocol="a2a",
        capability="quote",
        request={"sku": "widget", "quantity": 2,
                 "unit_price_cents": 1995},
        expected={"total_cents": 3990, "terminal_fulfillments": 1},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 15.0,
                "max_response_bytes": 1_048_576},
        evaluator=STRICT_PATH_EVALUATOR,
    ),
    "a2a-order-conflict@0.1": PathProfile(
        profile_id="a2a-order-conflict",
        version="0.1",
        protocol="a2a",
        capability="quote",
        request={"sku": "widget", "quantity": 2,
                 "unit_price_cents": 1995},
        expected={"total_cents": 3990, "terminal_fulfillments": 1,
                  "retry_changes": {"quantity": 5}},
        controlled_condition="conflicting_retry",
        limits={"timeout_seconds": 15.0,
                "max_response_bytes": 1_048_576},
        evaluator=ORDER_CONFLICT_EVALUATOR,
    ),
    # A hiring agent: it trials candidate invoice-extraction agents on the
    # posted job and returns a ranked verdict. A live hire takes one to two
    # minutes, so the stock 15 s limit cannot fit it.
    "a2a-trial-then-hire@0.1": PathProfile(
        profile_id="a2a-trial-then-hire",
        version="0.1",
        protocol="a2a",
        capability="trial_then_hire",
        request={"job": _path_request("agent_hunter_invoice_clean.json")},
        expected={"job_status": "ranked", "min_ranked": 2,
                  "not_tested": TRIAL_HIRE_NOT_TESTED},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 240.0,
                "max_response_bytes": 1_048_576},
        evaluator=TRIAL_HIRE_EVALUATOR,
    ),
    # The failure case: a job that cannot be scored automatically must be
    # rejected before any candidate is paid for a trial.
    "a2a-trial-then-hire-unscorable@0.1": PathProfile(
        profile_id="a2a-trial-then-hire-unscorable",
        version="0.1",
        protocol="a2a",
        capability="trial_then_hire",
        request={"job": _path_request("agent_hunter_unscorable.json")},
        expected={"job_status": "rejected",
                  "not_tested": TRIAL_HIRE_NOT_TESTED},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 240.0,
                "max_response_bytes": 1_048_576},
        evaluator=TRIAL_HIRE_EVALUATOR,
    ),
    # A provider agent being hired: it extracts one pinned synthetic invoice
    # (tag agent-hunter-fixture-v1) into JSON matching the request's own
    # output_schema.
    "a2a-invoice-extraction@0.1": PathProfile(
        profile_id="a2a-invoice-extraction",
        version="0.1",
        protocol="a2a",
        capability="invoice_extraction",
        request=_path_request("agent_hunter_provider_task.json"),
        expected={"not_tested": INVOICE_EXTRACTION_NOT_TESTED},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 90.0,
                "max_response_bytes": 1_048_576},
        evaluator=INVOICE_EXTRACTION_EVALUATOR,
    ),
}

DEFAULT_PATH_PROFILE = "a2a-capability-fulfillment@0.3"


def get_path_profile(ref: str) -> PathProfile:
    if ref not in PATH_PROFILES:
        raise KeyError(f"no path profile {ref!r};"
                       f" available: {sorted(PATH_PROFILES)}")
    return PATH_PROFILES[ref]
