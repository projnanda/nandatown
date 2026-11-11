"""End-to-end check of Pearl Street's exchange inside the Town Lab.

    pip install -e .
    pip install --no-deps -r examples/pearl_street/requirements.txt
    python -m pytest -q examples/pearl_street/test_pearl_street.py

Runs pearl_street_peak.yaml once and compares the bundle with the values in
expected.json, which were worked by hand before the first run. The run's
verdict is FAILED on purpose: awards_within_attested_limits catches the
overpromiser that Pearl Street admits. The other seven scenario stages and
ledger_conserved must pass; Town's privacy check is not_tested because the
scenario declares no redacted fields. A separate test verifies the committed
evidence bundle.
"""

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("exchange.core", reason="install requirements.txt (Pearl Street's exchange)")

from nandatown.bundle import load_bundle, verify_bundle  # noqa: E402
from nandatown.sim.runner import load_plugin_files, run_lab  # noqa: E402
from nandatown.sim.scenario import load_scenario_file  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO = os.path.join(HERE, "pearl_street_peak.yaml")
EVIDENCE = os.path.join(HERE, "evidence", "sim-667774539952")
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


def test_committed_evidence_bundle_verifies():
    """The bundle in evidence/ still hashes and replays to its recorded result
    (and matches expected.json), once this process has the plugin files."""
    load_plugin_files([os.path.join(HERE, "pearl_street_town.py"),
                       os.path.join(HERE, "pearl_street_validator.py")])
    assert verify_bundle(EVIDENCE) == []
    result = json.load(open(os.path.join(EVIDENCE, "result.json"), encoding="utf-8"))
    assert {s["name"]: s["status"] for s in result["stages"]} == EXPECTED["stages"]


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


# ---- fault injection: pinned observations ---------------------------------
# Each test asserts what actually happens under one Town transport fault.
# Neither Pearl Street nor Town is patched. A test named "finding" pins harm
# to an honest provider.

def _stages_with(**changes):
    out = dict(EXPECTED["stages"])
    out.update(changes)
    return out


@pytest.fixture(scope="module")
def faults(tmp_path_factory):
    out = {}
    for key in ("drop", "duplicate", "delay"):
        home = tmp_path_factory.mktemp(f"home-{key}")
        old = os.environ.get("NANDATOWN_HOME")
        os.environ["NANDATOWN_HOME"] = str(home)
        try:
            path = os.path.join(HERE, f"pearl_street_peak_{key}.yaml")
            bundle_dir, result = run_lab(path, str(tmp_path_factory.mktemp(f"runs-{key}")))
        finally:
            if old is None:
                os.environ.pop("NANDATOWN_HOME", None)
            else:
                os.environ["NANDATOWN_HOME"] = old
        out[key] = (result, load_bundle(bundle_dir)["events"])
    return out


def test_fault_variants_reuse_the_base_roster():
    import yaml

    def load(name):
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return yaml.safe_load(f)

    base = load("pearl_street_peak.yaml")
    keep = {k: v for k, v in base.items() if k not in ("name", "description", "faults")}
    for key in ("drop", "duplicate", "delay"):
        v = load(f"pearl_street_peak_{key}.yaml")
        assert v["validator"] == base["name"] and len(v["faults"]) == 1
        assert v["faults"][0]["action"] == key
        assert {k: x for k, x in v.items()
                if k not in ("name", "description", "faults", "validator")} == keep


def test_fault_drop_finding_lost_meter_reading_charges_an_honest_provider(faults):
    """FINDING. One of pvb-48's 12 readings never reaches the exchange.
    Pearl Street counts a missing reading as zero delivery, so the honest
    pvb-48 (it delivered 9,000 kW in every step, as the meter attested) is
    charged $2,250 at the price cap, its record falls to 11/12 and the
    utility rates it bad."""
    result, events = faults["drop"]
    dropped = _of(events, "message_dropped")
    sent = {e.subject: e for e in _of(events, "message_sent")}
    assert len(dropped) == 1 and dropped[0].at == 120.0
    assert sent[dropped[0].subject].detail["body"]["asset_id"] == "PVB_48/bess"
    attested = [e for e in _of(events, "receipt_attested")
                if e.subject == "pvb-48" and e.detail["claim"] == "meter_kw"]
    assert len(attested) == 12 and {e.detail["value"]["kw"] for e in attested} == {9000.0}
    s = {e.subject: e.detail for e in _of(events, "pearl_settle")}["pvb-48"]
    assert s["delivered_kw"] == pytest.approx(8250)
    assert s["charge_usd"] == pytest.approx(2250)
    assert s["new_delivery_record"] == pytest.approx(11 / 12)
    assert [e.detail["outcome"] for e in _of(events, "reputation_updated")
            if e.subject == "pvb-48"] == ["bad"]
    assert {st.name: st.status for st in result.stages} == _stages_with(
        settlement_matches_meter="failed", honest_providers_unharmed="failed")


def test_fault_duplicate_offer_is_counted_once(faults):
    """dc-08's offer arrives twice with the same msg_id. The exchange does
    not dedupe by id: the second copy is checked again and refused as
    outside_mandate (15,000 + 15,000 > the declared 19,020). Clearing,
    settlement and every stage match the fault-free run."""
    result, events = faults["duplicate"]
    copies = [e for e in _of(events, "pearl_message")
              if e.detail["from"] == "dc-08" and e.detail["msg_type"] == "offer"]
    assert len({e.subject for e in copies}) == 1
    assert [(e.detail["outcome"], e.detail.get("code")) for e in copies] == [
        ("accepted", None), ("rejected", "outside_mandate")]
    assert {e.subject: e.detail["accredited_kw"] for e in _of(events, "pearl_award")} \
        == EXPECTED["awards_kw"]
    assert {st.name: st.status for st in result.stages} == EXPECTED["stages"]


def test_duplicate_offer_within_the_mandate_is_overwritten_not_double_counted():
    """The same duplicate when the mandate has room for both copies, probed on
    Pearl Street's exchange directly: the second copy is accepted silently and
    replaces the first under the same msg_id, so it is still counted once.
    Dedupe is a side effect of keying offers by msg_id, not a check."""
    from exchange import messages as M
    from exchange.core import Exchange

    ex = Exchange("run", "OP", "meter", "clock", ["SYS"])
    t0, gate = "2023-01-02T06:00:00Z", "2023-01-02T07:00:00Z"
    win = {"start_utc": "2023-01-02T12:00:00Z", "end_utc": "2023-01-02T12:30:00Z"}

    def send(kind, sender, payload, msg_id, at=t0):
        raw = M.encode(M.make(kind, sender, "exchange", at, payload, msg_id, "run"))
        return [M.decode(r) for r in ex.handle(raw)]

    for a in ("A", "B"):
        send("asset_limits", "meter", {"asset_id": f"{a}/bess", "usable_kwh": 999, "power_kw": 100,
                                       "pv_kwh_per_day": 0, "owner": f"{a} LLC", "agent_id": a}, f"m:{a}")
        send("register", a, {"agent_id": a, "legal_entity": f"{a} LLC",
                             "assets": [{"asset_id": f"{a}/bess", "zone": "SYS", "type": "bess"}],
                             "mandate": {"max_kw": 100, "price_floor": 0, "workload_limits": None}},
             f"{a}:r")
    send("post_need", "OP", {"zone": "SYS", "window": win, "kw_needed": 60, "mode": "scheduled",
                             "price_cap": 50, "gate_utc": gate}, "OP:n")
    offer = {"need_id": "OP:n", "mode": "scheduled", "zone": "SYS", "window": win, "kw": 40,
             "floor_usd_mwh": 10, "standing": False}
    assert send("offer", "A", offer, "A:o1") == []
    assert send("offer", "A", offer, "A:o1") == []        # the duplicate: accepted silently
    assert send("offer", "B", dict(offer, floor_usd_mwh=20), "B:o1") == []
    awards = [m["payload"] for m in send("tick", "clock", {}, "c:1", at=gate) if m["msg_type"] == "award"]
    assert [(a["offer_id"], a["accredited_kw"]) for a in awards] == [("A:o1", 40.0), ("B:o1", 20.0)]


def test_fault_delay_arrival_time_decides_and_the_offer_is_refused(faults):
    """dc-08 sends its offer at 20:20 UTC, 40 minutes before the 21:00 gate;
    the transport delivers it at 21:05. The bridge stamps arrival time (a
    design choice, so no agent can backdate), and Pearl Street's gate check
    reads that stamp, so the offer is refused as after_gate_closure. dc-08 is
    not charged or rated, but it loses the award: the need is filled to
    27,000 of 30,000 kW, and the overpromiser's $100 floor now sets the price."""
    result, events = faults["delay"]
    sent = [e for e in _of(events, "message_sent")
            if e.observer == "dc-08" and e.detail["kind"] == "offer"]
    assert len(sent) == 1 and sent[0].at == 20.0
    got = [e.detail for e in _of(events, "pearl_message")
           if e.detail["from"] == "dc-08" and e.detail["msg_type"] == "offer"]
    assert [(d["t_utc"], d["outcome"], d["code"]) for d in got] == [
        ("2023-07-25T21:05:00Z", "rejected", "after_gate_closure")]
    awards = {e.subject: e.detail for e in _of(events, "pearl_award")}
    assert {a: d["accredited_kw"] for a, d in awards.items()} == {"pvb-48": 9000, "pvb-35": 18000}
    assert {d["price_usd_mwh"] for d in awards.values()} == {100}
    assert {st.name: st.status for st in result.stages} == _stages_with(
        settlement_coverage="failed")


def test_fault_sweep_table_is_current():
    """fault_sweep_results.md is what fault_sweep.py produces now."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pearl_fault_sweep",
                                                  os.path.join(HERE, "fault_sweep.py"))
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)
    with open(sweep.OUT, encoding="utf-8") as f:
        assert sweep.render() == f.read()
