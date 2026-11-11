"""FactoryBridge participant for the synthetic Nanda Town mailbox example.

This is a test adapter, not a manufacturing service. It executes the project
from a separate checkout; no project implementation is copied into Town.
"""
import json
import sys

import llm
from agents.planner import Planner
from bench.checker import check
from bench.order_io import parse_order
from bench.session import run_session
from marketplace.core import Marketplace
from simulator import World
from tests.simulator_fixture import config


def execute(request, *, live=False):
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return {"status": "rejected", "reason": "request_id required"}
    try:
        order = parse_order(request.get("order"))
    except (ValueError, TypeError) as exc:
        return {"request_id": request_id, "status": "rejected",
                "reason": str(exc), "simulation_started": False, "llm_usage": {"llm_calls": 0}}

    world = World.from_config(order, config(), seed=1, unreliability=0)
    marketplace = Marketplace(world, allow_broadcast=True)
    # Explicit deterministic test double. This is not an LLM or production run.
    original = llm.call_llm
    if not live:llm.call_llm = lambda *args: {
        "role": "assistant",
        "content": '{"plan_id":"plan-0","reason":"SCRIPTED TEST REVIEWER"}',
    }
    try:
        result = run_session(order, Planner(), world, marketplace,
                             check=check, checker_identity="bench.checker.check")
    finally:
        llm.call_llm = original
    if result["execution_failed"] or result.get("checker_error"):
        return {"request_id": request_id, "status": "error",
                "error": result.get("error") or result.get("checker_error"),
                "simulation_started": True, "llm_usage": result["llm_usage"]}
    return {"request_id": request_id,
            "status": "completed" if result["success"] else "not_fulfilled",
            "simulation_started": True, "simulation_only": True,
            "scripted_reviewer": not live, "llm_usage": result["llm_usage"], "model": result["model"], "verdict": result["independent_verdict"],
            "truth": result["truth"], "tool_usage": result["tool_usage"],
            "trace": result["trace"]}


if __name__ == "__main__":
    request = json.load(sys.stdin)
    json.dump(execute(request), sys.stdout, allow_nan=False)
    print()

