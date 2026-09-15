"""A recorded evaluator version keeps its rules when the current one moves.

Replay is only honest if 0.4.0 means tomorrow what it meant when a bundle
recorded it. These tests release a pretend next version and check that no
older version's judgment changes.
"""

import pytest

import nandatown.evaluator as evaluator_module
from nandatown.evaluator import evaluate

from test_evaluator import clean_events, profile
from test_evaluator_strictness import events_with, two_requests_one_answered

# Runs on which the historical rules disagree with one another, so a
# version that silently borrowed another's rules would be caught.
DIVERGENT = {
    "string applied flag": lambda: events_with(
        seller_note={"applied": "false", "total_cents": 3990}),
    "unanswered second request": two_requests_one_answered,
    "clean": clean_events,
}




@pytest.mark.parametrize("case", sorted(DIVERGENT))
@pytest.mark.parametrize("version", ["0.2.0", "0.3.0", "0.4.0"])
def test_releasing_a_new_version_changes_no_older_judgment(
        case, version, monkeypatch):
    before = evaluate(profile(), "run-1", DIVERGENT[case](), version=version)

    future = "9.9.9"
    rules = getattr(evaluator_module, "EVALUATOR_RULES", None)
    if rules is None:
        # Before rules were keyed by version, the only way to release one
        # was to move the constant and extend the tuple.
        monkeypatch.setattr(evaluator_module, "EVALUATOR_VERSIONS",
                            evaluator_module.EVALUATOR_VERSIONS + (future,))
    else:
        monkeypatch.setattr(evaluator_module, "EVALUATOR_RULES",
                            dict(rules, **{future: rules[
                                evaluator_module.EVALUATOR_VERSION]}))
        monkeypatch.setattr(evaluator_module, "EVALUATOR_VERSIONS",
                            tuple(evaluator_module.EVALUATOR_RULES))
    monkeypatch.setattr(evaluator_module, "EVALUATOR_VERSION", future)

    after = evaluate(profile(), "run-1", DIVERGENT[case](), version=version)

    assert after.model_dump(exclude={"evaluated_at"}) == before.model_dump(
        exclude={"evaluated_at"})


def test_every_released_version_has_rules():
    assert evaluator_module.EVALUATOR_VERSION in evaluator_module.EVALUATOR_RULES
    assert tuple(evaluator_module.EVALUATOR_RULES) == (
        evaluator_module.EVALUATOR_VERSIONS)
