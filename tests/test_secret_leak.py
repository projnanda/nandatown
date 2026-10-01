"""Value-aware redaction: a secret copied under another field name.

redact.v1 matches field names only, so a chatty agent that pastes its
private api_key into a message note exports the key while the privacy
stage still reports a pass. redact.values.v1 also removes the declared
secret values, and the secret_leak validator detects canary-format
secrets in exported events.
"""

import os

import pytest

from nandatown.bundle import verify_bundle
from nandatown.records import TownEvent
from nandatown.sim.runner import build_engine, run_lab
from nandatown.sim.scenario import load_bundled
from nandatown.sim.validators import evaluate_scenario

KEY = "nt-canary-3f9a1c0b7e2d4a65"


def statuses(result):
    return {s.name: s.status for s in result.stages}


def files_containing(bundle_dir, text):
    hits = []
    for root, _dirs, files in os.walk(bundle_dir):
        for name in files:
            path = os.path.join(root, name)
            with open(path, "rb") as f:
                if text.encode() in f.read():
                    hits.append(os.path.relpath(path, bundle_dir))
    return sorted(hits)


def test_value_rule_keeps_the_key_out_of_every_file(tmp_path):
    bundle_dir, result = run_lab("secret_leak", str(tmp_path))
    assert result.verdict == "passed", statuses(result)
    assert statuses(result)["secret_withheld"] == "passed"
    assert files_containing(bundle_dir, KEY) == []
    assert verify_bundle(bundle_dir) == []


def test_name_only_control_fails_for_the_predicted_reason(tmp_path):
    bundle_dir, result = run_lab("secret_leak_name_only", str(tmp_path))
    got = statuses(result)
    assert result.verdict == "failed"
    assert got["leak_attempted"] == "passed"
    assert got["secret_withheld"] == "failed"
    assert got["trade_completed"] == "passed"
    # The false pass this contribution exposes: the name-only stage
    # still says the declared field never left the run.
    assert got["privacy"] == "passed"
    assert files_containing(bundle_dir, KEY) == ["events.jsonl",
                                                 "intents.jsonl"]
    assert verify_bundle(bundle_dir) == []


def test_value_rule_scrubs_the_key_inside_text_only():
    engine = build_engine(load_bundled("secret_leak"))
    privacy = engine.layers["privacy"]
    record = {"note": f"my key is {KEY}, use it", "api_key": KEY,
              "quantity": 2, "sku": "widget"}
    assert privacy.redact(record) == {
        "note": "my key is [redacted], use it", "api_key": "[redacted]",
        "quantity": 2, "sku": "widget"}


def test_value_rule_leaves_numbers_and_short_strings_name_only():
    spec = load_bundled("secret_leak")
    buyer = next(a for a in spec.agents if a.name == "buyer-1")
    spec.agents[spec.agents.index(buyer)] = buyer.model_copy(
        update={"config": dict(buyer.config, api_key="short",
                               budget_cents=10000)})
    spec.redact_fields = ["api_key", "budget_cents"]
    privacy = build_engine(spec).layers["privacy"]
    assert privacy.secrets == []
    assert privacy.redact({"note": "short", "balance_cents": 10000}) == {
        "note": "short", "balance_cents": 10000}


@pytest.mark.parametrize("name", ["marketplace", "auction", "voting",
                                  "consensus", "supply_chain",
                                  "capability_spoofing"])
def test_value_rule_changes_nothing_in_other_scenarios(name):
    def run(privacy_plugin):
        spec = load_bundled(name)
        spec.layers["privacy"] = privacy_plugin
        engine = build_engine(spec)
        engine.run()
        return statuses(evaluate_scenario(spec, engine.run_id,
                                          engine.events))

    assert run("redact.values.v1") == run("redact.v1")


def test_secret_withheld_needs_an_attempt():
    spec = load_bundled("secret_leak")
    finished = TownEvent(event_id="ev-1", run_id="r", at=1.0,
                         observer="town", kind="run_finished",
                         subject="r", detail={})
    result = evaluate_scenario(spec, "r", [finished])
    assert statuses(result)["secret_withheld"] == "not_enough_evidence"


def test_a_canary_planted_in_a_passing_trace_fails():
    spec = load_bundled("secret_leak")
    engine = build_engine(spec)
    engine.run()
    events = [TownEvent.model_validate(
        engine.layers["privacy"].redact(e.model_dump()))
        for e in engine.events]
    assert statuses(evaluate_scenario(spec, "r", events))[
        "secret_withheld"] == "passed"
    planted = events[-1].model_copy(update={
        "detail": {"note": "nt-canary-0123456789abcdef"}})
    tampered = events[:-1] + [planted]
    assert statuses(evaluate_scenario(spec, "r", tampered))[
        "secret_withheld"] == "failed"
