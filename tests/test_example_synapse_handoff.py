"""Recheck the recorded synthetic handoff and catch missing or altered Town evidence."""

import importlib.util
import json
from pathlib import Path

import pytest


EXAMPLE = Path(__file__).resolve().parents[1] / "examples/integrations/synapse-handoff"
SPEC = importlib.util.spec_from_file_location("synapse_handoff_example", EXAMPLE / "run_check.py")
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.fixture
def cases():
    return json.loads((EXAMPLE / "evidence/local.json").read_text(encoding="utf-8"))["cases"]


def test_recorded_handoff_passes_offline(cases):
    assert [case["case"] for case in cases] == ["faithful", "altered"]
    for case in cases:
        runner.verify_case(case)


def test_recorded_handoff_rejects_missing_receipt_ack(cases):
    cases[0]["mailbox"].pop()
    with pytest.raises(ValueError, match="mailbox trace"):
        runner.verify_case(cases[0])


def test_recorded_handoff_rejects_accepted_tampering(cases):
    cases[1]["receipt"].update(result="matches_record", problems=[])
    with pytest.raises(ValueError, match="verification verdict"):
        runner.verify_case(cases[1])
