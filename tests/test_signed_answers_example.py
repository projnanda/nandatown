"""Signed Answers example: offline checks against the stand-in outlet.

No network beyond loopback, no model and no credential. The live outlet
agent is exercised by examples/signed_answers/run_check.py --live.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "signed_answers"
PLUGIN = "examples/signed_answers/signed_answers_town.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, EXAMPLE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


town = _load("signed_answers_town")
stub = _load("stub_outlet")


@pytest.fixture
def outlet(monkeypatch):
    servers = []

    def start(idempotent: bool = False):
        server = stub.make_server(0, idempotent)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        monkeypatch.setenv("SIGNED_ANSWERS_URL",
                           f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.setenv("SIGNED_ANSWERS_TOKEN", stub.STUB_TOKEN)
        monkeypatch.delenv("SIGNED_ANSWERS_QUESTION", raising=False)
        return server

    monkeypatch.chdir(REPO)
    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _run(scenario: str, tmp_path, patched=None):
    """Run one scenario. `patched` is a freshly loaded copy of the plugin
    whose client a test has altered; run_lab then uses that copy's roles
    instead of loading the file again."""
    from nandatown.bundle import verify_bundle
    from nandatown.sim.runner import run_lab

    bundle, result = run_lab(
        f"examples/signed_answers/scenarios/{scenario}.yaml",
        str(tmp_path), plugins=None if patched else [PLUGIN])
    assert verify_bundle(bundle) == []
    return bundle, {s.name: s.status for s in result.stages}, result.verdict


def test_subscriber_gets_a_verified_package_and_a_stranger_gets_none(
        outlet, tmp_path):
    outlet()
    _, stages, verdict = _run("signed_answers_clean", tmp_path)
    assert verdict == "passed"
    assert stages["outlet_listed"] == "passed"
    assert stages["outlet_discovered"] == "passed"
    assert stages["package_verified[reader-subscriber]"] == "passed"
    assert stages["sentences_match_published_story[reader-subscriber]"] == "passed"
    assert stages["no_package_without_credential[reader-stranger]"] == "passed"
    assert stages["privacy"] == "passed"


@pytest.mark.parametrize("scenario", [
    "signed_answers_clean", "signed_answers_duplicate",
    "signed_answers_duplicate_remembered", "signed_answers_altered"])
def test_package_text_and_question_stay_out_of_the_bundle(
        outlet, tmp_path, scenario):
    outlet()
    bundle, _, _ = _run(scenario, tmp_path)
    recorded = "".join(p.read_text() for p in Path(bundle).iterdir()
                       if p.is_file())
    assert "Harbor Gazette" not in recorded
    assert "transit board" not in recorded
    assert stub.STUB_TOKEN not in recorded
    assert "127.0.0.1" not in recorded


def test_duplicate_delivery_yields_two_packages_from_a_fresh_per_call_outlet(
        outlet, tmp_path):
    server = outlet()
    _, stages, verdict = _run("signed_answers_duplicate", tmp_path)
    assert verdict == "failed"
    assert stages["package_verified[reader-subscriber]"] == "passed"
    assert stages["one_package_per_request[reader-subscriber]"] == "failed"
    assert server.outlet.calls == 2


def test_duplicate_delivery_is_safe_when_the_outlet_is_idempotent(
        outlet, tmp_path):
    server = outlet(idempotent=True)
    _, stages, verdict = _run("signed_answers_duplicate", tmp_path)
    assert verdict == "passed"
    assert stages["one_package_per_request[reader-subscriber]"] == "passed"
    assert server.outlet.calls == 1


def test_gateway_memory_absorbs_the_redelivery(outlet, tmp_path):
    server = outlet()
    _, stages, verdict = _run("signed_answers_duplicate_remembered", tmp_path)
    assert verdict == "passed"
    assert stages["one_package_per_request[reader-subscriber]"] == "passed"
    assert server.outlet.calls == 1


def test_reader_rejects_a_package_altered_by_the_gateway(outlet, tmp_path):
    outlet()
    _, stages, verdict = _run("signed_answers_altered", tmp_path)
    assert verdict == "passed"
    assert stages["altered_package_rejected[reader-subscriber]"] == "passed"
    assert "package_verified[reader-subscriber]" not in stages


def test_unreachable_outlet_is_not_listed_and_nothing_is_asked(
        outlet, tmp_path, monkeypatch):
    outlet()
    monkeypatch.setenv("SIGNED_ANSWERS_URL", "http://127.0.0.1:9")
    bundle, stages, verdict = _run("signed_answers_clean", tmp_path)
    assert verdict == "failed"
    assert stages["outlet_listed"] == "failed"
    assert stages["outlet_discovered"] == "failed"
    assert stages["package_verified[reader-subscriber]"] == "not_enough_evidence"
    assert '"kind":"source_request"' not in (Path(bundle)
                                              / "events.jsonl").read_text()


def test_check_envelope_separates_its_three_facts():
    envelope = stub.StubOutlet().envelope()
    key = envelope["signature"]["key"]
    assert all(town.check_envelope(envelope, key).values())

    altered = town.tamper(envelope)
    checks = town.check_envelope(altered, key)
    assert checks == {"content_id_matches": False, "signature_valid": False,
                      "publisher_key_matches": True}

    # A re-addressed id fixes the fingerprint and still fails the signature.
    readdressed = copy.deepcopy(altered)
    readdressed["package_id"] = town.content_id(readdressed["package"])
    checks = town.check_envelope(readdressed, key)
    assert checks["content_id_matches"] and not checks["signature_valid"]

    other = "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
    assert not any(v for k, v in town.check_envelope(envelope, other).items()
                   if k != "content_id_matches")
    for junk in (None, [], {"package": "x"}, {"package": {}, "signature": 1}):
        assert not town.check_envelope(junk, key)["signature_valid"]


def test_only_package_links_on_the_outlets_own_address_are_followed():
    client = town.Outlet("https://outlet.example", None)
    digest = "a" * 64
    text = (f"Package: https://outlet.example/packages/sha256-{digest}\n"
            f"Also: https://elsewhere.example/packages/sha256-{digest}\n"
            f"And: https://outlet.example.evil.test/packages/sha256-{digest}")
    assert client.package_keys(text) == [f"sha256-{digest}"]


def _hostile(monkeypatch, reply_state, package):
    """Load a fresh copy of the plugin and replace the outlet's replies
    with ones a broken or hostile agent could send. The card stays."""
    town = _load("signed_answers_town")

    def ask(self, text, *, message_id, context_id, with_token):
        digest = "b" * 64
        return {"state": reply_state if reply_state in town.A2A_STATES
                else "OTHER_STATE",
                "text": f"{self.base}/packages/sha256-{digest}"}

    seen = []

    def fetch(self, package_key, *, with_token):
        seen.append(with_token)
        return package

    monkeypatch.setattr(town.Outlet, "ask", ask)
    monkeypatch.setattr(town.Outlet, "package", fetch)
    return town, seen


def test_outlet_supplied_text_cannot_reach_the_bundle(
        outlet, tmp_path, monkeypatch):
    outlet()
    secret = "OUTLET-TEXT-THAT-MUST-NOT-BE-RECORDED"
    patched, _ = _hostile(monkeypatch, secret, {
        "package_id": secret,
        "package": {"result": {"full_text": secret}, "sentences": secret},
        "signature": secret})
    bundle, stages, verdict = _run("signed_answers_clean", tmp_path, patched)
    recorded = "".join(p.read_text() for p in Path(bundle).iterdir()
                       if p.is_file())
    assert secret not in recorded
    assert verdict == "failed"
    assert stages["package_verified[reader-subscriber]"] == "failed"


def test_a_stranger_request_never_borrows_the_subscriber_credential(
        outlet, tmp_path, monkeypatch):
    outlet()
    patched, seen = _hostile(monkeypatch, "TASK_STATE_COMPLETED", None)
    _run("signed_answers_clean", tmp_path, patched)
    assert sorted(seen) == [False, True]  # stranger without, subscriber with


def test_unencodable_or_misshapen_packages_end_as_a_result_not_a_crash(
        outlet, tmp_path, monkeypatch):
    outlet()
    for i, package in enumerate([
            {"package": {"sentences": [{"text": "\ud800"}]}},
            {"package": "not a dict"}, {"package": {"sentences": ["x"]}}]):
        patched, _ = _hostile(monkeypatch, "TASK_STATE_COMPLETED", package)
        _, stages, verdict = _run("signed_answers_altered", tmp_path / str(i),
                                  patched)
        assert verdict in ("failed", "incomplete", "passed")
        assert "package_verified[reader-subscriber]" not in stages


def test_a_refusal_on_redelivery_is_a_failure_not_missing_evidence(
        outlet, tmp_path, monkeypatch):
    outlet()
    patched = _load("signed_answers_town")
    real = patched.Outlet.ask
    calls = []

    def ask(self, text, **kw):
        calls.append(1)
        return (real(self, text, **kw) if len(calls) == 1
                else {"state": "HTTP_500", "text": ""})

    monkeypatch.setattr(patched.Outlet, "ask", ask)
    _, stages, verdict = _run("signed_answers_duplicate", tmp_path, patched)
    assert verdict == "failed"
    assert stages["one_package_per_request[reader-subscriber]"] == "failed"
    assert stages["package_verified[reader-subscriber]"] == "failed"


def _signed(package):
    """A package signed with the stand-in outlet's key."""
    import base64
    import hashlib

    outlet = stub.StubOutlet()
    package = dict(package, publisher={"agent_key": outlet.did})
    raw = town.canonical(package)
    return {"package_id": "df://sha256-" + hashlib.sha256(raw).hexdigest(),
            "package": package,
            "signature": {"alg": "Ed25519", "key": outlet.did,
                          "value": base64.urlsafe_b64encode(
                              outlet.private.sign(raw)).decode().rstrip("=")}}


@pytest.mark.parametrize("state, package", [
    ("TASK_STATE_COMPLETED", {"result": "no_match", "sentences": []}),
    ("TASK_STATE_COMPLETED", {"result": "match", "sentences": []}),
    ("TASK_STATE_FAILED", {"result": "match",
                           "sentences": [{"n": 1, "text": "x", "sources": []}]}),
])
def test_a_validly_signed_package_that_is_not_an_answer_does_not_pass(
        outlet, tmp_path, monkeypatch, state, package):
    outlet()
    patched, _ = _hostile(monkeypatch, state, _signed(package))
    _, stages, verdict = _run("signed_answers_clean", tmp_path, patched)
    assert verdict == "failed"
    assert stages["package_verified[reader-subscriber]"] == "failed"


def test_altered_case_needs_a_package_that_was_valid_before_the_change(
        outlet, tmp_path, monkeypatch):
    outlet()
    broken = _signed({"result": "match",
                      "sentences": [{"n": 1, "text": "x 1", "sources": []}]})
    broken["signature"]["value"] = "AAAA"
    patched, _ = _hostile(monkeypatch, "TASK_STATE_COMPLETED", broken)
    _, stages, verdict = _run("signed_answers_altered", tmp_path, patched)
    assert verdict == "failed"
    assert stages["altered_package_rejected[reader-subscriber]"] == "failed"


def test_each_run_uses_its_own_message_identity_at_the_outlet(
        outlet, tmp_path):
    server = outlet(idempotent=True)
    _run("signed_answers_clean", tmp_path / "a")
    _run("signed_answers_clean", tmp_path / "b")
    assert server.outlet.calls == 2  # the second run is not served from the first
    _run("signed_answers_duplicate", tmp_path / "c")
    assert server.outlet.calls == 3  # and a redelivery inside a run is


def test_verifying_a_directory_with_no_bundles_is_not_a_pass(tmp_path):
    runner = _load("run_check")
    assert runner.verify_saved(str(tmp_path)) == 1


def test_a_sentence_that_is_not_in_the_published_story_fails(
        outlet, tmp_path, monkeypatch):
    outlet()
    monkeypatch.setattr(stub, "SENTENCES", stub.SENTENCES[:2] + [
        "This sentence was never published."])
    patched = _load("signed_answers_town")
    real = stub.StubOutlet.envelope

    # The story page is built from SENTENCES at request time, so sign the
    # package first with a sentence the page will not carry.
    signed = {}

    def frozen(self, base="http://127.0.0.1:0"):
        if not signed:
            signed.update(real(self, base))
            monkeypatch.setattr(stub, "SENTENCES", stub.SENTENCES[:2])
        return signed

    monkeypatch.setattr(stub.StubOutlet, "envelope", frozen)
    _, stages, verdict = _run("signed_answers_clean", tmp_path, patched)
    assert stages["package_verified[reader-subscriber]"] == "passed"
    assert stages["sentences_match_published_story[reader-subscriber]"] == "failed"
    assert verdict == "failed"


def test_story_on_another_site_is_not_fetched():
    assert town._same_site("https://www.outlet.example/a", "https://outlet.example")
    assert not town._same_site("https://elsewhere.example/a", "https://outlet.example")
    assert not town._same_site("http://outlet.example/a", "https://outlet.example")
    assert not town._same_site("file:///etc/passwd", "https://outlet.example")
    package = {"story": {"url": "https://elsewhere.example/a"},
               "publisher": {"url": "https://outlet.example"},
               "sentences": [{"text": "x"}]}
    assert town.sentences_in_published_story(package) is None
    assert town.sentences_in_published_story({"story": "x"}) is None
