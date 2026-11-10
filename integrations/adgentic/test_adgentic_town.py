"""Adgentic x Town compatibility checks (opt-in; needs an Adgentic checkout).

    ADGENTIC_DIR=/path/to/adgentic python -m pytest -q integrations/adgentic

Skipped when ADGENTIC_DIR is unset, so Town's own `pytest -q` (testpaths = tests)
is unaffected. Each test runs a real Track profile with the Adgentic exchange as the
buyer-side participant and checks the Town bundle and the Adgentic log.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_matrix  # noqa: E402

ADGENTIC_DIR = os.environ.get("ADGENTIC_DIR")
pytestmark = pytest.mark.skipif(not ADGENTIC_DIR, reason="set ADGENTIC_DIR to an Adgentic checkout")

CASES = {c["id"]: c for c in run_matrix.CASES}


def run_case(case_id: str, tmp_path: Path, monkeypatch) -> dict:
    import json
    import subprocess

    case = CASES[case_id]
    monkeypatch.setenv("NANDATOWN_HOME", str(tmp_path / "home"))
    for k, v in case["env"].items():
        monkeypatch.setenv(k, v)
    adg = Path(ADGENTIC_DIR).resolve()
    py = str(adg / ".venv" / "bin" / "python")
    cmd = f"{py} {Path(run_matrix.__file__).parent / 'adgentic_buyer.py'} --adgentic-dir {adg}"
    r = subprocess.run(["nandatown", "run", case["profile"], "--out", str(tmp_path / "runs"),
                        "--agent", f"buyer=cmd:{cmd}"], capture_output=True, text=True,
                       env=os.environ.copy())
    bundle = Path([l for l in r.stdout.splitlines() if l.startswith("Evidence bundle:")][-1]
                  .split(":", 1)[1].strip())
    row = run_matrix.check(case, bundle, py)
    assert row["match"], json.dumps(row, indent=1)
    return row


def test_clean_quote_becomes_accepted_adgentic_offer(tmp_path, monkeypatch):
    row = run_case("clean", tmp_path, monkeypatch)
    assert row["accepted_seller"] == "town-seller" and row["town_offer_review"] == "approved"


def test_seller_crash_is_absorbed_without_a_second_intent(tmp_path, monkeypatch):
    row = run_case("crash-restart", tmp_path, monkeypatch)
    assert row["intents"] == 1 and row["town_requests"] == 1 and row["acceptances"] == 1


def test_two_distinct_responses_still_one_offer(tmp_path, monkeypatch):
    row = run_case("amnesia-fresh-ids-drain", tmp_path, monkeypatch)
    assert row["town_responses_distinct"] == 2 and row["offers"] == 1


def test_short_adgentic_deadline_skips_recovering_seller(tmp_path, monkeypatch):
    row = run_case("crash-restart-timeout-1s", tmp_path, monkeypatch)
    assert row["town_timeouts"] == 1 and row["accepted_seller"] is None
