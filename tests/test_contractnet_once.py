"""contractnet.once.v1: one task_id names one task lifecycle.

A Town-native duplicate of the message that makes an issuer announce a
task (supply_chain's `order`) re-announces the same task_id and schedules a
second award. The one-shot policy must replay the identical announcement
and finalize the task at most once.
"""

import itertools
import json

import pytest

from nandatown.layers import resolve
from nandatown.records import canonical_json, fingerprint

POLICY = "contractnet.once.v1"


class Recorder:
    def __init__(self):
        self.events = []

    def emit(self, observer, kind, subject, detail=None):
        self.events.append({"observer": observer, "kind": kind,
                            "subject": subject, "detail": detail or {}})

    def count(self, kind):
        return sum(1 for e in self.events if e["kind"] == kind)


def test_duplicate_trigger_finalizes_task_at_most_once():
    eng = Recorder()
    coord = resolve("coordination", POLICY)(eng)
    spec = {"component": "axle"}

    assert coord.announce("maker", "supply-axle", spec, rule="lowest") is None
    # The duplicated trigger repeats the identical announcement.
    assert coord.announce("maker", "supply-axle", spec, rule="lowest") is None
    coord.bid("supply-axle", "axle-1", 500)
    coord.bid("supply-axle", "axle-2", 450)

    first = coord.award("supply-axle")
    # The duplicated trigger's scheduled award.
    second = coord.award("supply-axle")

    assert first == ("axle-2", 450)
    assert second is None
    assert eng.count("task_awarded") == 1
    assert eng.count("award_rejected") == 1
    assert eng.count("task_announced") == 1
    assert eng.count("announce_replayed") == 1


def only(eng, kind):
    matches = [e for e in eng.events if e["kind"] == kind]
    assert len(matches) == 1, (kind, matches)
    return matches[0]


def test_caller_mutation_after_announce_changes_neither_task_nor_evidence():
    eng = Recorder()
    coord = resolve("coordination", POLICY)(eng)
    spec = {"terms": {"qty": [2], "expedite": False}, "component": "axle"}
    original = canonical_json(spec)

    coord.announce("maker", "supply-axle", spec, rule="lowest")
    announced = only(eng, "task_announced")
    evidence = canonical_json(announced["detail"])

    # The caller keeps and later mutates its own nested spec.
    spec["terms"]["qty"].append(3)
    spec["terms"]["expedite"] = True
    mutated = json.loads(canonical_json(spec))

    task = coord.tasks["supply-axle"]
    assert canonical_json(task["spec"]) == original
    assert canonical_json(announced["detail"]) == evidence
    # Task state and the historical event do not share nested terms.
    assert task["spec"] is not announced["detail"]["spec"]

    # A fresh object with the original terms (keys now sorted) replays.
    coord.announce("maker", "supply-axle", json.loads(original), rule="lowest")
    assert eng.count("announce_replayed") == 1
    assert eng.count("announce_rejected") == 0

    # Terms matching only the mutated caller object are a conflict.
    coord.announce("maker", "supply-axle", mutated, rule="lowest")
    assert eng.count("announce_replayed") == 1
    assert eng.count("announce_rejected") == 1
    assert canonical_json(coord.tasks["supply-axle"]["spec"]) == original


@pytest.mark.parametrize("first, second",
                         list(itertools.permutations([True, 1, 1.0], 2)))
def test_replay_identity_is_canonical_json_not_python_equality(first, second):
    eng = Recorder()
    coord = resolve("coordination", POLICY)(eng)
    spec = {"component": "axle", "terms": {"qty": first}}
    reused = {"component": "axle", "terms": {"qty": second}}
    # Python equality conflates these; Town's canonical JSON does not.
    assert spec == reused
    assert canonical_json(spec) != canonical_json(reused)

    coord.announce("maker", "supply-axle", spec, rule="lowest")
    coord.announce("maker", "supply-axle", reused, rule="lowest")

    assert eng.count("announce_replayed") == 0
    assert eng.count("announce_rejected") == 1
    assert (canonical_json(coord.tasks["supply-axle"]["spec"])
            == canonical_json(spec))


def test_rejection_evidence_is_a_snapshot_of_the_conflicting_terms():
    eng = Recorder()
    coord = resolve("coordination", POLICY)(eng)
    coord.announce("maker", "supply-axle",
                   {"component": "axle", "terms": {"qty": [2]}},
                   rule="lowest")
    stored = canonical_json(coord.tasks["supply-axle"]["spec"])
    conflicting = {"component": "axle", "terms": {"qty": [5]}}
    submitted = canonical_json(conflicting)

    coord.announce("maker", "supply-axle", conflicting, rule="lowest")
    rejected = only(eng, "announce_rejected")
    evidence = canonical_json(rejected["detail"])

    conflicting["terms"]["qty"].append(6)

    assert canonical_json(rejected["detail"]) == evidence
    assert canonical_json(rejected["detail"]["spec"]) == submitted
    assert canonical_json(coord.tasks["supply-axle"]["spec"]) == stored


def test_replay_event_records_terms_comparable_to_the_announcement():
    eng = Recorder()
    coord = resolve("coordination", POLICY)(eng)
    coord.announce("maker", "supply-axle",
                   {"component": "axle", "terms": {"qty": [2]}},
                   rule="lowest")
    replay = {"component": "axle", "terms": {"qty": [2]}}
    coord.announce("maker", "supply-axle", replay, rule="lowest")
    replay["terms"]["qty"].append(3)

    announced = only(eng, "task_announced")
    replayed = only(eng, "announce_replayed")
    assert replayed["detail"]["issuer"] == announced["observer"]
    # task_announced's detail is exactly {spec, rule}.
    assert (fingerprint({"spec": replayed["detail"]["spec"],
                         "rule": replayed["detail"]["rule"]})
            == fingerprint(announced["detail"]))
