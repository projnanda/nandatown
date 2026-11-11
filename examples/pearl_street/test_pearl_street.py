"""End-to-end check of Pearl Street's exchange inside the Town Lab.

    pip install -e .
    pip install --no-deps -r examples/pearl_street/requirements.txt
    python -m pytest -q examples/pearl_street/test_pearl_street.py

Runs pearl_street_peak.yaml once and compares the bundle with the values in
expected.json, which were worked by hand before the first run. The run's
verdict is FAILED on purpose: awards_within_attested_limits catches the
overpromiser that Pearl Street admits. Every other stage must pass.
"""

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("exchange.core", reason="install requirements.txt (Pearl Street's exchange)")

from nandatown.bundle import load_bundle, verify_bundle  # noqa: E402
from nandatown.sim.runner import run_lab  # noqa: E402
from nandatown.sim.scenario import load_scenario_file  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO = os.path.join(HERE, "pearl_street_peak.yaml")
EXPECTED = json.load(open(os.path.join(HERE, "expected.json"), encoding="utf-8"))


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    home = tmp_path_factory.mktemp("town-home")
    old = os.environ.get("NANDATOWN_HOME")
    os.environ["NANDATOWN_HOME"] = str(home)
    try:
        bundle_dir, result = run_lab(SCENARIO, str(tmp_path_factory.mktemp("runs")))
    finally:
        if old is None:
            os.environ.pop("NANDATOWN_HOME", None)
        else:
            os.environ["NANDATOWN_HOME"] = old
    events = load_bundle(bundle_dir)["events"]
    return bundle_dir, result, events


def _of(events, kind):
    return [e for e in events if e.kind == kind]


def test_peak_event_end_to_end(run):
    bundle_dir, result, events = run

    # Stage verdicts: everything passes except the planted overpromise.
    assert {s.name: s.status for s in result.stages} == EXPECTED["stages"]
    assert result.verdict == EXPECTED["verdict"]

    # Boundary cases, as rejected by Pearl Street's exchange.
    rejected = {e.detail["from"]: (e.detail["msg_type"], e.detail["code"])
                for e in _of(events, "pearl_message") if e.detail["outcome"] == "rejected"}
    assert rejected == {p: (r["msg_type"], r["code"]) for p, r in EXPECTED["rejections"].items()}

    # Uniform-price clearing.
    awards = {e.subject: e.detail for e in _of(events, "pearl_award")}
    assert {a: d["accredited_kw"] for a, d in awards.items()} == EXPECTED["awards_kw"]
    assert {d["price_usd_mwh"] for d in awards.values()} == {EXPECTED["uniform_price_usd_mwh"]}

    # Settlement against the meter.
    settles = {e.subject: e.detail for e in _of(events, "pearl_settle")}
    assert set(settles) == set(EXPECTED["settlement"])
    for agent, want in EXPECTED["settlement"].items():
        for k, v in want.items():
            assert settles[agent][k] == pytest.approx(v, abs=1e-6), (agent, k)

    # Ledger: transfers and final balances in cents.
    transfers = {f"{e.detail['from']}->{e.detail['to']}": e.detail["cents"]
                 for e in _of(events, "payment_settled")}
    assert transfers == EXPECTED["ledger_cents"]["transfers"]
    balances = {e.subject: e.detail["balance_cents"] for e in _of(events, "account_opened")}
    for e in _of(events, "payment_settled"):
        balances[e.detail["from"]] -= e.detail["cents"]
        balances[e.detail["to"]] += e.detail["cents"]
    assert balances == EXPECTED["ledger_cents"]["final_balances"]

    # Town reputation after the event.
    scores = {}
    for e in _of(events, "reputation_updated"):
        scores[e.subject] = e.detail["score"]
    assert scores == EXPECTED["reputation"]

    # What the overpromise cost the grid at peak.
    delivered = sum(d["delivered_kw"] for d in settles.values())
    assert delivered == pytest.approx(EXPECTED["peak"]["delivered_kw"])
    assert EXPECTED["peak"]["needed_kw"] - delivered == pytest.approx(EXPECTED["peak"]["short_kw"])

    # The bundle verifies in this process (the plugin files are loaded here).
    assert verify_bundle(bundle_dir) == []


def test_scenario_limits_come_from_the_engie_slice():
    slice_ = json.load(open(os.path.join(HERE, "engie_slice.json"), encoding="utf-8"))
    spec = load_scenario_file(SCENARIO)
    meter = next(a for a in spec.agents if a.role == "meter")
    by_entity = {a["asset_id"].split("/")[0]: a for a in meter.config["assets"]}
    for asset in slice_["assets"]:
        a = by_entity[asset["entity_id"]]
        assert (a["power_kw"], a["usable_kwh"]) == (asset["power_kw"], asset["usable_kwh"])


def test_known_gap_fresh_process_verify_does_not_load_plugin_files(run, tmp_path):
    """Town's `nandatown verify` replays a Lab bundle without loading the
    scenario's plugin_files, so in a new process the pearl_street_peak
    validator is not registered and replay reports a mismatch. Pinned here so
    the gap is visible; if Town starts loading plugin_files, this test fails
    and should be removed."""
    bundle_dir, _, _ = run
    cli = "import sys; from nandatown.cli import main; sys.exit(main())"
    out = subprocess.run([sys.executable, "-c", cli, "verify", bundle_dir],
                         capture_output=True, text=True,
                         env=dict(os.environ, NANDATOWN_HOME=str(tmp_path)))
    assert out.returncode == 1
    assert "evaluator replay mismatch" in out.stdout
