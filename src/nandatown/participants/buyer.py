"""The stock buyer: discovers the seller, requests a quote, checks it.

The buyer's correctness assertion is its own attributed fact: it travels
as the note on the buyer's processed acknowledgement of the quote
response. The town records it but cannot synthesize it.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from ..client import TownClient
from ..records import ResearchTask
from ..research import (
    RESEARCH_CAPABILITY,
    RESEARCH_REQUEST_ID,
    ResearchResultError,
    build_research_request,
    validate_research_result,
)
from .base import Journal, run_loop

EXIT_CORRECT = 0
EXIT_INCORRECT = 4
EXIT_NO_RESPONSE = 5


def find_seller(client: TownClient, capability: str = "quote.read") -> str | None:
    for p in client.participants():
        if capability in p.get("capabilities", []):
            return p["name"]
    return None


def run(client: TownClient, name: str, token: str, state_dir: str,
        deadline_seconds: float = 25.0,
        grant_json: str | None = None) -> int:
    client.join_auto(name, token, grant_json)
    task = client.run_context["task"]
    if task.get("schema_version") == "sharpeshooter.research-task.v1":
        return _run_research(client, task, state_dir, deadline_seconds)
    seller = find_seller(client)
    if seller is None:
        return EXIT_NO_RESPONSE
    client.send(
        message_id="q-1", to=seller, kind="quote_request",
        body={"sku": task["sku"], "quantity": task["quantity"],
              "unit_price_cents": task["unit_price_cents"]},
    )
    journal = Journal(os.path.join(state_dir, "journal.db"))
    outcome: dict[str, Any] = {}

    def handler(claim: dict[str, Any]):
        if claim["kind"] != "quote_response":
            return "rejected", {"reason": "unknown kind"}, []
        if journal.seen(claim["message_id"]):
            return "processed", {"duplicate": True}, []
        total = claim["body"]["total_cents"]
        correct = total == task["expected_total_cents"]
        journal.record(claim["message_id"], {"correct": correct})
        outcome["correct"] = correct
        return "processed", {"correct": correct, "total_cents": total,
                             "expected_total_cents":
                                 task["expected_total_cents"]}, []

    deadline = time.time() + deadline_seconds
    run_loop(client, handler,
             until=lambda: "correct" in outcome or time.time() > deadline)
    if "correct" not in outcome:
        return EXIT_NO_RESPONSE
    return EXIT_CORRECT if outcome["correct"] else EXIT_INCORRECT


def _run_research(client: TownClient, task_document: dict[str, Any],
                  state_dir: str, deadline_seconds: float) -> int:
    """Request and independently validate one bounded reviewed result."""
    try:
        task = ResearchTask.model_validate(task_document)
    except Exception:
        return EXIT_INCORRECT
    seller = find_seller(client, RESEARCH_CAPABILITY)
    if seller is None:
        return EXIT_NO_RESPONSE
    request = build_research_request(task)
    client.send(
        message_id=RESEARCH_REQUEST_ID,
        to=seller,
        kind="research_request",
        body=request,
    )
    journal = Journal(os.path.join(state_dir, "journal.db"))
    outcome: dict[str, Any] = {}

    def handler(claim: dict[str, Any]):
        if claim["kind"] != "research_result":
            return "rejected", {"reason": "unknown kind"}, []
        if journal.seen(claim["message_id"]):
            return "processed", {"duplicate_recognized": True}, []
        try:
            assertions = validate_research_result(
                claim["body"], request_id=RESEARCH_REQUEST_ID,
                request=request)
        except ResearchResultError as exc:
            outcome["accepted"] = False
            return "rejected", {"reason": exc.code}, []
        journal.record(claim["message_id"], assertions)
        outcome["accepted"] = True
        return "processed", assertions, []

    deadline = time.time() + deadline_seconds
    run_loop(client, handler,
             until=lambda: "accepted" in outcome or time.time() > deadline)
    if "accepted" not in outcome:
        return EXIT_NO_RESPONSE
    return EXIT_CORRECT if outcome["accepted"] else EXIT_INCORRECT


def main() -> None:
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    code = run(client, env["NAME"], env["TOKEN"], env["STATE_DIR"],
               deadline_seconds=float(env.get("DEADLINE", "25")),
               grant_json=env.get("TOWN_GRANT"))
    sys.exit(code)


if __name__ == "__main__":
    main()
