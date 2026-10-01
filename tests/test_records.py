import json

import pytest
from pydantic import ValidationError

from nandatown.records import (
    EvidenceResult,
    StageResult,
    TestProfile,
    TownEvent,
    canonical_json,
    fingerprint,
)


def sample_profile() -> TestProfile:
    return TestProfile(
        name="quote-clean",
        task={
            "kind": "quote",
            "sku": "widget",
            "quantity": 2,
            "unit_price_cents": 1995,
            "expected_total_cents": 3990,
        },
        roles={"buyer": "buyer", "seller": "seller"},
        capabilities={"buyer": [], "seller": ["quote.read"]},
        fault="none",
        lease_seconds=5.0,
        evaluator="stage-evaluator",
    )


def test_fingerprint_stable_across_key_order():
    a = {"b": 1, "a": [1, 2, {"z": True, "y": None}]}
    b = {"a": [1, 2, {"y": None, "z": True}], "b": 1}
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a).startswith("sha256:")


def test_fingerprint_changes_with_content():
    assert fingerprint({"total_cents": 3990}) != fingerprint({"total_cents": 3991})


def test_canonical_json_is_compact_and_sorted():
    s = canonical_json({"b": 1, "a": 2})
    assert s == '{"a":2,"b":1}'


MAIN_PROFILE_FINGERPRINTS = {
    "quote-clean": "sha256:87a8ada4df2b3b2dc28fded719700e2db02559d8ed4e1647ffaf221e577eaa39",
    "quote-crash-restart": "sha256:a788b2fab17b08a8f6b6ba8d72a433861d5a88d2aee3713b598a747d0628ae36",
    "quote-drop-wakeup": "sha256:5ab74b4bd0a3b113845df7403b34650897f43c2e1aea1d85e6e672ac7a2719c4",
    "quote-duplicate-delivery": "sha256:649ac81897f7d652b41a86393afa52d301fe3197b8db3a3aaaab6e57c25cfca0",
    "quote-lost-ack": "sha256:68c9b4c2eab9177a86cc99b1e8089820c5857d78abdb0b67b6cd7ca5ec556702",
    "quote-llm": "sha256:1e0e144ee7c96edcccfabdd25817305df306892b86c11d0a28b5f030ee0321bb",
    "quote-llm-truncation": "sha256:123efd5c3fd6c30ce48ca2920343006be6834123034c9848c5460c04de98508b",
    "quote-llm-tool-error": "sha256:a18f78eed773ee4a14a1c0eb28f76366a980a97c1aad57cceedab856107a5a37",
}


def test_profiles_without_a_budget_keep_their_fingerprints():
    from nandatown.profiles import PROFILES

    assert {n: fingerprint(p.model_dump()) for n, p in PROFILES.items()
            if n in MAIN_PROFILE_FINGERPRINTS} == MAIN_PROFILE_FINGERPRINTS


def test_the_poison_control_is_the_same_recipe_without_the_budget():
    from nandatown.profiles import PROFILES

    bounded = PROFILES["quote-poison-request"].model_dump()
    control = PROFILES["quote-poison-unbounded"].model_dump()
    assert bounded["max_attempts"] == 3 and "max_attempts" not in control
    assert {**bounded, "name": "", "max_attempts": 0} == {
        **control, "name": "", "max_attempts": 0}


@pytest.mark.parametrize("value", [0, True, "3", 2.5])
def test_a_budget_must_be_a_positive_integer(value):
    with pytest.raises(ValidationError):
        TestProfile.model_validate({**sample_profile().model_dump(),
                                    "max_attempts": value})


def test_profile_round_trips_through_json():
    p = sample_profile()
    p2 = TestProfile.model_validate(json.loads(p.model_dump_json()))
    assert p2 == p
    assert fingerprint(p.model_dump()) == fingerprint(p2.model_dump())


def test_event_requires_observer_and_kind():
    with pytest.raises(ValidationError):
        TownEvent(event_id="ev-1", run_id="r", at=1.0, subject="q-1", detail={})


def test_evidence_result_verdict_fields():
    r = EvidenceResult(
        run_id="run-1",
        evaluator_version="0.2.0",
        stages=[StageResult(name="accepted", status="passed", evidence=["ev-1"])],
        verdict="passed",
        evaluated_at=1.0,
    )
    assert r.stages[0].status == "passed"
    with pytest.raises(ValidationError):
        StageResult(name="accepted", status="maybe")
