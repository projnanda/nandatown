# SPDX-License-Identifier: Apache-2.0
"""Town's compatibility check against AgentSchool credentials.

AgentSchool (https://github.com/justinspar/agentschool) certifies agents in a
skill and issues a signed credential other agents verify before delegating.
``integrations/agentschool/`` puts that credential in Town's trust layer, so a
Town buyer hires only an agent that can present one and prove it holds the key.

Two kinds of test live here, and they prove different things.

The verifier tests mint their own credentials with a throwaway Ed25519 key.
That is Town's own test data, not AgentSchool evidence: it shows Town's reader
of the format behaves correctly on each failure, and it establishes nothing
about AgentSchool itself.

The scenario tests run against a fixture. With the committed fixture captured
from a real AgentSchool run they exercise real credentials, and they skip when
that capture is absent. They also run against a synthetic fixture built here,
which keeps the plugin and the validators under test with no AgentSchool
present.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from nandatown.bundle import verify_bundle
from nandatown.sim.runner import run_lab

INTEGRATION = Path(__file__).resolve().parents[1] / "integrations" / "agentschool"
SCENARIOS = INTEGRATION / "scenarios"
REAL_FIXTURE = INTEGRATION / "fixtures" / "directory.json"

ISSUER = "https://agentschool.local"
SKILL = "data-driven-slides"
AUDIENCE = "nandatown"


def _load(name: str):
    """Load a module beside the plugin, under the name the plugin uses."""
    full = f"nandatown_agentschool_{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, INTEGRATION / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


cv = _load("credential_verify")
cs = _load("credential_source")


# -- a throwaway issuer, so the verifier can be tested on every failure --


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class CredentialIssuer:
    """An Ed25519 issuer that signs in AgentSchool's format.

    This is Town's own fixture. A credential it signs says nothing about
    AgentSchool; it exists so each check in the verifier can be driven to
    failure on demand.
    """

    def __init__(self, kid: str = "test-kid-1") -> None:
        self.key = Ed25519PrivateKey.generate()
        self.kid = kid

    def public_jwk(self, key: Ed25519PrivateKey | None = None) -> dict:
        raw = (key or self.key).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {"kty": "OKP", "crv": "Ed25519", "x": b64url(raw)}

    def jwks(self) -> dict:
        return {"keys": [dict(self.public_jwk(), kid=self.kid, alg="EdDSA",
                              use="sig")]}

    def sign(self, claims: dict, *, typ: str = "JWT",
             alg: str = "EdDSA", kid: str | None = None) -> str:
        header = {"alg": alg, "typ": typ, "kid": self.kid if kid is None else kid}
        parts = [b64url(json.dumps(header, separators=(",", ":")).encode()),
                 b64url(json.dumps(claims, separators=(",", ":")).encode())]
        signing_input = ".".join(parts).encode()
        return ".".join(parts + [b64url(self.key.sign(signing_input))])

    def credential(self, *, sub: str, jti: str, holder: dict,
                   level: int = 1, score: float = 98.6,
                   iss: str = ISSUER, skill: str = SKILL,
                   now: float | None = None, ttl: int = 86400,
                   nbf_offset: int = 0, drop: str | None = None) -> str:
        now = int(time.time() if now is None else now)
        claims = {
            "iss": iss, "sub": sub, "jti": jti, "iat": now,
            "nbf": now + nbf_offset, "exp": now + ttl,
            "skill": skill, "skill_version": "2.0.0", "level": level,
            "score": score, "score_range": [score - 3, score + 3],
            "attempts": 1, "exam_version": "harbor-pine-slide-design-v2",
            "exam_session": f"test-{sub}", "holder_key": holder,
            "operator": "town-test", "status_url": "http://localhost/none",
        }
        if drop is not None:
            claims.pop(drop, None)
        return self.sign(claims)

    def revocation_list(self, *, revoked: list[str] | None = None,
                        suspended: list[str] | None = None,
                        issued_at: float | None = None,
                        max_age: int = 300) -> str:
        issued = int(time.time() if issued_at is None else issued_at)
        return self.sign({"iss": ISSUER, "iat": issued, "issued_at": issued,
                          "max_age_seconds": max_age,
                          "revoked": revoked or [],
                          "suspended": suspended or []},
                         typ="revocation-list+jwt")


def holder_pair() -> tuple[Ed25519PrivateKey, dict]:
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    return key, {"kty": "OKP", "crv": "Ed25519", "x": b64url(raw)}


def sign_proof(key: Ed25519PrivateKey, nonce: str, audience: str) -> str:
    return b64url(key.sign(cv.proof_message(nonce, audience)))


@pytest.fixture
def issuer() -> CredentialIssuer:
    return CredentialIssuer()


@pytest.fixture
def holder():
    return holder_pair()


# -- the credential checks ---------------------------------------------


def test_a_current_signed_unrevoked_credential_verifies(issuer, holder):
    _, jwk = holder
    token = issuer.credential(sub="agt_1", jti="j1", holder=jwk)
    verdict = cv.verify_credential(token, jwks=issuer.jwks(),
                                   revocations_jwt=issuer.revocation_list())
    assert verdict.ok, verdict.reasons
    assert verdict.checks == {"issuer_signature": True, "not_expired": True,
                              "not_revoked": True}
    assert verdict.claims["skill"] == SKILL
    assert verdict.claims["level"] == 1


@pytest.mark.parametrize("mutate,reason", [
    ("tamper", "issuer_signature"),
    ("unknown_kid", "unknown_kid"),
    ("wrong_alg", "issuer_signature"),
    ("wrong_issuer", "wrong_issuer"),
    ("missing_jti", "missing_claims:jti"),
    ("garbage", "malformed"),
])
def test_a_credential_that_fails_its_signature_checks_names_the_reason(
        issuer, holder, mutate, reason):
    _, jwk = holder
    if mutate == "garbage":
        token = "not.a.jws"
    elif mutate == "unknown_kid":
        token = issuer.sign({"iss": ISSUER, "sub": "a", "jti": "j",
                             "nbf": 0, "exp": time.time() + 60},
                            kid="some-other-kid")
    elif mutate == "wrong_alg":
        token = issuer.sign({"iss": ISSUER, "sub": "a", "jti": "j",
                             "nbf": 0, "exp": time.time() + 60}, alg="HS256")
    elif mutate == "wrong_issuer":
        token = issuer.credential(sub="agt_1", jti="j1", holder=jwk,
                                  iss="https://not-agentschool.example")
    elif mutate == "missing_jti":
        token = issuer.credential(sub="agt_1", jti="j1", holder=jwk,
                                  drop="jti")
    else:
        good = issuer.credential(sub="agt_1", jti="j1", holder=jwk)
        head, payload, signature = good.split(".")
        swapped = issuer.credential(sub="agt_evil", jti="j1", holder=jwk)
        token = f"{head}.{swapped.split('.')[1]}.{signature}"

    verdict = cv.verify_credential(token, jwks=issuer.jwks(),
                                   revocations_jwt=issuer.revocation_list())
    assert not verdict.ok
    assert reason in verdict.reasons, verdict.reasons
    assert verdict.checks.get("issuer_signature") is False


@pytest.mark.parametrize("kwargs,reason", [
    ({"ttl": -10}, "expired"),
    ({"nbf_offset": 600}, "not_yet_valid"),
])
def test_a_credential_outside_its_window_fails_not_expired(
        issuer, holder, kwargs, reason):
    _, jwk = holder
    token = issuer.credential(sub="agt_1", jti="j1", holder=jwk, **kwargs)
    verdict = cv.verify_credential(token, jwks=issuer.jwks(),
                                   revocations_jwt=issuer.revocation_list())
    assert not verdict.ok
    assert reason in verdict.reasons
    assert verdict.checks["not_expired"] is False
    # The signature is still good; only the window failed.
    assert verdict.checks["issuer_signature"] is True


def test_clock_skew_inside_the_leeway_is_tolerated(issuer, holder):
    """A credential dated slightly ahead is accepted, as AgentSchool does."""
    _, jwk = holder
    token = issuer.credential(sub="agt_1", jti="j1", holder=jwk,
                              nbf_offset=cv.LEEWAY_S - 5)
    verdict = cv.verify_credential(token, jwks=issuer.jwks(),
                                   revocations_jwt=issuer.revocation_list())
    assert verdict.ok, verdict.reasons


@pytest.mark.parametrize("status,reason", [
    ("revoked", "revoked"),
    ("suspended", "suspended"),
])
def test_a_withdrawn_credential_fails_not_revoked(issuer, holder, status,
                                                  reason):
    _, jwk = holder
    token = issuer.credential(sub="agt_1", jti="j1", holder=jwk)
    revocations = issuer.revocation_list(**{status: ["j1"]})
    verdict = cv.verify_credential(token, jwks=issuer.jwks(),
                                   revocations_jwt=revocations)
    assert not verdict.ok
    assert reason in verdict.reasons
    assert verdict.checks["not_revoked"] is False
    assert verdict.checks["issuer_signature"] is True


def test_an_unknown_credential_is_not_treated_as_current(issuer, holder):
    """A jti the list has never heard of is still checked, not assumed good.

    AgentSchool's own server answers "unknown_credential" for a jti it does
    not hold. A signed list that simply does not name a credential cannot
    distinguish the two, so Town reads absence from the list as valid and the
    PR records that limit.
    """
    _, jwk = holder
    token = issuer.credential(sub="agt_1", jti="never-issued", holder=jwk)
    verdict = cv.verify_credential(token, jwks=issuer.jwks(),
                                   revocations_jwt=issuer.revocation_list())
    assert verdict.checks["not_revoked"] is True
    assert verdict.ok


@pytest.mark.parametrize("revocations,reason", [
    (None, "revocation_list_absent"),
    ("stale", "revocation_list_unusable"),
    ("foreign", "revocation_list_unusable"),
    ("garbage", "revocation_list_unusable"),
])
def test_a_revocation_list_town_cannot_trust_is_not_read_as_no_revocations(
        issuer, holder, revocations, reason):
    """A missing, stale or unsigned list must fail, never pass by default."""
    _, jwk = holder
    token = issuer.credential(sub="agt_1", jti="j1", holder=jwk)
    if revocations == "stale":
        revocations = issuer.revocation_list(
            issued_at=time.time() - 3600, max_age=300)
    elif revocations == "foreign":
        revocations = CredentialIssuer(kid=issuer.kid).revocation_list()
    elif revocations == "garbage":
        revocations = "nope"
    verdict = cv.verify_credential(token, jwks=issuer.jwks(),
                                   revocations_jwt=revocations)
    assert not verdict.ok
    assert reason in verdict.reasons, verdict.reasons
    assert verdict.checks["not_revoked"] is False


def test_an_archived_list_passes_only_when_the_operator_lifts_the_limit(
        issuer, holder):
    """The offline fixture path: freshness waived knowingly, not by accident."""
    _, jwk = holder
    token = issuer.credential(sub="agt_1", jti="j1", holder=jwk)
    old = issuer.revocation_list(issued_at=time.time() - 86400, max_age=300)
    assert not cv.verify_credential(token, jwks=issuer.jwks(),
                                    revocations_jwt=old).ok
    waived = cv.verify_credential(token, jwks=issuer.jwks(),
                                  revocations_jwt=old,
                                  revocation_max_age=10 ** 9)
    assert waived.ok, waived.reasons


# -- holder proofs -----------------------------------------------------


def test_the_holder_key_named_in_the_credential_can_prove_custody(holder):
    key, jwk = holder
    nonce = "n_abc"
    assert cv.verify_holder_proof(jwk, nonce, AUDIENCE,
                                  sign_proof(key, nonce, AUDIENCE))


@pytest.mark.parametrize("wrong", ["key", "nonce", "audience", "signature"])
def test_a_proof_that_does_not_match_the_credential_is_refused(holder, wrong):
    key, jwk = holder
    other_key, _ = holder_pair()
    nonce = "n_abc"
    signer = other_key if wrong == "key" else key
    signature = sign_proof(signer, "n_other" if wrong == "nonce" else nonce,
                           "elsewhere" if wrong == "audience" else AUDIENCE)
    if wrong == "signature":
        signature = "not-base64url-at-all!!"
    assert not cv.verify_holder_proof(jwk, nonce, AUDIENCE, signature)


def test_a_presentation_under_a_fresh_town_nonce_verifies(issuer, holder):
    key, jwk = holder
    token = issuer.credential(sub="agt_1", jti="j1", holder=jwk)
    ledger = cv.NonceLedger()
    nonce = ledger.issue("agt_1", AUDIENCE, now=time.time())
    verdict = cv.verify_presentation(
        token, nonce=nonce, signature=sign_proof(key, nonce, AUDIENCE),
        audience=AUDIENCE, jwks=issuer.jwks(),
        revocations_jwt=issuer.revocation_list(), nonces=ledger)
    assert verdict.ok, verdict.reasons
    assert verdict.checks["nonce_fresh"] is True
    assert verdict.checks["nonce_unused"] is True
    assert verdict.checks["holder_signature"] is True


def test_the_same_nonce_cannot_be_presented_twice(issuer, holder):
    """Replaying a captured proof must fail on the second use."""
    key, jwk = holder
    token = issuer.credential(sub="agt_1", jti="j1", holder=jwk)
    ledger = cv.NonceLedger()
    nonce = ledger.issue("agt_1", AUDIENCE, now=time.time())
    signature = sign_proof(key, nonce, AUDIENCE)
    common = {"audience": AUDIENCE, "jwks": issuer.jwks(),
              "revocations_jwt": issuer.revocation_list(), "nonces": ledger}
    assert cv.verify_presentation(token, nonce=nonce, signature=signature,
                                  **common).ok
    second = cv.verify_presentation(token, nonce=nonce, signature=signature,
                                    **common)
    assert not second.ok
    assert "nonce_used" in second.reasons
    assert second.checks["nonce_unused"] is False


@pytest.mark.parametrize("case,reason", [
    ("unknown", "nonce_unknown"),
    ("expired", "nonce_expired"),
    ("other_subject", "nonce_subject_mismatch"),
])
def test_a_nonce_town_did_not_issue_for_this_agent_is_refused(
        issuer, holder, case, reason):
    key, jwk = holder
    token = issuer.credential(sub="agt_1", jti="j1", holder=jwk)
    ledger = cv.NonceLedger(ttl_s=60)
    now = time.time()
    if case == "unknown":
        nonce = "n_never_issued"
    elif case == "expired":
        nonce = ledger.issue("agt_1", AUDIENCE, now=now - 600)
    else:
        nonce = ledger.issue("agt_other", AUDIENCE, now=now)
    verdict = cv.verify_presentation(
        token, nonce=nonce, signature=sign_proof(key, nonce, AUDIENCE),
        audience=AUDIENCE, jwks=issuer.jwks(),
        revocations_jwt=issuer.revocation_list(), nonces=ledger, now=now)
    assert not verdict.ok
    assert reason in verdict.reasons, verdict.reasons
    assert verdict.checks["nonce_fresh"] is False


def test_a_replayed_proof_verifies_its_signature_and_says_freshness_is_unknown(
        issuer, holder):
    """The offline path: the holder signature is real, the timing is not shown."""
    key, jwk = holder
    token = issuer.credential(sub="agt_1", jti="j1", holder=jwk)
    nonce = "n_captured_earlier"
    verdict = cv.verify_presentation(
        token, nonce=nonce, signature=sign_proof(key, nonce, AUDIENCE),
        audience=AUDIENCE, jwks=issuer.jwks(),
        revocations_jwt=issuer.revocation_list(), nonces=None)
    assert verdict.ok, verdict.reasons
    assert verdict.checks["holder_signature"] is True
    assert "nonce_fresh" not in verdict.checks
    assert "nonce_freshness_not_checked" in verdict.reasons


def test_a_revocation_list_signed_by_another_key_is_rejected_outright(issuer):
    foreign = CredentialIssuer(kid=issuer.kid)
    with pytest.raises(cv.CredentialFormatError):
        cv.verify_revocation_list(foreign.revocation_list(), issuer.jwks())


# -- a synthetic fixture, so the plugin runs with no AgentSchool --------


def build_synthetic_fixture(path: Path) -> dict:
    """Write a fixture shaped like a real capture, with Town's own keys.

    The three agents mirror the states a real training run leaves behind: one
    that passed and holds a current credential, one whose credential was
    revoked, and one that failed and has none.
    """
    issuer = CredentialIssuer(kid="test-kid-1")
    atlas_key, atlas_jwk = holder_pair()
    brisk_key, brisk_jwk = holder_pair()
    now = time.time()

    atlas = issuer.credential(sub="agt_atlas", jti="jti-atlas",
                              holder=atlas_jwk, level=1, score=98.6, now=now)
    brisk = issuer.credential(sub="agt_brisk", jti="jti-brisk",
                              holder=brisk_jwk, level=1, score=97.6, now=now)
    atlas_nonce, brisk_nonce = "n_atlas_capture", "n_brisk_capture"

    fixture = {
        "schema": cs.FIXTURE_SCHEMA,
        "captured_at": now,
        "synthetic": True,
        "note": ("Town's own test data, signed with a throwaway key. It is not"
                 " an AgentSchool capture and is not evidence about"
                 " AgentSchool."),
        "agentschool": {"commit": None, "server": None,
                        "issuer_kid": issuer.kid, "skill": SKILL,
                        "skill_version": "2.0.0"},
        "jwks": issuer.jwks(),
        "revocations_jwt": issuer.revocation_list(revoked=["jti-brisk"],
                                                  issued_at=now),
        "revocation_captured_at": now,
        "agents": [
            {"name": "atlas", "agent_id": "agt_atlas", "skill": SKILL,
             "level": 1, "score": 98.6, "credential_status": "valid",
             "credential_jti": "jti-atlas", "credential_jwt": atlas,
             "recorded_proof": {
                 "nonce": atlas_nonce, "audience": AUDIENCE,
                 "signature": sign_proof(atlas_key, atlas_nonce, AUDIENCE),
                 "signer": "synthetic"}},
            {"name": "brisk", "agent_id": "agt_brisk", "skill": SKILL,
             "level": 1, "score": 97.6, "credential_status": "revoked",
             "credential_jti": "jti-brisk", "credential_jwt": brisk,
             "recorded_proof": {
                 "nonce": brisk_nonce, "audience": AUDIENCE,
                 "signature": sign_proof(brisk_key, brisk_nonce, AUDIENCE),
                 "signer": "synthetic"}},
            {"name": "flash", "agent_id": "agt_flash", "skill": SKILL,
             "level": None, "score": None,
             "credential_status": "no_credential", "credential_jwt": None,
             "absent_reason": "no_credential", "recorded_proof": None},
        ],
    }
    path.write_text(json.dumps(fixture, indent=1) + "\n")
    return fixture


@pytest.fixture
def synthetic_fixture(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "synthetic_directory.json"
    build_synthetic_fixture(path)
    monkeypatch.setenv("NANDATOWN_AGENTSCHOOL_MODE", "fixture")
    monkeypatch.setenv("NANDATOWN_AGENTSCHOOL_FIXTURE", str(path))
    return path


def stages(result) -> dict[str, str]:
    return {stage.name: stage.status for stage in result.stages}


def notes(result) -> dict[str, str]:
    return {stage.name: stage.note for stage in result.stages}


# -- the source -------------------------------------------------------


def test_the_fixture_source_hides_a_revoked_agent_from_the_certified_search(
        synthetic_fixture):
    source = cs.FixtureSource(synthetic_fixture)
    names = {entry["name"] for entry in source.certified(SKILL, 1)}
    assert names == {"atlas"}
    # but every agent is still resolvable, which is how Town checks one it
    # met through its own registry
    assert set(source.roster()) == {"atlas", "brisk", "flash"}


def test_the_fixture_source_states_what_it_cannot_establish(
        synthetic_fixture):
    description = cs.FixtureSource(synthetic_fixture).describe()
    assert description.mode == "fixture"
    assert "nonce_freshness" in description.cannot_establish
    assert "revocation_list_freshness" in description.cannot_establish


def test_an_unreadable_fixture_is_refused_rather_than_ignored(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": "something-else"}')
    with pytest.raises(cs.SourceUnavailable):
        cs.FixtureSource(bad)
    with pytest.raises(cs.SourceUnavailable):
        cs.FixtureSource(tmp_path / "missing.json")


def test_live_mode_without_a_token_is_refused(monkeypatch):
    with pytest.raises(cs.SourceUnavailable):
        cs.source_from_env({"NANDATOWN_AGENTSCHOOL_MODE": "live"})
    with pytest.raises(cs.SourceUnavailable):
        cs.source_from_env({"NANDATOWN_AGENTSCHOOL_MODE": "sideways"})


def test_the_roster_carries_the_endpoint_of_an_agent_search_omits(tmp_path):
    """An agent with a revoked credential is absent from AgentSchool's search.

    Town still has to challenge it, or the reason it was refused gets mixed up
    with Town's inability to reach it, so the roster carries its service URL.
    """
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({
        "atlas": {"agent_id": "agt_1", "endpoint": "http://127.0.0.1:8101"},
        "brisk": {"agent_id": "agt_2", "endpoint": "http://127.0.0.1:8102"},
        "flash": "agt_3",
    }))
    roster, endpoints = cs.read_roster(str(path))
    assert roster == {"atlas": "agt_1", "brisk": "agt_2", "flash": "agt_3"}
    assert endpoints == {"agt_1": "http://127.0.0.1:8101",
                         "agt_2": "http://127.0.0.1:8102"}
    assert cs.read_roster(None) == ({}, {})


@pytest.mark.parametrize("content", ['["not", "an", "object"]', '{"a": 1}',
                                     "not json at all"])
def test_an_unusable_roster_is_refused(tmp_path, content):
    path = tmp_path / "roster.json"
    path.write_text(content)
    with pytest.raises(cs.SourceUnavailable):
        cs.read_roster(str(path))


# -- the scenarios ----------------------------------------------------


def run(scenario: str, out: Path):
    return run_lab(str(SCENARIOS / scenario), str(out))


def test_only_the_verified_holder_is_hired_and_paid(synthetic_fixture,
                                                    tmp_path):
    """The main workflow: three sellers, one credential, one trade."""
    bundle, result = run("credential_gating.yaml", tmp_path / "gated")
    got = stages(result)
    assert result.verdict == "passed", notes(result)

    assert got["credential_source_recorded"] == "passed"
    assert got["credential_verified"] == "passed"
    assert got["holder_key_proved"] == "passed"
    assert got["revoked_refused"] == "passed"
    assert got["uncertified_refused"] == "passed"
    assert got["unverified_got_no_work"] == "passed"
    assert got["certified_trade_completed"] == "passed"
    assert got["ledger_conserved"] == "passed"
    assert got["privacy"] == "passed"

    # What a replayed capture cannot show is reported, not claimed.
    assert got["holder_proof_fresh"] == "not_tested"
    assert got["verdicts_agreed"] == "not_tested"
    assert got["outcome_reported"] == "not_tested"

    assert verify_bundle(bundle) == []


def test_the_cheapest_seller_wins_when_the_credential_check_is_ignored(
        synthetic_fixture, tmp_path):
    """The control. One setting changes, and the uncertified agent is hired.

    The credentials are challenged and verified in this run too, so the only
    difference from the gated run is whether the verdict decides anything.
    That is what shows the gate, rather than the price comparison, is what
    keeps work away from an uncertified agent.
    """
    bundle, result = run("credential_gating_ungated_control.yaml",
                         tmp_path / "ungated")
    got = stages(result)
    assert result.verdict == "failed", notes(result)

    # The checks still ran and still reached the right verdicts.
    assert got["credential_verified"] == "passed"
    assert got["revoked_refused"] == "passed"
    assert got["uncertified_refused"] == "passed"

    # And the work went to an agent that failed its check.
    assert got["unverified_got_no_work"] == "failed"
    assert "seller-uncertified" in notes(result)["unverified_got_no_work"]
    assert verify_bundle(bundle) == []


def test_nothing_is_delegated_when_the_only_credential_is_revoked(
        synthetic_fixture, tmp_path):
    """The boundary case: fail closed rather than hiring anyway."""
    bundle, result = run("revoked_credential_fail_closed.yaml",
                         tmp_path / "revoked")
    got = stages(result)
    assert result.verdict == "passed", notes(result)
    assert got["revoked_refused"] == "passed"
    assert got["no_work_delegated"] == "passed"
    assert got["refusal_recorded"] == "passed"
    assert got["nothing_verified"] == "passed"
    assert verify_bundle(bundle) == []


def test_a_credential_derived_score_is_not_moved_by_one_receipt(
        synthetic_fixture, tmp_path):
    """A counterparty's receipt cannot raise or lower a certified level.

    The receipt is still recorded with delta 0 and its reason, so the trace
    shows the report arriving and shows that it changed nothing.
    """
    bundle, _ = run("credential_gating.yaml", tmp_path / "score")
    events = [json.loads(line) for line
              in (Path(bundle) / "events.jsonl").read_text().splitlines()]
    updates = [e for e in events if e["kind"] == "reputation_updated"]
    assert updates, "the buyer's receipt should still be recorded"
    for update in updates:
        assert update["detail"]["delta"] == 0
        assert update["detail"]["formula"] == "agentschool.v1"
        assert update["detail"]["reason"] == "score_is_credential_derived"


# -- replaying a bundle whose validator came from a plugin file --------


def test_verify_names_the_plugin_a_contributed_validator_needs(
        synthetic_fixture, tmp_path):
    """A bundle this Town cannot replay must say why, not just "mismatch".

    A scenario may put its validator in a ``plugin_files`` module, as this
    integration does. In a fresh process that module is not loaded, the
    validator is absent, and the replay differs from the recorded result for a
    reason that has nothing to do with the evidence. Before the fix in
    ``bundle.py`` the only report was "evaluator replay mismatch", which reads
    like corrupted evidence.
    """
    from nandatown.sim.validators import VALIDATORS
    from nandatown.bundle import verify_bundle as verify

    bundle, _ = run("credential_gating.yaml", tmp_path / "unloaded")
    assert verify(bundle) == []

    # Stand in for a fresh process that never loaded the plugin file.
    removed = VALIDATORS.pop("agentschool_credential_gating")
    try:
        problems = verify(bundle)
        assert len(problems) == 1, problems
        assert "agentschool_credential_gating" in problems[0]
        assert "plugin.py" in problems[0]
        assert "--plugin" in problems[0]
    finally:
        VALIDATORS["agentschool_credential_gating"] = removed

    assert verify(bundle) == []


def test_verify_replays_a_contributed_validator_when_given_its_plugin(
        synthetic_fixture, tmp_path):
    """``--plugin`` loads the validator, so the bundle verifies offline."""
    from nandatown.sim.validators import VALIDATORS
    from nandatown.bundle import verify_bundle as verify

    bundle, _ = run("credential_gating.yaml", tmp_path / "with-plugin")
    removed = VALIDATORS.pop("agentschool_credential_gating")
    try:
        assert verify(bundle,
                      plugins=[str(INTEGRATION / "plugin.py")]) == []
    finally:
        VALIDATORS.setdefault("agentschool_credential_gating", removed)


def test_verify_reports_an_unloadable_plugin_instead_of_raising(tmp_path):
    from nandatown.bundle import verify_bundle as verify

    problems = verify(str(tmp_path), plugins=[str(tmp_path / "nope.py")])
    assert problems and "cannot load plugin file" in problems[0]


# -- the same scenarios against the real capture ----------------------

needs_capture = pytest.mark.skipif(
    not REAL_FIXTURE.exists(),
    reason=("no AgentSchool capture at integrations/agentschool/fixtures/"
            "directory.json; see that directory's README to make one"))


@needs_capture
def test_the_captured_fixture_describes_the_run_it_came_from():
    source = cs.FixtureSource(REAL_FIXTURE)
    description = source.describe()
    assert description.issuer_kid, "the capture must name the issuer key"
    assert description.skill == SKILL
    data = json.loads(REAL_FIXTURE.read_text())
    assert not data.get("synthetic"), "the committed fixture must be real"
    assert data["agentschool"]["commit"], "record the AgentSchool commit"
    assert data.get("limitations"), "record what the capture cannot show"


@needs_capture
@pytest.mark.parametrize("scenario,verdict", [
    ("credential_gating.yaml", "passed"),
    ("credential_gating_ungated_control.yaml", "failed"),
    ("revoked_credential_fail_closed.yaml", "passed"),
])
def test_the_scenarios_reach_the_same_verdicts_on_real_credentials(
        tmp_path, monkeypatch, scenario, verdict):
    monkeypatch.setenv("NANDATOWN_AGENTSCHOOL_MODE", "fixture")
    monkeypatch.setenv("NANDATOWN_AGENTSCHOOL_FIXTURE", str(REAL_FIXTURE))
    bundle, result = run(scenario, tmp_path / scenario.replace(".", "-"))
    assert result.verdict == verdict, notes(result)
    assert verify_bundle(bundle) == []
