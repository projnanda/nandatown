# SPDX-License-Identifier: Apache-2.0
"""Leal integration: a job agent gets Leal's recipe, follows it, and Leal checks its work.

Every test runs offline, from answers captured from Leal's live ``get_recipe``,
``check_application`` and ``leal_public_key`` MCP tools, so it needs no Leal checkout and
no model account.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files, run_lab

INTEGRATION = Path(__file__).resolve().parent.parent / "integrations" / "leal"
SCENARIOS = INTEGRATION / "scenarios"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("NANDATOWN_LEAL_MODE", raising=False)
    load_plugin_files([str(INTEGRATION / "plugin.py")])


def stages(result) -> dict[str, str]:
    return {s.name: s.status for s in result.stages}


def notes(result) -> dict[str, str]:
    return {s.name: s.note for s in result.stages}


def test_agent_follows_the_recipe_and_applies(tmp_path):
    bundle, result = run_lab(str(SCENARIOS / "recipe_application.yaml"), str(tmp_path))
    got = stages(result)
    assert result.verdict == "passed", notes(result)
    assert got["recipe_served_by_leal"] == "passed"
    assert got["recipe_followed_in_order"] == "passed"
    assert notes(result)["recipe_followed_in_order"] == (
        "screen > research > tailor_resume > write_letter > self_check > submit"
    )
    assert got["recipe_self_check_passed"] == "passed"
    assert got["employer_verified_leal_attestation"] == "passed"
    assert got["confirmation_recorded_word_for_word"] == "passed"
    assert verify_bundle(bundle) == []


def _scenario_with(tmp_path, old: str, new: str) -> Path:
    path = tmp_path / "variant.yaml"
    text = (SCENARIOS / "recipe_application.yaml").read_text(encoding="utf-8")
    assert old in text
    text = text.replace(old, new)
    text = text.replace("../plugin.py", str(INTEGRATION / "plugin.py").replace("\\", "/"))
    path.write_text(text, encoding="utf-8")
    return path


def _events(bundle: str, kind: str) -> list[dict]:
    lines = (Path(bundle) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["kind"] == kind]


def test_leal_refuses_an_invented_number_and_nothing_is_submitted(tmp_path):
    """The letter says 900+ where the confirmed fact is 700+: Leal refuses, the agent holds."""
    path = _scenario_with(tmp_path, "documents: clean", "documents: invented_number")
    bundle, result = run_lab(str(path), str(tmp_path / "runs"))
    got = stages(result)
    assert got["recipe_self_check_passed"] == "failed"
    assert "900+" in notes(result)["recipe_self_check_passed"]
    assert got["confirmation_recorded_word_for_word"] != "passed"
    assert _events(bundle, "application_received") == []
    assert result.verdict != "passed"


def test_without_an_answer_from_leal_the_agent_does_not_submit(tmp_path, monkeypatch):
    """Fail closed: if Leal cannot check the application, nothing goes out in the candidate's name."""
    source = sys.modules["nandatown_leal_leal_source"]

    def down(self, profile_id, packet):
        raise source.LealUnavailable("Leal is down")

    monkeypatch.setattr(source.OfflineSource, "check_application", down)
    bundle, result = run_lab(str(SCENARIOS / "recipe_application.yaml"), str(tmp_path))
    assert stages(result)["recipe_self_check_passed"] == "failed"
    assert "Leal is down" in notes(result)["recipe_self_check_passed"]
    assert _events(bundle, "application_received") == []


def test_employer_rejects_an_application_changed_after_leal_checked_it():
    source = sys.modules["nandatown_leal_leal_source"]
    offline = source.OfflineSource()
    facts = {"employer": "Acme Payments", "role": "Product Manager, Payments",
             "posting_text": _acme_posting()}
    packet = source.build_application("clean", facts)
    attestation = offline.check_application(source.PROFILE_ID, packet)["attestation"]
    key = offline.public_key()
    assert source.attestation_problem(attestation, packet, key) is None

    changed = dict(packet, letter_text=packet["letter_text"] + " I led a team of 40.")
    assert source.attestation_problem(attestation, changed, key) == (
        "the application changed after Leal checked it"
    )
    assert source.attestation_problem(attestation, packet, "00" * 32) == (
        "signed by a key that is not Leal's"
    )
    forged = dict(attestation, signature="00" * 64)
    assert source.attestation_problem(forged, packet, key) == "the signature does not verify"
    assert source.attestation_problem(None, packet, key) == "no Leal attestation attached"


def _acme_posting() -> str:
    import yaml

    spec = yaml.safe_load((SCENARIOS / "recipe_application.yaml").read_text(encoding="utf-8"))
    return next(a for a in spec["agents"] if a["role"] == "employer")["config"]["posting_text"]


def test_an_application_leal_never_saw_is_unavailable_not_invented():
    source = sys.modules["nandatown_leal_leal_source"]
    facts = {"employer": "Acme Payments", "role": "Product Manager, Payments",
             "posting_text": "Some other posting"}
    with pytest.raises(source.LealUnavailable):
        source.OfflineSource().check_application(
            source.PROFILE_ID, source.build_application("clean", facts)
        )


def test_recipe_stops_the_agent_before_a_job_it_cannot_get(tmp_path):
    bundle, result = run_lab(str(SCENARIOS / "sponsorship_bar.yaml"), str(tmp_path))
    got = stages(result)
    assert result.verdict == "passed", notes(result)
    assert got["recipe_agent_stopped_at_screening"] == "passed"
    assert "0 documents written" in notes(result)["recipe_agent_stopped_at_screening"]
    assert got["agent_without_recipe_applied_and_was_rejected"] == "passed"
    assert verify_bundle(bundle) == []


def test_the_screening_stage_fails_if_the_recipe_has_no_screening_step(tmp_path, monkeypatch):
    """The stage measures the recipe, not a hard-coded rule in the agent."""
    source = sys.modules["nandatown_leal_leal_source"]
    real = source.OfflineSource.get_recipe

    def without_screen(self, recipe_id):
        recipe = json.loads(json.dumps(real(self, recipe_id)))
        recipe["steps"] = [s for s in recipe["steps"] if s["id"] != "screen"]
        return recipe

    monkeypatch.setattr(source.OfflineSource, "get_recipe", without_screen)
    _, result = run_lab(str(SCENARIOS / "sponsorship_bar.yaml"), str(tmp_path))
    assert stages(result)["recipe_agent_stopped_at_screening"] == "failed"
    assert result.verdict == "failed"


def test_without_leal_the_agent_still_applies_and_says_so(tmp_path):
    """Leal missing from the registry: the agent works without a recipe and records it."""
    path = tmp_path / "no_leal.yaml"
    text = (SCENARIOS / "recipe_application.yaml").read_text(encoding="utf-8")
    text = text.replace("  - name: leal\n    role: leal_recipes\n    config:\n      balance_cents: 0\n", "")
    text = text.replace("../plugin.py", str(INTEGRATION / "plugin.py").replace("\\", "/"))
    path.write_text(text, encoding="utf-8")
    _, result = run_lab(str(path), str(tmp_path / "runs"))
    assert stages(result)["recipe_served_by_leal"] == "not_enough_evidence"
    assert result.verdict != "passed"


def test_the_captured_recipe_is_what_leal_serves():
    data = json.loads((INTEGRATION / "fixtures" / "recipe.json").read_text(encoding="utf-8"))
    recipe = data["recipes"]["job-applications"]
    assert recipe["price"] == "free"
    assert recipe["steps"][0]["id"] == "screen"
    assert "will not be providing visa sponsorship" in recipe["steps"][0]["stop_phrases"]
    assert data["leal_commit"]
    assert len(data["public_key"]) == 64
    outcomes = {c["packet"]: c["response"]["decision"]["outcome"] for c in data["checks"].values()}
    assert outcomes == {"clean": "approve", "invented_number": "refuse"}


def test_letters_name_the_employer_the_agent_found():
    source = sys.modules["nandatown_leal_leal_source"]
    facts = {"employer": "Northwind Bank", "role": "PM", "posting_text": "A PM role."}
    application = source.build_application("clean", facts)
    assert "Northwind Bank" in application["letter_text"]
    assert "{employer}" not in application["letter_text"]


def test_an_unknown_recipe_is_unavailable_not_invented():
    source = sys.modules["nandatown_leal_leal_source"]
    with pytest.raises(source.LealUnavailable):
        source.OfflineSource().get_recipe("tax-filing")
