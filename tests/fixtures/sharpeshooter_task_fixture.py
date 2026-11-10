"""Deterministic external-command fixture owned by adapter tests.

Tests install this source below a temporary project root with the current
Python interpreter in its shebang.  The executable name selects a bounded
failure mode; no ambient configuration is required.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


LIMITATIONS = [
    "The task is bounded and structured; it is not unrestricted prose research.",
    "Task content controls provenance and tested specifications, but the deterministic evaluator does not understand the economic thesis.",
    "Synthetic metrics do not validate strategy performance.",
    "Town proves transport and attributed requester checks; SharpeShooter proves its workflow/receipt; cross-system evidence correlates them.",
    "A receipt hash is tamper-evidence, not an independently signed truth attestation.",
    "The run does not prove real-agent reasoning, A2A/MCP conformance, hostile-code sandboxing, production readiness, or arbitrary interoperability.",
    "A `started` journal fails closed after interruption; v1 does not promise automatic project-level resumption.",
]


def canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value) -> str:
    data = value if isinstance(value, bytes) else canonical(value)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def argument(name: str) -> str:
    return sys.argv[sys.argv.index(name) + 1]


def main() -> int:
    db_path = Path(argument("--db"))
    mode = db_path.parent.name.removeprefix("runtime-")
    capture_path = Path(argument("--capture"))
    runtime = db_path.parent
    raw_task = sys.stdin.buffer.read()
    counter_path = runtime / "fixture-counter.txt"
    count = int(counter_path.read_text() or "0") + 1 if counter_path.exists() else 1
    counter_path.write_text(str(count), encoding="ascii")
    observation = {
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "stdin_sha256": digest(raw_task),
        "started_visible": (
            Path(os.environ["TMPDIR"]).parent / "sharpeshooter-journal-v1.json"
        ).exists(),
    }
    (runtime / "fixture-observation.json").write_bytes(canonical(observation))

    if mode == "nonzero":
        sys.stderr.write("fixture-secret-nonzero")
        return 7
    if mode == "timeout":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (runtime / "fixture-descendant.txt").write_text(str(child.pid), encoding="ascii")
        time.sleep(60)
    if mode == "stdout-oversize":
        sys.stdout.write("x" * 131_073)
        return 0
    if mode == "stderr-oversize":
        sys.stderr.write("x" * 16_385)
        return 7
    if mode == "both-oversize":
        sys.stderr.write("e" * 16_385)
        sys.stdout.write("o" * 131_073)
        return 7
    if mode == "malformed":
        sys.stdout.write("{not-json")
        return 0
    if mode == "project-oversize":
        sys.stdout.write("x" * 65_537)
        return 0
    if mode == "stdout-limit":
        sys.stdout.write("x" * 131_072)
        return 0

    task = json.loads(raw_task)
    task_digest = digest(task)
    selected = next(
        candidate for candidate in task["candidates"]
        if candidate["candidate_id"] == task["selected_candidate_id"]
    )
    candidates = [
        {
            "candidate_id": candidate["candidate_id"],
            "is_run_id": f"{index + 10:032x}",
            "strategy_hash": digest(candidate["strategy_spec"]),
        }
        for index, candidate in enumerate(task["candidates"])
    ]
    selected_hash = digest(selected["strategy_spec"])
    receipt = {
        "schema_version": "1.0",
        "receipt_id": "8" * 32,
        "issued_at": "2026-11-09T00:00:00Z",
        "idea": {
            "idea_id": "1" * 32,
            "family_id": "2" * 32,
            "operator_id": "9" * 32,
            "author_agent_id": "5" * 32,
        },
        "strategy": {
            "version_id": "a" * 32,
            "strategy_hash": selected_hash,
            "frozen_spec": selected["strategy_spec"],
            "frozen_at": "2026-11-09T00:00:00Z",
        },
        "evaluation": {
            "run_id": "4" * 32,
            "split": "oos",
            "family_oos_trial_number": 1,
            "dataset_version": "dv003",
            "dataset_split_hash": digest({"split": "oos"}),
            "evaluator_name": "deterministic_synthetic_stub",
            "evaluator_version": "1",
            "synthetic_metrics": True,
            "metrics": {"observations": 100},
            "checks": {"synthetic_metrics": True},
        },
        "review": {
            "review_id": "7" * 32,
            "reviewer_agent_id": "6" * 32,
            "reviewer_operator_id": "b" * 32,
            "separation_rule": "distinct_agent",
            "assessment": "fixture review",
            "concerns": [],
            "recommendation": "accept",
        },
        "provenance": {
            "registration_event_id": 1,
            "approval_event_id": 2,
            "test_event_id": 3,
            "review_event_id": 4,
        },
    }
    receipt["provenance"]["padding"] = ""
    receipt["provenance"]["padding"] = "x" * (32_768 - len(canonical(receipt)))
    if mode == "receipt-oversize":
        receipt["provenance"]["padding"] += "x"
    result = {
        "schema_version": "sharpeshooter.research-result.v1",
        "client_task_id": task["client_task_id"],
        "task_digest": task_digest,
        "title": task["idea"]["title"],
        "hypothesis": task["idea"]["hypothesis"],
        "sources_digest": digest(task["idea"]["sources"]),
        "agent_runtime": "fake",
        "idea_id": "1" * 32,
        "family_id": "2" * 32,
        "candidate_results": candidates,
        "selected_candidate_id": task["selected_candidate_id"],
        "selected_is_strategy_hash": selected_hash,
        "oos_run_id": "4" * 32,
        "oos_strategy_hash": selected_hash,
        "family_oos_trial_number": 1,
        "budget_before": 0,
        "budget_after": 1,
        "author_agent_id": "5" * 32,
        "reviewer_agent_id": "6" * 32,
        "review_id": "7" * 32,
        "review_recommendation": "accept",
        "receipt_id": "8" * 32,
        "receipt": receipt,
        "receipt_integrity_hash": digest(receipt),
        "receipt_verification": "VERIFIED",
        "receipt_consumed_by_scout": True,
        "operator_approval": "approved",
        "operator_verdict": "accept",
        "synthetic_metrics": True,
        "limitations": LIMITATIONS,
    }
    if mode == "mismatch":
        result["task_digest"] = "sha256:" + "0" * 64
    if mode == "bad-receipt":
        result["receipt_integrity_hash"] = "sha256:" + "0" * 64
    if mode == "bad-receipt-correlation":
        receipt["idea"]["idea_id"] = "0" * 32
        result["receipt_integrity_hash"] = digest(receipt)
    if mode == "unknown-result":
        result["unexpected"] = True
    if mode == "capture-oversize":
        capture_path.write_bytes(b"x" * 32_769)
    elif mode == "unsafe-capture":
        capture_path.write_text(
            '{"token":"provider-secret-marker","path":"/private/runtime"}',
            encoding="utf-8",
        )
    else:
        capture_path.write_bytes(b"x" * 32_768)
    sys.stderr.write("e" * 16_384)
    sys.stdout.buffer.write(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
