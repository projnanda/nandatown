"""Value-aware redaction: a secret copied under another field name.

redact.v1 matches field names only, so a chatty agent that pastes its
private api_key into a message note exports the key while the privacy
stage still reports a pass. redact.values.v1 also removes the declared
secret values, and the secret_leak validator detects canary-format
secrets in exported events.
"""

import os

import pytest
import yaml

from nandatown.bundle import verify_bundle
from nandatown.cli import main
from nandatown.layers.privacy import RedactionCollision
from nandatown.records import TownEvent
from nandatown.sim import agents as sim_agents
from nandatown.sim.runner import UnsupportedValue, build_engine, run_lab
from nandatown.sim.scenario import load_bundled
from nandatown.sim.validators import evaluate_scenario

KEY = "nt-canary-3f9a1c0b7e2d4a65"


class KeyChattyBuyer(sim_agents.ChattyBuyer):
    """Test-only: also uses its key as a JSON key, top-level and nested."""

    def quote_request_body(self):
        body = super().quote_request_body()
        key = self.config["api_key"]
        body[key] = "as a key"
        body["meta"] = {"seen": {f"my {key}": 1}}
        return body


class CollidingChattyBuyer(sim_agents.ChattyBuyer):
    """Test-only: a key that becomes '[redacted]' next to a literal one."""

    def quote_request_body(self):
        body = super().quote_request_body()
        body[self.config["api_key"]] = 1
        body["[redacted]"] = 2
        return body


class TupleChattyBuyer(sim_agents.ChattyBuyer):
    """Test-only: also sends its key inside a tuple, and observes one."""

    def quote_request_body(self):
        key = self.config["api_key"]
        self.api.observe("key_kept", self.name,
                         {"keys": (key, ("nested", key))})
        return dict(super().quote_request_body(), keys=(key,))


class OddValueChattyBuyer(sim_agents.ChattyBuyer):
    """Test-only: observes a value built from its key that JSON cannot
    hold exactly. Observations skip message signing, so the value
    reaches the export path."""

    odd = None

    def quote_request_body(self):
        self.api.observe("odd_value", self.name,
                         {"value": self.odd(self.config["api_key"])})
        return super().quote_request_body()


class NestedChattyBuyer(sim_agents.ChattyBuyer):
    """Test-only: its key is declared one level down, under credentials."""

    def quote_request_body(self):
        self.api.observe("secret_shared", self.name,
                         {"field": "api_key", "in_kind": "quote_request"})
        key = self.config["credentials"]["api_key"]
        return dict(sim_agents.Buyer.quote_request_body(self),
                    note=f"my key is {key}")


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


def test_value_rule_scrubs_the_key_when_used_as_a_nested_dict_key():
    engine = build_engine(load_bundled("secret_leak"))
    privacy = engine.layers["privacy"]
    record = {KEY: 1, "nested": [{"x": {f"my {KEY}": "v"}}]}
    assert privacy.redact(record) == {
        "[redacted]": 1, "nested": [{"x": {"my [redacted]": "v"}}]}


def test_value_rule_refuses_keys_that_collide_after_redaction():
    engine = build_engine(load_bundled("secret_leak"))
    privacy = engine.layers["privacy"]
    with pytest.raises(RedactionCollision) as caught:
        privacy.redact({"outer": {KEY: 1, "[redacted]": 2}})
    assert KEY not in str(caught.value)


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


def test_full_run_keeps_a_key_used_as_a_json_key_out_of_every_file(
        tmp_path, monkeypatch):
    monkeypatch.setitem(sim_agents.ROLES, "chatty_buyer", KeyChattyBuyer)
    bundle_dir, result = run_lab("secret_leak", str(tmp_path))
    assert result.verdict == "passed", statuses(result)
    assert files_containing(bundle_dir, KEY) == []
    assert verify_bundle(bundle_dir) == []


def test_full_run_on_name_only_rule_exports_the_key_used_as_a_json_key(
        tmp_path, monkeypatch):
    # Control: the test agent really puts the key into exported keys.
    monkeypatch.setitem(sim_agents.ROLES, "chatty_buyer", KeyChattyBuyer)
    bundle_dir, result = run_lab("secret_leak", str(tmp_path),
                                 layer_overrides={"privacy": "redact.v1"})
    assert statuses(result)["secret_withheld"] == "failed"
    assert files_containing(bundle_dir, KEY) == ["events.jsonl",
                                                 "intents.jsonl"]


def test_full_run_refuses_a_key_collision_and_writes_no_bundle(
        tmp_path, monkeypatch):
    monkeypatch.setitem(sim_agents.ROLES, "chatty_buyer",
                        CollidingChattyBuyer)
    out = tmp_path / "out"
    with pytest.raises(RedactionCollision):
        run_lab("secret_leak", str(out))
    assert not out.exists() or list(out.iterdir()) == []


def test_cli_run_and_compare_report_a_collision_as_a_refusal(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sim_agents.ROLES, "chatty_buyer",
                        CollidingChattyBuyer)
    out = tmp_path / "out"
    assert main(["run", "secret_leak", "--out", str(out)]) == 1
    printed = capsys.readouterr().out
    assert "run refused" in printed and "no bundle was written" in printed
    assert not out.exists() or list(out.iterdir()) == []

    assert main(["compare", "secret_leak", "--swap", "privacy=redact.v1",
                 "--out", str(tmp_path / "cmp")]) == 1
    printed += capsys.readouterr().out
    assert "comparison refused" in printed
    assert KEY not in printed


def write_scenario(directory, change):
    """secret_leak as a YAML file, changed by change(data)."""
    data = load_bundled("secret_leak").model_dump()
    change(data)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "scenario.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


def name_buyer_after_key(data):
    for agent in data["agents"]:
        if agent["role"] == "chatty_buyer":
            agent["name"] = KEY


KEY_IN_RUN_METADATA = {
    "agent name": lambda d: write_scenario(d, name_buyer_after_key),
    "scenario name": lambda d: write_scenario(
        d, lambda data: data.update(name=KEY)),
    "scenario path": lambda d: write_scenario(d / KEY, lambda data: None),
}


@pytest.mark.parametrize("where", sorted(KEY_IN_RUN_METADATA))
def test_full_run_keeps_a_key_in_run_metadata_out_of_every_file(
        tmp_path, where):
    scenario = KEY_IN_RUN_METADATA[where](tmp_path / "in")
    bundle_dir, result = run_lab(scenario, str(tmp_path / "out"))
    assert result.verdict == "passed", statuses(result)
    assert statuses(result)["leak_attempted"] == "passed"
    assert main(["visualize", bundle_dir]) == 0
    assert main(["receipt", bundle_dir]) == 0
    assert files_containing(bundle_dir, KEY) == []
    assert verify_bundle(bundle_dir) == []


def test_full_run_on_name_only_rule_exports_a_key_in_the_agent_name(
        tmp_path):
    # Control: the agent name really carries the key into run metadata.
    scenario = write_scenario(tmp_path / "in", name_buyer_after_key)
    bundle_dir, result = run_lab(scenario, str(tmp_path / "out"),
                                 layer_overrides={"privacy": "redact.v1"})
    assert statuses(result)["secret_withheld"] == "failed"
    hits = files_containing(bundle_dir, KEY)
    assert "run.json" in hits and "report.md" in hits
    assert verify_bundle(bundle_dir) == []


def test_full_run_keeps_a_key_in_a_tuple_out_of_every_file(
        tmp_path, monkeypatch):
    monkeypatch.setitem(sim_agents.ROLES, "chatty_buyer", TupleChattyBuyer)
    bundle_dir, result = run_lab("secret_leak", str(tmp_path))
    assert result.verdict == "passed", statuses(result)
    assert files_containing(bundle_dir, KEY) == []
    assert verify_bundle(bundle_dir) == []


def test_full_run_on_name_only_rule_judges_a_tuple_as_exported(
        tmp_path, monkeypatch):
    # Control: the tuple reaches both files, and the recorded result is
    # the one a replay of those files gives.
    monkeypatch.setitem(sim_agents.ROLES, "chatty_buyer", TupleChattyBuyer)
    bundle_dir, result = run_lab("secret_leak", str(tmp_path),
                                 layer_overrides={"privacy": "redact.v1"})
    assert statuses(result)["secret_withheld"] == "failed"
    assert files_containing(bundle_dir, KEY) == ["events.jsonl",
                                                 "intents.jsonl"]
    assert verify_bundle(bundle_dir) == []


ODD_VALUES = {
    "set": lambda key: {key},
    "bytes": lambda key: key.encode(),
    "nan": lambda key: float("nan"),
    "infinity": lambda key: float("inf"),
    "int key": lambda key: {1: key},
}


@pytest.mark.parametrize("kind", sorted(ODD_VALUES))
def test_full_run_refuses_a_value_json_cannot_hold_and_writes_no_bundle(
        tmp_path, monkeypatch, kind):
    role = type("OddBuyer", (OddValueChattyBuyer,),
                {"odd": staticmethod(ODD_VALUES[kind])})
    monkeypatch.setitem(sim_agents.ROLES, "chatty_buyer", role)
    out = tmp_path / "out"
    with pytest.raises(UnsupportedValue) as caught:
        run_lab("secret_leak", str(out))
    assert KEY not in str(caught.value)
    assert not out.exists() or list(out.iterdir()) == []


def test_cli_run_and_compare_report_an_unsupported_value_as_a_refusal(
        tmp_path, monkeypatch, capsys):
    role = type("OddBuyer", (OddValueChattyBuyer,),
                {"odd": staticmethod(ODD_VALUES["set"])})
    monkeypatch.setitem(sim_agents.ROLES, "chatty_buyer", role)
    out = tmp_path / "out"
    assert main(["run", "secret_leak", "--out", str(out)]) == 1
    printed = capsys.readouterr().out
    assert "run refused" in printed and "no bundle was written" in printed
    assert not out.exists() or list(out.iterdir()) == []

    assert main(["compare", "secret_leak", "--swap", "privacy=redact.v1",
                 "--out", str(tmp_path / "cmp")]) == 1
    printed += capsys.readouterr().out
    assert "comparison refused" in printed
    assert KEY not in printed


def test_full_run_refuses_participants_that_share_a_name_after_redaction(
        tmp_path):
    def collide(data):
        name_buyer_after_key(data)
        data["agents"][0]["name"] = "[redacted]"

    scenario = write_scenario(tmp_path / "in", collide)
    out = tmp_path / "out"
    with pytest.raises(RedactionCollision) as caught:
        run_lab(scenario, str(out))
    assert "participants" in str(caught.value)
    assert KEY not in str(caught.value)
    assert not out.exists() or list(out.iterdir()) == []


def nest_buyer_key(data):
    for agent in data["agents"]:
        if agent["role"] == "chatty_buyer":
            agent["config"]["credentials"] = {
                "api_key": agent["config"].pop("api_key")}


def test_value_rule_finds_secrets_in_nested_declared_fields():
    spec = load_bundled("secret_leak")
    buyer = next(a for a in spec.agents if a.role == "chatty_buyer")
    other = "nt-canary-0123456789abcdef"
    config = {k: v for k, v in buyer.config.items() if k != "api_key"}
    spec.agents[spec.agents.index(buyer)] = buyer.model_copy(update={
        "config": dict(config, credentials={"api_key": KEY},
                       backup={"api_key": [other]})})
    privacy = build_engine(spec).layers["privacy"]
    assert privacy.redact({"note": f"{KEY} and {other}"}) == {
        "note": "[redacted] and [redacted]"}


def test_full_run_keeps_a_nested_declared_key_out_of_every_file(
        tmp_path, monkeypatch):
    monkeypatch.setitem(sim_agents.ROLES, "chatty_buyer", NestedChattyBuyer)
    scenario = write_scenario(tmp_path / "in", nest_buyer_key)
    bundle_dir, result = run_lab(scenario, str(tmp_path / "out"))
    assert result.verdict == "passed", statuses(result)
    assert files_containing(bundle_dir, KEY) == []
    assert verify_bundle(bundle_dir) == []
