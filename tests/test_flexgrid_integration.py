"""FlexGrid on Town: a market day on a real run, every node an agent or every
node on rules.

These need a FlexGrid checkout and are skipped without ``FLEXGRID_HOME``; see
examples/flexgrid/README.md. They run FlexGrid's own nodes, buyers and
coordinator as separate processes, not a fixture of Town's. The rules day
takes about seven minutes. The checks on the recorded evidence need neither a
model nor a run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "flexgrid"
sys.path.insert(0, str(EXAMPLE))

import run_check  # noqa: E402

HOME = os.environ.get("FLEXGRID_HOME")
needs_home = pytest.mark.skipif(
    not HOME, reason="FLEXGRID_HOME (a FlexGrid checkout) is not set")
needs_evidence = pytest.mark.skipif(
    not (run_check.EVIDENCE / "summary.json").exists(),
    reason="no recorded evidence yet")


@pytest.fixture(autouse=True)
def isolated_town(tmp_path, monkeypatch):
    monkeypatch.setenv("NANDATOWN_HOME", str(tmp_path / "town_home"))
    monkeypatch.delenv("FLEXGRID_CONFIG_DIR", raising=False)


def recorded(to: Path) -> tuple[dict, dict[str, Path]]:
    """The summary, and every market bundle: as it stands if it's a plain one,
    otherwise out of the archive, unpacked here."""
    with tarfile.open(run_check.EVIDENCE / run_check.EXTRAS) as extras:
        extras.extractall(to, filter="data")
    summary = json.loads((run_check.EVIDENCE / "summary.json").read_text())
    plain = {name: run_check.EVIDENCE / name for name in summary["cases"]}
    return summary, {name: path if (path / "manifest.json").exists()
                     else to / name for name, path in plain.items()}


def lines(folder: Path, name: str) -> list[dict]:
    return [json.loads(x) for x in (folder / name).read_text().splitlines()]


@needs_evidence
def test_recorded_evidence_still_verifies(tmp_path):
    from nandatown.bundle import verify_bundle

    _, market = recorded(tmp_path)
    for bundle in market.values():
        assert verify_bundle(str(bundle)) == [], bundle


@needs_evidence
def test_recorded_summary_matches_its_bundles(tmp_path):
    summary, market = recorded(tmp_path)
    for name, case in summary["cases"].items():
        result = json.loads((market[name] / "result.json").read_text())
        assert result["verdict"] == case["town_run"]["verdict"], name
        assert result["run_id"] == case["town_run"]["run_id"], name
        assert case["ok"], name


@needs_evidence
def test_recorded_failure_shows_in_towns_own_log(tmp_path):
    """The repeated ids, from Town's event log, not from run_check's summary."""
    _, market = recorded(tmp_path)

    def refused(case):
        return sum(e["kind"] == "identity_reuse_rejected"
                   for e in lines(market[case], "events.jsonl"))

    assert refused("network_failure_before") > 0
    assert refused("network_failure") == 0


@needs_evidence
def test_recorded_rules_day_reads_back_from_towns_records():
    """Town's record alone holds the day: offers, readings, money, signatures."""
    folder = run_check.EVIDENCE / "normal_rules"
    view = run_check.town_view(lines(folder, "events.jsonl"),
                               lines(folder, "intents.jsonl"), 0)
    nodes = len(view["offers"])
    assert nodes == len(run_check.NODES) and view["bad_signatures"] == 0
    assert set(view["answers"].values()) == {"ACCEPTED"}
    for buyer in run_check.ORDERS:
        assert len(view["readings"][buyer]) == nodes * run_check.NORMAL_DAY.slots
        assert view["settlements"][buyer][-1]["net_zero_ok"]
    assert len(view["payments"]) == nodes


@needs_evidence
def test_recorded_agent_day_agrees_with_towns_record():
    """Each node's offer on Town's record, against its agent's receipt, and
    each cut against what its person heard. Needs the recording, not the
    model."""
    summary = json.loads((run_check.EVIDENCE / "summary.json").read_text())
    case = summary["cases"]["normal"]
    folder = run_check.EVIDENCE / "normal"
    view = run_check.town_view(lines(folder, "events.jsonl"),
                               lines(folder, "intents.jsonl"), 0)
    checks = run_check.agent_checks(
        run_check.CASES["normal"], view,
        {"answers": case["model_answers"], "chains": case["model_chains"]},
        case["agents"], list(run_check.NODES))
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]


@needs_evidence
def test_recorded_free_text_from_the_nodes_is_where_the_readme_says(tmp_path):
    """What the coordinator hears beyond the amount, from Town's record. A
    string with a space is free text, unless it's a time. On the current code a
    node sends it only in these places, a shortfall's reason is one of a fixed
    list, and one offer says why it left a part out. The recorded check "heard
    only the amount" reads just the keys of offer.agent."""
    summary, market = recorded(tmp_path)
    a_time = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
    labels = {"the forecast changed", "the owner left the event",
              "the owner needs the car now", "its battery tripped",
              "the owner changed the comfort limit",
              "the building closed for the day",
              "an event was added in a sold zone", "an alarm is on",
              "a device is out", "the schedule changed",
              "an owner's need came first", "another cause"}
    allowed = {"beckn.on_discover.offer.forecast_method",
               "beckn.on_discover.offer.kept_out.why[]",
               "beckn.on_status.node_report.summary",
               "beckn.on_status.node_report.facts[].text",
               "beckn.rt_capacity.reason"}

    def strings(value, path):
        if isinstance(value, dict):
            for key, inner in value.items():
                yield from strings(inner, f"{path}.{key}")
        elif isinstance(value, list):
            for inner in value:
                yield from strings(inner, f"{path}[]")
        elif isinstance(value, str):
            yield path, value

    seen: Counter = Counter()
    for name in summary["cases"]:
        if name == "network_failure_before":        # the code of 6 Nov
            continue
        for row in lines(market[name], "intents.jsonl"):
            sent = row["payload"]
            if row["action"] != "send" or row["actor"] not in run_check.NODES:
                continue
            for path, text in strings(sent["body"].get("message") or {},
                                      sent["kind"]):
                if " " not in text or a_time.match(text):
                    continue
                seen[path] += 1
                assert path in allowed, (name, path, text)
                if path == "beckn.rt_capacity.reason":
                    assert text in labels, (name, text)
    assert seen["beckn.on_discover.offer.kept_out.why[]"] == 1


def agent_day(town_kw: float, wire: dict | None = None, told: bool = True,
              reason: str = "I keep some back for the morning.") -> tuple:
    """A small day: one node, its receipts, what its owner heard, and what
    Town carried."""
    node = "hh-039"
    offer = {"slots": [{"kw_offered": town_kw}, {"kw_offered": 2.0}],
             "agent": wire or {"decided_by": "llm", "decision": "offer"}}
    view = {"offers": {node: offer}, "offer_rounds": {node: [offer]},
            "answers": {node: "ACCEPTED"}}
    cut = {"point": "offer", "ts": "2026-06-28 10:00",
           "rule": {"kw_per_half_hour": [2.5, 2.5]},
           "chose": {"kw_per_half_hour": [2.0, 2.0]}, "less_than_rule": True,
           "by": "agent", "reason": reason, "facts": [], "flags": []}
    yes = {"point": "commitment", "ts": "2026-06-28 10:15", "rule": {},
           "chose": {"answer": "accept"}, "less_than_rule": False,
           "by": "agent", "reason": "Nothing changed.", "facts": [],
           "flags": []}
    heard = [{"ts": "2026-06-28 10:00", "point": "offer", "to": "owner",
              "text": "I offered 2.0 kW instead of 2.5 kW."}] if told else []
    now = {node: {"receipts": [cut, yes], "told": heard}}
    got = {"answers": [{"node": node, "step": "5 offer", "ms": 9000,
                        "decided": {"take_part": True}, "error": None}],
           "chains": {node: {"ok": True}}}
    return view, got, now, [node]


def test_agent_checks_are_not_vacuous():
    """The agent day's checks, on a day that holds and on four that don't."""
    case = run_check.CASES["normal"]

    def failed(view, got, now, nodes):
        checks = run_check.agent_checks(case, view, got, now, nodes)
        return [c["name"] for c in checks if not c["ok"]]

    assert not failed(*agent_day(2.0))
    assert any("the amount its agent chose" in n
               for n in failed(*agent_day(2.5)))
    assert any("heard only the amount" in n for n in failed(*agent_day(
        2.0, wire={"decided_by": "llm", "reason": "I keep some back."})))
    assert any("was told it" in n for n in failed(*agent_day(2.0, told=False)))
    assert any("was told it" in n for n in failed(*agent_day(2.0, reason="")))


def test_every_scenario_has_its_control_and_the_failure_its_before():
    names = set(run_check.CASES)
    for c in run_check.SCENARIOS:
        assert c.model and f"{c.name}_rules" in names
        assert not run_check.CASES[f"{c.name}_rules"].model
    before = run_check.CASES["network_failure_before"]
    assert before.code == "baseline" and before.passes == 2
    assert before.verdict == "failed" and not before.waits


@needs_home
def test_rules_market_day_end_to_end(tmp_path):
    a = argparse.Namespace(homes=list(run_check.HOMES), step=2.0,
                           work=str(tmp_path / "work"), model=None,
                           model_name=None, base_port=41700,
                           model_step=run_check.OLD_MODEL_STEP)
    out = tmp_path / "evidence"
    out.mkdir()
    r = run_check.run_market_case("normal_rules", a, {"current": Path(HOME)},
                                  out, run_check.take_ports(41700, 6))
    assert r["ok"], ([s for s in r["stages"] if not s["ok"]],
                     [c for c in r["checks"] if not c["ok"]],
                     r["town_run"]["stages"])
    assert r["town_run"]["proof"], r["town_run"]["proof_text"]
