"""Agents Gym scorecards as Town's trust layer (integrations/agents_gym).

A lender delegates SME loan file reviews to the candidate a trust layer
ranks highest. With agentsgym.attested.v1 the ranking comes from verified
Agents Gym scorecards; with reputation.v1 the same market hires the
cheapest reviewer, the fabricator, and fails on purpose. No network: the
cards are a fixture captured from the production gym, and the live source
is exercised against an in-process transport and a closed local port.
"""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import load_plugin_files, run_lab
from nandatown.sim.scenario import load_scenario_file

ROOT = Path(__file__).resolve().parent.parent / "integrations" / "agents_gym"
SCENARIOS = ROOT / "scenarios"


def _module(name):
    full = f"nandatown_agentsgym_{name}"
    if full not in sys.modules:
        spec = importlib.util.spec_from_file_location(full, ROOT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
    return sys.modules[full]


sc = _module("scorecard")
gs = _module("gym_source")
FIXTURE = json.loads((ROOT / "fixtures" / "sme_loan_cards.json").read_text())
AS_OF = FIXTURE["captured_at"]


def card(name):
    return copy.deepcopy(FIXTURE["candidates"][name]["signed_card"])


def run(scenario, tmp_path):
    bundle, result = run_lab(str(SCENARIOS / f"{scenario}.yaml"),
                             str(tmp_path))
    events = [json.loads(line) for line in
              (Path(bundle) / "events.jsonl").read_text().splitlines()]
    stages = {s.name: s.status for s in result.stages}
    return bundle, result, stages, events


def kinds(events, kind):
    return [e for e in events if e["kind"] == kind]


# -- Town's own reader of the card -------------------------------------


@pytest.mark.parametrize("name,score", [
    ("honest", 100), ("flaky", 90),
    ("fabricator", -1), ("shortcut", -1), ("lazy", -1),
])
def test_every_captured_card_verifies_and_scores_by_its_behavior(name, score):
    verdict = sc.verify(card(name), pinned_key=gs.PINNED_KEY, as_of=AS_OF)
    assert verdict.ok, verdict.reasons
    assert sc.score(sc.evidence(card(name))) == score


def test_town_reaches_the_gym_verdict_on_every_captured_card():
    for name, entry in FIXTURE["candidates"].items():
        ok = sc.verify(entry["signed_card"], pinned_key=gs.PINNED_KEY,
                       as_of=AS_OF).ok
        gym = entry["gym_verify"]
        assert ok == all(gym[level] == "pass" for level in
                         ("signature", "key_trusted", "time_window")), name


def test_erasing_the_fabrication_breaks_the_signature():
    forged = card("fabricator")
    forged["body"]["worlds"]["parent"]["behavior"]["fabrication"]["claims"] = 0
    verdict = sc.verify(forged, pinned_key=gs.PINNED_KEY, as_of=AS_OF)
    assert not verdict.ok
    assert verdict.checks["signature"] == "fail:bad_signature"


def test_a_card_under_another_key_is_not_trusted():
    other = "ed25519:" + "11" * 32
    verdict = sc.verify(card("honest"), pinned_key=other, as_of=AS_OF)
    assert verdict.checks == {"signature": "pass",
                              "key_trusted": "fail:untrusted_key",
                              "time_window": "pass",
                              "recomputed": "not_checked"}
    assert not verdict.ok


def test_an_expired_card_and_an_unpinned_key_are_not_passes():
    expired = sc.verify(card("honest"), pinned_key=gs.PINNED_KEY,
                        as_of="2027-01-01T00:00:00Z")
    assert expired.reasons == ["time_window:fail:expired"]
    unpinned = sc.verify(card("honest"), pinned_key=None, as_of=AS_OF)
    assert not unpinned.ok
    assert "key_trusted:not_checked" in unpinned.reasons


def test_canonical_bytes_refuse_floats_like_the_gym_does():
    with pytest.raises(sc.CanonicalError):
        sc.canonical_bytes({"score": 0.5})


# -- the live source ---------------------------------------------------


def _endpoint_response():
    runs = [{"candidate": n, "card": e["signed_card"], "verify": e["gym_verify"]}
            for n, e in FIXTURE["candidates"].items()]
    return {"runs": runs, "generatedAt": AS_OF[:-1] + ".000Z"}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_live_source_reads_the_public_endpoint_response():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_endpoint_response())

    source = gs.GymSource.live("https://gym.example/api/public/evaluate",
                               client=_client(handler))
    assert source.error is None
    assert seen["body"]["workflow"] == "sme_loan"
    assert sorted(source.cards) == sorted(gs.CANDIDATES)
    assert source.captured_at == AS_OF


@pytest.mark.parametrize("handler,error", [
    (lambda r: httpx.Response(429, json={"error": {"code": "rate_limited"}}),
     "gym_http_429"),
    (lambda r: httpx.Response(200, text="<html>login</html>"),
     "gym_response_unreadable"),
])
def test_live_source_failures_leave_no_cards(handler, error):
    source = gs.GymSource.live("https://gym.example", client=_client(handler))
    assert source.error == error
    assert source.cards == {}


def test_live_source_timeout_is_named():
    def handler(request):
        raise httpx.ReadTimeout("slow gym", request=request)

    source = gs.GymSource.live("https://gym.example", client=_client(handler))
    assert source.error == "gym_timeout"


# -- the scenarios end to end ------------------------------------------


def test_attested_trust_hires_only_on_clean_verified_cards(tmp_path):
    bundle, result, stages, events = run("attested_delegation", tmp_path)
    assert result.verdict == "passed", stages
    hired = {e["detail"]["to"] for e in kinds(events, "delegation_awarded")}
    assert hired == {"reviewer-honest"}
    assert len(kinds(events, "gap_caught")) == 3
    assert not kinds(events, "loan_defaulted")
    assert stages["recomputed_by_town"] == "not_tested"
    load_plugin_files([str(ROOT / "plugin.py")])
    assert verify_bundle(bundle) == []


def test_default_receipt_trust_hires_the_fabricator_and_fails(tmp_path):
    _, result, stages, events = run("attested_delegation_receipts_control",
                                    tmp_path)
    assert result.verdict == "failed"
    assert stages["no_delegation_against_evidence"] == "failed"
    assert stages["no_missed_gap_defaults"] == "failed"
    assert stages["cards_judged_by_town"] == "passed"
    hired = {e["detail"]["to"] for e in kinds(events, "delegation_awarded")}
    assert hired == {"reviewer-fabricator"}
    assert len(kinds(events, "loan_defaulted")) == 3


def test_the_two_runs_differ_only_in_the_trust_layer():
    a = load_scenario_file(str(SCENARIOS / "attested_delegation.yaml"))
    b = load_scenario_file(str(SCENARIOS /
                               "attested_delegation_receipts_control.yaml"))
    assert a.agents == b.agents and a.seed == b.seed
    assert {k: v for k, v in a.layers.items() if k != "trust"} == \
        {k: v for k, v in b.layers.items() if k != "trust"}
    assert (a.layers["trust"], b.layers["trust"]) == \
        ("agentsgym.attested.v1", "reputation.v1")


def test_a_tampered_card_earns_no_trust_and_no_work(tmp_path):
    _, result, stages, events = run("tampered_card", tmp_path)
    assert result.verdict == "passed", stages
    rejected = [e for e in kinds(events, "card_rejected")
                if e["subject"] == "reviewer-fabricator"]
    assert rejected[0]["detail"]["checks"]["signature"] == "fail:bad_signature"
    hired = {e["detail"]["to"] for e in kinds(events, "delegation_awarded")}
    assert "reviewer-fabricator" not in hired
    assert kinds(events, "gym_verdict_not_applicable")


def test_a_borrowed_card_is_refused_as_a_holder_mismatch(tmp_path):
    spec_path = tmp_path / "borrowed.yaml"
    text = (SCENARIOS / "tampered_card.yaml").read_text()
    spec_path.write_text(
        text.replace("present: tampered", "present: borrow:honest")
        .replace("- ../plugin.py", f"- {ROOT / 'plugin.py'}"))
    bundle, result = run_lab(str(spec_path), str(tmp_path / "runs"))
    events = [json.loads(line) for line in
              (Path(bundle) / "events.jsonl").read_text().splitlines()]
    assert result.verdict == "passed"
    rejected = [e for e in kinds(events, "card_rejected")
                if e["subject"] == "reviewer-fabricator"]
    assert rejected[0]["detail"]["reasons"] == ["holder_mismatch"]
    assert rejected[0]["detail"]["checks"]["signature"] == "pass"


def test_an_unreachable_gym_fails_closed(tmp_path):
    bundle, result, stages, events = run("gym_unreachable_fail_closed",
                                         tmp_path)
    assert result.verdict == "passed", stages
    assert kinds(events, "gym_unreachable")
    assert not kinds(events, "delegation_awarded")
    assert not kinds(events, "escrow_held")
    assert len(kinds(events, "delegation_refused")) == 8
    load_plugin_files([str(ROOT / "plugin.py")])
    assert verify_bundle(bundle) == []


def test_same_seed_same_delegations(tmp_path):
    first = run("attested_delegation", tmp_path / "a")[3]
    second = run("attested_delegation", tmp_path / "b")[3]

    def logical(events):
        # Run ids differ between attempts by design; the logic must not.
        return [(e["kind"], e["subject"], e["at"]) for e in events
                if e["kind"] not in ("run_created", "run_finished")]

    assert logical(first) == logical(second)
