"""The gitmem compatibility check as pytest cases.

Not collected by Town's default test run (testpaths = tests). Run it
explicitly, with the pinned gitmem checkout:

    GITMEM_SRC=/path/to/nandatown-memory-mvp/memory_mvp \\
        python -m pytest -q examples/gitmem
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_check  # noqa: E402

GITMEM_SRC = os.environ.get("GITMEM_SRC", "")
pytestmark = pytest.mark.skipif(
    not (GITMEM_SRC and (Path(GITMEM_SRC) / "gitmem").is_dir()),
    reason="set GITMEM_SRC to a nandatown-memory-mvp memory_mvp directory")


@pytest.mark.parametrize("case", run_check.CASES, ids=lambda c: c.name)
def test_case_matches_expectation(case, tmp_path):
    sys.path.insert(0, GITMEM_SRC)
    report = run_check.run_case(case, tmp_path, GITMEM_SRC)
    assert "error" not in report, report
    assert run_check.actual_of(report) == run_check.expected_of(case), \
        case.why
