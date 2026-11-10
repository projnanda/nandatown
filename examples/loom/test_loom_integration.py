"""End-to-end check: Nanda Town Lab <-> external LOOM portal.

    pip install "git+https://github.com/jasonpipi/loom-portal@v0.1.0"
    python -m pytest -q examples/loom

Starts a real LOOM portal on a free local port, runs both scenarios
through the Lab runner, and checks every stage. Lives under examples/
so Town's own CI (testpaths = ["tests"]) does not depend on LOOM.
"""

from __future__ import annotations

import os
import threading

import pytest

pytest.importorskip("loom_portal")

from loom_portal.server import serve

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import run_lab

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "loom_health_access.yaml")
DOWN = os.path.join(HERE, "loom_health_access_portal_down.yaml")

MAIN_STAGES = {
    "loom_reachable", "discovery", "decisions_match_policy",
    "minimum_disclosure", "overreach_contained", "expiry_enforced",
    "friction_not_depth", "idempotent_retry", "ledger_conserved",
    "privacy",
}
DOWN_STAGES = {"loom_unreachable_observed", "fail_closed",
               "ledger_conserved", "privacy"}


@pytest.fixture()
def portal(monkeypatch):
    server = serve(port=0, seed_credit={"cambridge-clinic": 85})
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("LOOM_URL", f"http://127.0.0.1:{server.server_port}")
    yield server
    server.shutdown()


def _statuses(result):
    return {s.name: s.status for s in result.stages}


def test_health_access_end_to_end(portal, tmp_path):
    bundle, result = run_lab(MAIN, str(tmp_path))
    statuses = _statuses(result)
    assert set(statuses) == MAIN_STAGES
    failed = {k: v for k, v in statuses.items() if v != "passed"}
    assert not failed, failed
    assert result.verdict == "passed"
    # The bundle is complete and the evaluator reproduces it once the
    # LOOM validators are registered (see verify_loom.py).
    assert verify_bundle(bundle) == []


def test_portal_down_fails_closed(tmp_path):
    # No portal fixture: the gateway points at a closed port.
    bundle, result = run_lab(DOWN, str(tmp_path))
    statuses = _statuses(result)
    assert set(statuses) == DOWN_STAGES
    assert all(v == "passed" for v in statuses.values()), statuses
    assert result.verdict == "passed"
    assert verify_bundle(bundle) == []
