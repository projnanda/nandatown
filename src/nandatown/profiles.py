"""Built-in test profiles: the boring quote task under five conditions.

The task is not the demo. Custody, recovery, stale-attempt rejection,
correlation, and correctness are the demo.
"""

from __future__ import annotations

from .records import TestProfile

_TASK = {
    "kind": "quote",
    "sku": "widget",
    "quantity": 2,
    "unit_price_cents": 1995,
    "expected_total_cents": 3990,
}

_ROLES = {"buyer": "buyer", "seller": "seller"}
_CAPABILITIES = {"buyer": [], "seller": ["quote.read"]}

_RESEARCH_TASK = {
    "schema_version": "sharpeshooter.research-task.v1",
    "client_task_id": "nanda-hw8-momentum-1",
    "idea": {
        "title": "Point-in-time cross-sectional momentum",
        "hypothesis": "Past relative strength predicts next-period cross-sectional ranking.",
        "rationale": "Compare two prespecified long-only momentum variants under the same deterministic synthetic evaluator.",
        "sources": [{
            "uri": "https://doi.org/10.1111/j.1540-6261.1993.tb04702.x",
            "label": "Jegadeesh and Titman (1993)",
            "accessed_on": "2026-11-09",
        }],
    },
    "candidates": [
        {
            "candidate_id": "momentum-12-1",
            "strategy_spec": {
                "name": "12-1 monthly momentum",
                "universe": "point-in-time eligible equities",
                "signal": "trailing 12-month total return excluding most recent month",
                "entry_rule": "enter top decile",
                "exit_rule": "exit below top quintile",
                "position_sizing": "equal weight, long only",
                "rebalance": "monthly",
                "parameters": {
                    "lookback_months": 12,
                    "skip_recent_months": 1,
                    "entry_quantile": 0.9,
                    "exit_quantile": 0.8,
                    "max_position_weight": 0.1,
                },
            },
        },
        {
            "candidate_id": "momentum-6-1",
            "strategy_spec": {
                "name": "6-1 monthly momentum",
                "universe": "point-in-time eligible equities",
                "signal": "trailing 6-month total return excluding most recent month",
                "entry_rule": "enter top decile",
                "exit_rule": "exit below top quintile",
                "position_sizing": "equal weight, long only",
                "rebalance": "monthly",
                "parameters": {
                    "lookback_months": 6,
                    "skip_recent_months": 1,
                    "entry_quantile": 0.9,
                    "exit_quantile": 0.8,
                    "max_position_weight": 0.1,
                },
            },
        },
    ],
    "selected_candidate_id": "momentum-6-1",
}

_RESEARCH_ROLES = {"buyer": "buyer", "seller": "seller"}
_RESEARCH_CAPABILITIES = {
    "buyer": [], "seller": ["research.reviewed-receipt"],
}


def _profile(name: str, fault: str, lease_seconds: float,
             runtimes: dict[str, str] | None = None) -> TestProfile:
    return TestProfile(name=name, task=_TASK, roles=_ROLES,
                       capabilities=_CAPABILITIES, fault=fault,
                       lease_seconds=lease_seconds,
                       evaluator="stage-evaluator",
                       runtimes=runtimes or {})


def _research_profile(name: str, fault: str) -> TestProfile:
    return TestProfile(
        name=name,
        task=_RESEARCH_TASK,
        roles=_RESEARCH_ROLES,
        capabilities=_RESEARCH_CAPABILITIES,
        fault=fault,
        lease_seconds=45.0,
        evaluator="sharpeshooter-research-0.1.0",
    )


_LLM = {"buyer": "llm", "seller": "llm"}

PROFILES: dict[str, TestProfile] = {
    "quote-clean": _profile("quote-clean", "none", 5.0),
    "quote-crash-restart": _profile("quote-crash-restart",
                                    "crash_after_claim", 1.5),
    "quote-amnesia-restart": _profile("quote-amnesia-restart",
                                      "crash_amnesia", 1.5),
    "quote-amnesia-fresh-ids": _profile("quote-amnesia-fresh-ids",
                                        "crash_amnesia_fresh_ids", 1.5),
    "quote-drop-wakeup": _profile("quote-drop-wakeup", "drop_wakeup", 5.0),
    "quote-duplicate-delivery": _profile("quote-duplicate-delivery",
                                         "duplicate_delivery", 5.0),
    "quote-lost-ack": _profile("quote-lost-ack", "lost_ack", 5.0),
    "quote-llm": _profile("quote-llm", "none", 5.0, _LLM),
    "quote-llm-truncation": _profile("quote-llm-truncation",
                                     "context_truncation", 5.0, _LLM),
    "quote-llm-tool-error": _profile("quote-llm-tool-error",
                                     "tool_error", 5.0, _LLM),
    "sharpeshooter-research-clean": _research_profile(
        "sharpeshooter-research-clean", "none"),
    "sharpeshooter-research-duplicate-delivery": _research_profile(
        "sharpeshooter-research-duplicate-delivery", "duplicate_delivery"),
}

DEFAULT_PROFILE = "quote-crash-restart"

FAULT_DESCRIPTIONS = {
    "quote-clean": "no fault; the calibration baseline",
    "quote-crash-restart": "the seller stops after claiming the work, the"
                           " stale attempt is fenced, the town redelivers",
    "quote-amnesia-restart": "the seller answers, then loses its journal"
                             " and dies before acknowledging; its second"
                             " application must not become a second"
                             " response",
    "quote-amnesia-fresh-ids": "negative control: the same fault with"
                               " response ids minted fresh per application;"
                               " FAILS on purpose with two responses",
    "quote-drop-wakeup": "the wake-up hint is lost; the durable inbox must"
                         " still deliver",
    "quote-duplicate-delivery": "the same work is offered twice; the seller"
                                " must apply it once",
    "quote-lost-ack": "the first acknowledgement is lost; the retry must be"
                      " safe",
    "quote-llm": "tier two: both participants run the model tool loop"
                 " (mock brain by default, --model for real inference)",
    "quote-llm-truncation": "tier two plus the first agent-native fault:"
                            " context truncated mid-run, the protocol must"
                            " carry the recovery",
    "quote-llm-tool-error": "tier two plus the second agent-native fault:"
                            " a tool result is lost mid-call and the agent"
                            " must notice and retry",
    "sharpeshooter-research-clean": "bounded deterministic research task"
                                    " through the external provider adapter",
    "sharpeshooter-research-duplicate-delivery": "the same research request"
                                                 " is offered twice; the"
                                                 " provider must replay one"
                                                 " result without rerunning",
}
