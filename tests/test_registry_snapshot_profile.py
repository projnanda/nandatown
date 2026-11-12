"""examples/registry_snapshot: a claim is checked against a registry record
that outlives the run; a former controller with valid signatures is refused
before any traffic, and the validator recomputes every decision."""

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from nandatown.layers import resolve
from nandatown.records import TownEvent, canonical_json, fingerprint
from nandatown.sim.engine import Engine
from nandatown.sim.runner import run_lab
from nandatown.sim.scenario import AgentSpec, ScenarioSpec, load_scenario_file
from nandatown.sim.validators import Trace

PROFILE = Path(__file__).resolve().parents[1] / "examples" / "registry_snapshot"
SCENARIOS = PROFILE / "scenarios"
MOCK = json.loads((PROFILE / "snapshots" / "mock.json").read_text(encoding="utf-8"))
IDENTITY = "mock:rotation-demo"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Importing registers the plugins, the role and the validator. A scenario
# naming the file in plugin_files imports it again; tests use the classes
# the layer registry resolves, not this module's copies.
rs = load(PROFILE / "registry_snapshot.py", "registry_snapshot_under_test")
sys.path.insert(0, str(PROFILE))
mock = load(PROFILE / "make_mock.py", "make_mock_under_test")
sign_claim = load(PROFILE / "sign_claim.py", "sign_claim_under_test")
KEYS = mock.keys()


def claim(key, agent, identity=IDENTITY):
    return mock.sign_claim(KEYS[key], agent, identity, "mock-registry")


CURRENT, FORMER = claim("k2", "provider-current"), claim("k1", "provider-former")


@pytest.fixture(autouse=True)
def real_plugins():
    """Re-register this checkout's profile before every test: a run of a
    copied profile (the missing-snapshot test) registers the copy's classes
    under the same ids."""
    load(PROFILE / "registry_snapshot.py", "registry_snapshot_registered")


def stages(result):
    return {s.name: s.status for s in result.stages}


def events_of(bundle):
    return [TownEvent.model_validate(json.loads(line)) for line in
            (Path(bundle) / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def scenario(tmp_path, registry="snapshot.mock.v1", current=CURRENT, former=FORMER,
             former_ask=1500, name="custom"):
    """A rotation scenario with chosen claims; the plugin path is absolute."""
    agents = []
    if current is not None:
        agents.append({"name": "provider-current", "role": "seller", "config": {
            "sku": "text-stats", "ask_cents": 1995, "floor_cents": 1700, "stock": 10,
            "balance_cents": 0, "claim": current}})
    if former is not None:
        agents.append({"name": "provider-former", "role": "former_controller", "config": {
            "sku": "text-stats", "ask_cents": former_ask, "floor_cents": former_ask - 300,
            "stock": 10, "balance_cents": 0, "claim": former}})
    agents.append({"name": "buyer-1", "role": "buyer", "config": {
        "sku": "text-stats", "quantity": 1, "cap_cents": 1900, "balance_cents": 10000,
        "rounds": 1}})
    doc = {"name": name, "seed": 42, "validator": "registry_rotation",
           "plugin_files": [str(PROFILE / "registry_snapshot.py")],
           "layers": {"identity": "claims.v1", "auth": "ed25519.v1", "registry": registry},
           "agents": agents, "max_time": 60}
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def run(path, tmp_path):
    bundle, result = run_lab(str(path), str(tmp_path / "out"))
    return bundle, result, load_scenario_file(str(path))


# -- whole runs ------------------------------------------------------------

def test_mock_pass(tmp_path):
    _, result = run_lab(str(SCENARIOS / "rotation_mock.yaml"), str(tmp_path))
    assert result.verdict == "passed"
    assert stages(result) == {
        "snapshot_loaded": "passed", "former_controller_refused": "passed",
        "former_controller_contained": "passed",
        "current_controller_verified": "passed",
        "current_controller_paid": "passed", "ledger_conserved": "passed",
        "privacy": "not_tested"}


def test_signature_only_control_fails_on_the_runs_evidence(tmp_path):
    bundle, result = run_lab(
        str(SCENARIOS / "rotation_mock_sigonly_control.yaml"), str(tmp_path))
    assert result.verdict == "failed"
    assert stages(result) == {
        "snapshot_loaded": "passed", "former_controller_refused": "not_tested",
        "former_controller_contained": "failed",
        "current_controller_verified": "not_tested",
        "current_controller_paid": "failed", "ledger_conserved": "passed",
        "privacy": "not_tested"}
    released = [e for e in events_of(bundle) if e.kind == "escrow_released"]
    assert [e.detail["to"] for e in released] == ["provider-former"]


def test_control_pays_the_current_controller_when_the_former_is_priced_out(tmp_path):
    """Under the control, current_controller_paid measures the run: with the
    former priced above the buyer's cap, the current is paid and it passes."""
    _, result, _ = run(scenario(tmp_path, "snapshot.mock.sigonly.v1", former_ask=5000),
                       tmp_path)
    assert stages(result)["current_controller_paid"] == "passed"
    assert stages(result)["former_controller_contained"] == "failed"  # listed and asked


def test_control_without_a_former_is_not_a_failure(tmp_path):
    _, result, _ = run(scenario(tmp_path, "snapshot.mock.sigonly.v1", former=None), tmp_path)
    assert result.verdict == "incomplete"
    assert stages(result)["current_controller_paid"] == "passed"


def test_wrapper_verifies_and_stock_verify_cannot(tmp_path):
    bundle, _ = run_lab(str(SCENARIOS / "rotation_mock.yaml"), str(tmp_path))
    wrapper = subprocess.run([sys.executable, str(PROFILE / "verify_bundle.py"), bundle],
                             capture_output=True, text=True)
    assert wrapper.returncode == 0, wrapper.stdout + wrapper.stderr
    stock = subprocess.run(
        [sys.executable, "-c", "import sys; from nandatown.cli import main;"
         " sys.exit(main(['verify', sys.argv[1]]))", bundle], capture_output=True, text=True)
    assert stock.returncode != 0


def test_wrapper_refuses_a_changed_profile(tmp_path):
    edited = "registry_snapshot.py"
    bundle, _ = run_lab(str(SCENARIOS / "rotation_mock.yaml"), str(tmp_path / "out"))
    copy_dir = tmp_path / "registry_snapshot"
    shutil.copytree(PROFILE, copy_dir, ignore=shutil.ignore_patterns("__pycache__"))
    target = copy_dir / edited
    target.write_text(target.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(copy_dir / "verify_bundle.py"), bundle],
                       capture_output=True, text=True)
    assert r.returncode == 3 and "version difference" in r.stdout


@pytest.mark.parametrize("name", [
    "auction", "capability_spoofing", "capability_spoofing_weak_auth",
    "capped_influence", "capped_influence_uncapped_control", "consensus",
    "marketplace", "registry_eviction", "registry_eviction_unowned",
    "supply_chain", "voting"])
def test_drop_in_identity_and_auth_keep_bundled_verdicts(tmp_path, name):
    """claims.v1 + ed25519.v1 in place of keys.v1 + the scenario's auth.
    Only capability_spoofing_weak_auth changes, by design: the swap replaces
    the plain.v1 it exists to demonstrate."""
    plugin = [str(PROFILE / "registry_snapshot.py")]
    _, base = run_lab(name, str(tmp_path / "base"), plugins=plugin)
    _, swapped = run_lab(name, str(tmp_path / "swap"), plugins=plugin,
                         layer_overrides={"identity": "claims.v1", "auth": "ed25519.v1"})
    if name == "capability_spoofing_weak_auth":
        assert (base.verdict, swapped.verdict) == ("failed", "passed")
    else:
        assert swapped.verdict == base.verdict


def test_mock_data_is_reproducible():
    for rel, render in mock.FILES.items():
        assert (PROFILE / rel).read_text(encoding="utf-8") == render(), rel


def test_sign_claim_uses_the_towns_canonical_json():
    assert sign_claim.canonical_json is canonical_json


def test_ed25519_auth_names_the_identity_it_needs():
    spec = ScenarioSpec(name="x", agents=[], layers={"auth": "ed25519.v1"})
    with pytest.raises(TypeError, match="claims.v1"):
        Engine(spec).layers["auth"].sign_as("a", {})


# -- the registry check, unit level ------------------------------------------

def town(*agents, registry="snapshot.mock.v1", snapshot=None):
    """Agents: (name, role, claim or None)."""
    spec = ScenarioSpec(
        name="registry-unit",
        agents=[AgentSpec(name=n, role=r, config={"claim": c} if c else {})
                for n, r, c in agents],
        layers={"identity": "claims.v1", "auth": "ed25519.v1", "registry": registry})
    engine = Engine(spec)
    if snapshot is not None:
        engine.layers["registry"].snapshot = snapshot
        engine.layers["registry"].problem = ""
    return engine


def publish(engine, name, card=None, signer=None, publisher=None, signature=None):
    identity, auth = engine.layers["identity"], engine.layers["auth"]
    card = card or identity.card(name, ["sell.text-stats"], {})
    sig = signature if signature is not None else auth.sign_as(signer or name, card)
    return engine.layers["registry"].publish(publisher or signer or name, card, sig)


def kinds(engine, kind):
    return [e for e in engine.events if e.kind == kind]


def test_current_resolves_and_former_is_refused_with_its_rotation():
    e = town(("provider-current", "seller", CURRENT),
             ("provider-former", "former_controller", FORMER))
    assert publish(e, "provider-current")
    assert not publish(e, "provider-former")
    resolved, = kinds(e, "registry_claim_resolved")
    refused, = kinds(e, "registry_claim_refused")
    record = MOCK["identities"][IDENTITY]
    assert resolved.detail["owner_pkh"] == record["owner_pkh"]
    assert refused.detail["reason"] == "controller_mismatch"
    assert refused.detail["rotation_tx"] == record["history"][1]["tx"]
    assert e.layers["registry"].names_with("sell.text-stats") == ["provider-current"]


def test_a_claim_is_recorded_as_resolved_only_once_it_is_listed():
    e = town(("provider-current", "seller", CURRENT))
    assert publish(e, "provider-current")
    assert not publish(e, "provider-current")  # same version: index.owned.v1 refuses
    assert len(kinds(e, "registry_claim_resolved")) == 1


def test_a_key_that_never_controlled_is_refused_without_a_rotation():
    e = town(("stranger", "seller", claim("k3", "stranger")))
    assert not publish(e, "stranger")
    refused, = kinds(e, "registry_claim_refused")
    assert (refused.detail["reason"], refused.detail["rotation_tx"]) == ("controller_mismatch", None)


def test_a_copied_controller_key_fails_on_the_claim_signature():
    copied = dict(claim("k3", "copycat"), controller_pub=CURRENT["controller_pub"])
    e = town(("copycat", "seller", copied))
    assert not publish(e, "copycat")
    assert kinds(e, "registry_claim_refused")[0].detail["reason"] == "claim_signature_invalid"


def test_a_claim_signed_for_another_agent_does_not_transfer():
    e = town(("thief", "seller", CURRENT))
    assert not publish(e, "thief")
    assert kinds(e, "registry_claim_refused")[0].detail["reason"] == "claim_signature_invalid"


def test_an_unconfigured_claim_is_refused_even_when_validly_signed():
    e = town(("rival", "seller", None))
    card = e.layers["identity"].card("rival", ["sell.text-stats"], {})
    card["facts"]["registry_claim"] = claim("k2", "rival")
    assert not publish(e, "rival", card=card)
    assert kinds(e, "registry_claim_refused")[0].detail["reason"] == \
        "claim_not_configured_for_agent"


def test_unknown_and_retired_identities_are_refused():
    snap = copy.deepcopy(MOCK)
    rec = snap["identities"][IDENTITY]
    rec["status"], rec["owner_pkh"] = "retired", None
    rec["history"].append({"event": "retired", "slot": 900, "block_height": 450,
                           "tx": "mock-retire",
                           "from_pkh": MOCK["identities"][IDENTITY]["owner_pkh"]})
    e = town(("provider-current", "seller", CURRENT), snapshot=snap)
    assert not publish(e, "provider-current")
    assert kinds(e, "registry_claim_refused")[0].detail["reason"] == "identity_retired"
    e2 = town(("provider-current", "seller", CURRENT), snapshot=dict(MOCK, identities={}))
    assert not publish(e2, "provider-current")
    assert kinds(e2, "registry_claim_refused")[0].detail["reason"] == "identity_unknown"


def test_an_edited_snapshot_is_unavailable_not_trusted(tmp_path):
    snap = copy.deepcopy(MOCK)
    snap["identities"][IDENTITY]["owner_pkh"] = snap["identities"][IDENTITY]["history"][0]["owner_pkh"]
    (tmp_path / "mock.json").write_text(json.dumps(snap), encoding="utf-8")
    cls = resolve("registry", "snapshot.mock.v1")
    loaded, problem = cls._load(cls.__new__(cls), tmp_path / "mock.json")
    assert loaded is None and "fingerprint" in problem


def test_an_inconsistent_pinned_snapshot_is_still_unavailable(tmp_path, monkeypatch):
    snap = copy.deepcopy(MOCK)
    snap["identities"][IDENTITY]["owner_pkh"] = snap["identities"][IDENTITY]["history"][0]["owner_pkh"]
    path = tmp_path / "mock.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    cls = resolve("registry", "snapshot.mock.v1")
    monkeypatch.setattr(cls, "pinned", fingerprint(snap))
    loaded, problem = cls._load(cls.__new__(cls), path)
    assert loaded is None and "does not match its own history" in problem


@pytest.mark.parametrize("edit,why", [
    (lambda r: r.update(owner_pkh=r["history"][0]["owner_pkh"]), "does not match its own history"),
    (lambda r: r["history"][1].update(slot=5000), "after the tip"),
    (lambda r: r["history"][1].update(slot=50), "out of order"),
    (lambda r: r["history"][1].update(from_pkh="ab" * 28), "not the controller"),
    (lambda r: r["history"].reverse(), "start with registered"),
    (lambda r: r["history"].insert(1, {"event": "teleported", "slot": 200}), "unknown event"),
    (lambda r: r["history"].insert(1, {"event": "retired", "slot": 200, "from_pkh": "x"}),
     "follows retirement"),
])
def test_an_inconsistent_record_is_rejected(edit, why):
    record = copy.deepcopy(MOCK["identities"][IDENTITY])
    edit(record)
    assert why in rs.replay_history(record, MOCK["tip"]["slot"])
    assert rs.replay_history(MOCK["identities"][IDENTITY], MOCK["tip"]["slot"]) == ""


def test_missing_snapshot_refuses_every_claim_as_unavailable():
    e = town(("provider-current", "seller", CURRENT))
    e.layers["registry"].snapshot, e.layers["registry"].problem = None, "gone"
    assert not publish(e, "provider-current")
    assert kinds(e, "registry_claim_refused")[0].detail["reason"] == "snapshot_unavailable"
    assert kinds(e, "registry_snapshot_unavailable")


def test_a_foreign_claim_cannot_rewrite_another_listing():
    """#303's invariant: a rival with its own valid claim republishes
    provider-current's card under that name; nothing about it changes."""
    e = town(("provider-current", "seller", CURRENT), ("rival", "seller", None))
    assert publish(e, "provider-current")
    card = e.layers["identity"].card("provider-current", [], {})
    card["facts"]["registry_claim"] = claim("k3", "provider-current")
    card["version"] = 2
    assert not publish(e, "provider-current", card=card, signer="rival")
    assert e.layers["registry"].names_with("sell.text-stats") == ["provider-current"]
    assert "provider-current" not in e.layers["registry"].unbound
    assert not kinds(e, "registry_claim_refused")


def test_a_third_party_cannot_unbind_by_publishing_in_anothers_name():
    """P6b: a card for provider-current with a broken claim and no town
    signature, published under its name, is never evaluated as a claim."""
    e = town(("provider-current", "seller", CURRENT), ("jammer", "seller", None))
    card = e.layers["identity"].card("provider-current", ["sell.text-stats"], {})
    card["facts"]["registry_claim"] = dict(CURRENT, signature="00" * 64)
    assert not publish(e, "provider-current", card=card, publisher="provider-current",
                       signature="")
    assert e.layers["registry"].unbound == {}
    assert not kinds(e, "registry_claim_refused")
    assert publish(e, "provider-current")


def test_a_third_party_cannot_write_the_formers_refusal():
    """P6c: someone else publishing the former's card with the former's
    public claim produces no claim event at all."""
    e = town(("provider-former", "former_controller", FORMER), ("framer", "seller", None))
    card = e.layers["identity"].card("provider-former", ["sell.text-stats"], {})
    assert not publish(e, "provider-former", card=card, signer="framer")
    assert not kinds(e, "registry_claim_refused")
    assert "provider-former" not in e.layers["registry"].unbound


def test_a_former_cannot_dodge_unbinding_with_another_publisher_name():
    """P7c: whose claim it is comes from the town signature, not the
    publisher argument; once unbound, a claimless card cannot list it."""
    e = town(("provider-former", "former_controller", FORMER))
    assert not publish(e, "provider-former", publisher="somebody-else")
    assert "provider-former" in e.layers["registry"].unbound
    bare = {"name": "provider-former", "agent_id": "aid-1",
            "capabilities": ["sell.text-stats"], "facts": {}}
    assert not publish(e, "provider-former", card=bare)


def test_a_copied_valid_claim_under_anothers_signature_records_nothing():
    """P8b: no resolution is recorded for a card that is never listed."""
    e = town(("provider-current", "seller", CURRENT), ("rival", "seller", None))
    card = e.layers["identity"].card("provider-current", ["sell.text-stats"], {})
    assert not publish(e, "provider-current", card=card, signer="rival")
    assert not kinds(e, "registry_claim_resolved")


def test_a_refused_sender_cannot_deliver_even_card_shaped_bodies():
    e = town(("provider-former", "former_controller", FORMER))
    assert not publish(e, "provider-former")
    body = e.layers["identity"].card("provider-former", ["sell.text-stats"], {})
    sig = e.layers["auth"].sign_as("provider-former", body)
    assert not e.layers["auth"].verify("provider-former", body, sig, subject="m-1")
    assert kinds(e, "registry_sender_unbound")[0].detail["sender"] == "provider-former"


def test_a_card_shaped_message_cannot_harm_another_agent():
    e = town(("provider-current", "seller", CURRENT), ("jammer", "seller", None))
    assert publish(e, "provider-current")
    body = {"name": "provider-current", "agent_id": "x", "capabilities": [],
            "facts": {"registry_claim": claim("k3", "provider-current")}}
    assert e.layers["auth"].verify("jammer", body, e.layers["auth"].sign_as("jammer", body))
    assert e.layers["registry"].unbound == {}


# -- the validator, on real and hand-built traces --------------------------

@pytest.fixture(scope="module")
def base(tmp_path_factory):
    spec = load_scenario_file(str(SCENARIOS / "rotation_mock.yaml"))
    bundle, result = run_lab(str(SCENARIOS / "rotation_mock.yaml"),
                             str(tmp_path_factory.mktemp("base")))
    assert result.verdict == "passed"
    return spec, events_of(bundle)


def judge(spec, events):
    return {s.name: s.status for s in rs.registry_rotation(spec, Trace(events))}


def ev(events, event_kind, **match):
    return next(e for e in events if e.kind == event_kind
                and all(e.detail.get(k) == v for k, v in match.items()))


def swap(events, old, **update):
    new = old.model_copy(update=update)
    return [new if e is old else e for e in events]


def detail_swap(events, old, **fields):
    return swap(events, old, detail=dict(old.detail, **fields))


def put_before(events, moving, anchor):
    out = [e for e in events if e is not moving]
    out.insert(out.index(anchor), moving)
    return out


def new_event(events, **kw):
    return TownEvent(event_id=f"ev-x{len(events)}-{kw.get('kind')}", run_id=events[0].run_id,
                     at=99.0, detail=kw.pop("detail", {}), **kw)


def repinned(events, monkeypatch, edit):
    """Edit the recorded snapshot, then pin the edit, so a test reaches the
    rules past the fingerprint check."""
    loaded = ev(events, "registry_snapshot_loaded")
    fields = {k: copy.deepcopy(loaded.detail.get(k)) for k in rs.SNAPSHOT_FIELDS}
    edit(fields)
    fp = fingerprint(fields)
    monkeypatch.setattr(resolve("registry", "snapshot.mock.v1"), "pinned", fp)
    return swap(events, loaded, detail=dict(loaded.detail, **fields, fingerprint=fp))


def test_validator_passes_the_real_trace(base):
    spec, events = base
    assert set(judge(spec, events).values()) == {"passed"}


def test_refusal_after_inbound_contact_fails(base):
    spec, events = base
    refusal = ev(events, "registry_claim_refused")
    contact = new_event(events, observer="buyer-1", kind="message_sent", subject="m-x",
                        detail={"to": "provider-former", "kind": "quote_request"})
    i = events.index(refusal)
    assert judge(spec, events[:i] + [contact] + events[i:])["former_controller_refused"] == "failed"


def test_refusal_after_accepted_outbound_traffic_fails(base):
    """V06: the former's own accepted message before its refusal counts."""
    spec, events = base
    refusal = ev(events, "registry_claim_refused")
    sent = new_event(events, observer="provider-former", kind="message_sent", subject="m-y",
                     detail={"to": "buyer-1", "kind": "quote_response"})
    delivered = new_event(events, observer="town", kind="message_delivered", subject="m-y",
                          detail={"to": "buyer-1"})
    i = events.index(refusal)
    assert judge(spec, events[:i] + [sent, delivered] + events[i:])[
        "former_controller_refused"] == "failed"


def test_accepted_traffic_from_the_former_fails_containment(base):
    spec, events = base
    sent = new_event(events, observer="provider-former", kind="message_sent", subject="m-z",
                     detail={"to": "buyer-1", "kind": "quote_response"})
    delivered = new_event(events, observer="town", kind="message_delivered", subject="m-z",
                          detail={"to": "buyer-1"})
    assert judge(spec, events + [sent, delivered])["former_controller_contained"] == "failed"
    dropped = new_event(events, observer="town", kind="delivery_failed", subject="m-z",
                        detail={"to": "buyer-1", "reason": "bad signature"})
    assert judge(spec, events + [sent, delivered, dropped])["former_controller_contained"] == "passed"


def test_a_settlement_or_a_listing_of_the_former_fails_containment(base):
    spec, events = base
    settled = new_event(events, observer="town", kind="payment_settled", subject="o",
                        detail={"to": "provider-former", "cents": 1})
    assert judge(spec, events + [settled])["former_controller_contained"] == "failed"
    listed = new_event(events, observer="town", kind="card_registered",
                       subject="provider-former", detail={"verified": True})
    assert judge(spec, events + [listed])["former_controller_contained"] == "failed"


def test_every_former_must_be_refused(base):
    spec, events = base
    two = spec.model_copy(deep=True)
    two.agents = list(two.agents) + [AgentSpec(name="former-2", role="former_controller",
                                               config={"claim": FORMER})]
    contact = new_event(events, observer="buyer-1", kind="message_sent", subject="m-w",
                        detail={"to": "former-2", "kind": "quote_request"})
    s = judge(two, events + [contact])
    assert (s["former_controller_refused"], s["former_controller_contained"]) == ("failed", "failed")


def test_no_former_means_not_enough_evidence(base):
    spec, events = base
    none = spec.model_copy(deep=True)
    none.agents = [a for a in none.agents if a.role != "former_controller"]
    s = judge(none, events)
    assert (s["former_controller_refused"], s["former_controller_contained"]) == \
        ("not_enough_evidence", "not_enough_evidence")


@pytest.mark.parametrize("field,value", [
    ("card_signature", "00" * 64),
    ("town_key", "00" * 32),
    ("signature", "00" * 64),
])
def test_a_refusal_whose_evidence_does_not_recompute_fails(base, field, value):
    spec, events = base
    trace = detail_swap(events, ev(events, "registry_claim_refused"), **{field: value})
    assert judge(spec, trace)["former_controller_refused"] == "failed"


def test_a_refusal_for_another_reason_does_not_count(base):
    spec, events = base
    trace = detail_swap(events, ev(events, "registry_claim_refused"),
                        reason="claim_signature_invalid")
    assert judge(spec, trace)["former_controller_refused"] == "failed"


def test_a_refused_key_that_never_controlled_does_not_count(tmp_path):
    """A real run in which the 'former' presents k3's claim: its refusal
    recomputes in every field, but the recorded history never moved control
    away from k3."""
    _, result, _ = run(scenario(tmp_path, former=claim("k3", "provider-former")), tmp_path)
    assert stages(result)["former_controller_refused"] == "failed"


def test_the_recorded_key_hash_must_hash_the_recorded_key(tmp_path):
    """V27: from the k3 run, only the recorded hash is changed to K1's (a
    former controller): every other check passes, the hash check fails."""
    bundle, _, spec = run(scenario(tmp_path, former=claim("k3", "provider-former")), tmp_path)
    events = events_of(bundle)
    trace = detail_swap(events, ev(events, "registry_claim_refused"),
                        controller_pkh=rs.key_hash(FORMER["controller_pub"]))
    assert judge(spec, trace)["former_controller_refused"] == "failed"


def test_a_refusal_against_a_retired_record_does_not_count(base, monkeypatch):
    """V28: _not_former requires the recorded identity to be active."""
    spec, events = base

    def retire(f):
        rec = f["identities"][IDENTITY]
        rec["history"].append({"event": "retired", "slot": 900, "block_height": 450,
                               "tx": "mock-retire", "from_pkh": rec["owner_pkh"]})
        rec["status"], rec["owner_pkh"] = "retired", None
    assert judge(spec, repinned(events, monkeypatch, retire))["former_controller_refused"] == "failed"


def test_the_refused_identity_must_be_the_configured_one(base, monkeypatch):
    """N8: every claimant is configured for mock:other, which the re-pinned
    snapshot holds; the recorded claims concern mock:rotation-demo."""
    spec, events = base
    other = spec.model_copy(deep=True)
    for a in other.agents:
        if "claim" in a.config:
            a.config["claim"] = dict(a.config["claim"], identity="mock:other")
    trace = repinned(events, monkeypatch, lambda f: f["identities"].update(
        {"mock:other": copy.deepcopy(f["identities"][IDENTITY])}))
    s = judge(other, trace)
    assert (s["former_controller_refused"], s["current_controller_verified"]) == ("failed", "failed")


def test_former_and_current_must_claim_one_identity(base, monkeypatch):
    spec, events = base
    split = spec.model_copy(deep=True)
    former = next(a for a in split.agents if a.role == "former_controller")
    former.config["claim"] = dict(former.config["claim"], identity="mock:other")
    trace = repinned(events, monkeypatch, lambda f: f["identities"].update(
        {"mock:other": copy.deepcopy(f["identities"][IDENTITY])}))
    assert set(list(judge(split, trace).values())[1:]) == {"not_enough_evidence"}


def test_a_resolution_without_its_listing_does_not_verify(base):
    spec, events = base
    trace = [e for e in events
             if not (e.kind == "card_registered" and e.subject == "provider-current")]
    assert judge(spec, trace)["current_controller_verified"] == "failed"


def test_a_resolution_must_name_the_recorded_controller(tmp_path):
    """V17: a sigonly run lists provider-current with K1's valid claim; recast
    as a resolution in a comparing run, its evidence recomputes, but K1 is
    not the recorded controller."""
    bundle, _, spec = run(scenario(tmp_path, "snapshot.mock.sigonly.v1",
                                   current=claim("k1", "provider-current"), former=None), tmp_path)
    events = events_of(bundle)
    trace = swap(events, ev(events, "registry_claim_unchecked"), kind="registry_claim_resolved")
    trace = detail_swap(trace, ev(trace, "registry_snapshot_loaded"), compare_records=True)
    assert judge(spec, trace)["current_controller_verified"] == "failed"


def test_a_resolution_whose_evidence_does_not_recompute_fails(base):
    """V18."""
    spec, events = base
    trace = detail_swap(events, ev(events, "registry_claim_resolved"), signature="00" * 64)
    assert judge(spec, trace)["current_controller_verified"] == "failed"


def test_a_second_key_for_the_same_agent_fails_verification(base):
    spec, events = base
    resolved = ev(events, "registry_claim_resolved")
    other = resolved.model_copy(update={"event_id": "ev-dup",
                                        "detail": dict(resolved.detail, controller_pkh="ab" * 28)})
    assert judge(spec, events + [other])["current_controller_verified"] == "failed"


def test_an_order_to_someone_else_does_not_pay_the_current(base):
    """V23."""
    spec, events = base
    trace = detail_swap(events, ev(events, "message_sent", kind="purchase_order"),
                        to="provider-former")
    assert judge(spec, trace)["current_controller_paid"] == "failed"


def test_payment_before_its_order_does_not_count(base):
    spec, events = base
    release = ev(events, "escrow_released", to="provider-current")
    order = ev(events, "message_sent", kind="purchase_order")
    assert judge(spec, put_before(events, release, order))["current_controller_paid"] == "failed"


def test_an_order_before_the_claim_was_listed_does_not_count(base):
    spec, events = base
    resolved = ev(events, "registry_claim_resolved")
    order = ev(events, "message_sent", kind="purchase_order")
    assert judge(spec, put_before(events, order, resolved))["current_controller_paid"] == "failed"


def test_without_a_snapshot_every_stage_is_not_enough_evidence(base):
    spec, events = base
    trace = swap(events, ev(events, "registry_snapshot_loaded"),
                 kind="registry_snapshot_unavailable", detail={"reason": "x"})
    assert set(judge(spec, trace).values()) == {"not_enough_evidence"}


def test_contradictory_snapshot_events_are_not_judged(base):
    spec, events = base
    loaded = ev(events, "registry_snapshot_loaded")
    also = loaded.model_copy(update={"event_id": "ev-also",
                                     "kind": "registry_snapshot_unavailable"})
    assert set(judge(spec, events + [also]).values()) == {"not_enough_evidence"}


def test_a_bundle_from_another_validator_version_is_not_judged(base):
    spec, events = base
    trace = detail_swap(events, ev(events, "registry_snapshot_loaded"),
                        validator_version="registry-rotation-0.1")
    assert set(judge(spec, trace).values()) == {"not_enough_evidence"}


def test_a_recorded_snapshot_that_does_not_rebuild_to_the_pin_is_an_error(base):
    """P14: the recorded tip is edited and the fingerprint string kept."""
    spec, events = base
    trace = detail_swap(events, ev(events, "registry_snapshot_loaded"),
                        tip={"slot": 999999, "block_height": 1, "block_hash": "x"})
    s = judge(spec, trace)
    assert s["snapshot_loaded"] == "error"
    assert set(list(s.values())[1:]) == {"not_enough_evidence"}


def test_an_inconsistent_recorded_snapshot_is_an_error(base, monkeypatch):
    spec, events = base
    trace = repinned(events, monkeypatch, lambda f: f["identities"][IDENTITY].update(
        owner_pkh=f["identities"][IDENTITY]["history"][0]["owner_pkh"]))
    s = judge(spec, trace)
    assert s["snapshot_loaded"] == "error"
    assert s["former_controller_refused"] == "not_enough_evidence"


def test_an_identity_absent_from_the_record_is_not_judged(base, monkeypatch):
    """N6: the record cannot judge a rotation it does not hold."""
    spec, events = base
    trace = repinned(events, monkeypatch, lambda f: f.update(identities={}))
    assert set(judge(spec, trace).values()) == {"not_enough_evidence"}


def test_run_level_missing_snapshot_is_incomplete(tmp_path):
    copy_dir = tmp_path / "registry_snapshot"
    shutil.copytree(PROFILE, copy_dir, ignore=shutil.ignore_patterns("mock.json", "__pycache__"))
    _, result = run_lab(str(copy_dir / "scenarios" / "rotation_mock.yaml"), str(tmp_path / "out"))
    assert result.verdict == "incomplete"
    assert stages(result)["snapshot_loaded"] == "not_enough_evidence"


# -- recompute checks, each isolated in a live engine --------------------------

def engine_trace():
    """An engine where provider-former's claim was refused; the test can sign
    with any agent's town key through the engine's auth layer."""
    e = town(("provider-current", "seller", CURRENT),
             ("provider-former", "former_controller", FORMER),
             ("jammer", "seller", None))
    publish(e, "provider-current")
    publish(e, "provider-former")
    e.layers["identity"].create("jammer")
    return e


def refused_with(e, **fields):
    refusal = next(x for x in e.events if x.kind == "registry_claim_refused")
    trace = detail_swap(e.events, refusal, **fields)
    return {s.name: s.status for s in rs.registry_rotation(e.spec, Trace(trace))}


def test_engine_trace_refusal_counts_as_recorded():
    assert refused_with(engine_trace())["former_controller_refused"] == "passed"


def test_the_recorded_card_must_name_the_agent():
    """A card naming someone else, really signed by the former's town key."""
    e = engine_trace()
    card = dict(e.layers["identity"].card("provider-former", ["sell.text-stats"], {}),
                name="provider-current")
    sig = e.layers["auth"].sign_as("provider-former", card)
    assert refused_with(e, card=card, card_signature=sig)["former_controller_refused"] == "failed"


def test_the_recorded_town_key_must_be_the_agents():
    """The former's card re-signed by jammer, with jammer's key recorded."""
    e = engine_trace()
    card = e.layers["identity"].card("provider-former", ["sell.text-stats"], {})
    sig = e.layers["auth"].sign_as("jammer", card)
    assert refused_with(e, card=card, card_signature=sig,
                        town_key=e.layers["identity"].public_key("jammer"))[
        "former_controller_refused"] == "failed"


def test_the_recorded_claim_signature_must_verify():
    """The card carries a claim with a broken signature, the card itself is
    genuinely signed by the former, and the recorded fields match the card."""
    e = engine_trace()
    bad = dict(FORMER, signature="00" * 64)
    card = e.layers["identity"].card("provider-former", ["sell.text-stats"], {})
    card["facts"]["registry_claim"] = bad
    sig = e.layers["auth"].sign_as("provider-former", card)
    assert refused_with(e, card=card, card_signature=sig, signature=bad["signature"])[
        "former_controller_refused"] == "failed"


def test_the_recorded_claim_must_be_the_one_on_the_card(base, monkeypatch):
    """In a re-pinned history where k3 controlled before k1, the refusal
    records k3's valid claim (a former controller too) while the card carries
    k1's: only the card-claim match fails."""
    spec, events = base
    k3 = claim("k3", "provider-former")
    k3_pkh = rs.key_hash(k3["controller_pub"])

    def three_keys(f):
        rec = f["identities"][IDENTITY]
        first = rec["history"][0]
        rec["history"] = [dict(first, owner_pkh=k3_pkh, slot=50, block_height=25),
                          {"event": "controller_rotated", "slot": 80, "block_height": 40,
                           "tx": "mock-k3-to-k1", "from_pkh": k3_pkh,
                           "to_pkh": first["owner_pkh"]}] + rec["history"][1:]
    trace = repinned(events, monkeypatch, three_keys)
    trace = detail_swap(trace, ev(trace, "registry_claim_refused"),
                        controller_pub=k3["controller_pub"], signature=k3["signature"],
                        controller_pkh=k3_pkh)
    assert judge(spec, trace)["former_controller_refused"] == "failed"


def test_a_pinned_snapshot_for_another_source_is_unavailable(tmp_path, monkeypatch):
    snap = dict(copy.deepcopy(MOCK), source="elsewhere")
    path = tmp_path / "mock.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    cls = resolve("registry", "snapshot.mock.v1")
    monkeypatch.setattr(cls, "pinned", fingerprint(snap))
    loaded, problem = cls._load(cls.__new__(cls), path)
    assert loaded is None and "source" in problem
