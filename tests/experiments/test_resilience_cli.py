# SPDX-License-Identifier: MIT
# Copyright (c) 2026 nanda-resilience contributors
import json
import sys

from nandatown.experiments.resilience import cli


def test_cli_reports_invalid_input(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", [
        "nandatown-resilience", "run", "--disclosure", "2", "--out", str(tmp_path / "bad"),
    ])
    assert cli.main() == 1
    assert "error:" in capsys.readouterr().err
    assert not (tmp_path / "bad").exists()


def test_cli_custom_settings_are_reproducible(monkeypatch, capsys, tmp_path):
    out = tmp_path / "run"
    monkeypatch.setattr(sys, "argv", [
        "nandatown-resilience", "run", "--condition", "clean",
        "--buyers", "1", "--attempts", "1", "--dependency-capacity", "2",
        "--out", str(out),
    ])
    assert cli.main() == 0
    row = json.loads(capsys.readouterr().out)
    assert row["tasks"] == 20
    record = json.loads((out / "run.json").read_text())
    assert "--buyers 1" in record["config"]["rerun_command"]
    assert "--attempts 1" in record["config"]["rerun_command"]
    assert "--dependency-capacity 2" in record["config"]["rerun_command"]


def test_cli_refuses_existing_scenario(monkeypatch, capsys, tmp_path):
    recipe = tmp_path / "scenario.yaml"
    recipe.write_text("preserve")
    monkeypatch.setattr(sys, "argv", [
        "nandatown-resilience", "scenario", "--out", str(recipe),
    ])
    assert cli.main() == 1
    assert recipe.read_text() == "preserve"
    assert "error:" in capsys.readouterr().err


def test_cli_campaign_rejects_duplicate_seeds(monkeypatch, capsys, tmp_path):
    out = tmp_path / "campaign"
    monkeypatch.setattr(sys, "argv", [
        "nandatown-resilience", "campaign", "--seeds", "0", "0", "--out", str(out),
    ])
    assert cli.main() == 1
    assert "unique" in capsys.readouterr().err
    assert not out.exists()
