# SPDX-License-Identifier: Apache-2.0
"""Leal integration: a job agent gets Leal's recipe and follows it while it works.

Every test runs offline, from the recipe captured from Leal's live ``get_recipe`` MCP tool,
so it needs no Leal checkout and no model account.
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
    assert got["confirmation_recorded_word_for_word"] == "passed"
    assert verify_bundle(bundle) == []


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


def test_letters_name_the_employer_the_agent_found():
    source = sys.modules["nandatown_leal_leal_source"]
    facts = {"employer": "Northwind Bank", "role": "PM"}
    application = source.build_application("clean", facts)
    assert "Northwind Bank" in application["letter_text"]
    assert "{employer}" not in application["letter_text"]


def test_an_unknown_recipe_is_unavailable_not_invented():
    source = sys.modules["nandatown_leal_leal_source"]
    with pytest.raises(source.LealUnavailable):
        source.OfflineSource().get_recipe("tax-filing")
