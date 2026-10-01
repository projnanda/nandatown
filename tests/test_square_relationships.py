from functools import partial

import pytest

from nandatown.identity_portable import Keystore, resolve_file
from nandatown.square.relationships import (
    RelationshipBook,
    RelationshipError,
    relationship_id,
    relationship_terms,
    revocation_payload,
)

FREEBUSY = "calendar.freebusy"
DETAILS = "calendar.details"


class RecordingEngine:
    def __init__(self):
        self.events = []

    def emit(self, observer, kind, subject, detail=None):
        self.events.append({"observer": observer, "kind": kind,
                            "subject": subject, "detail": detail or {}})

    def kinds(self):
        return [e["kind"] for e in self.events]


@pytest.fixture()
def town(tmp_path):
    ks = Keystore(str(tmp_path))
    a = ks.new_identity("instinct")["agent_id"]
    b = ks.new_identity("muse")["agent_id"]
    c = ks.new_identity("openclaw")["agent_id"]
    engine = RecordingEngine()
    book = RelationshipBook(engine, partial(resolve_file, ks.registry_path))
    return ks, engine, book, a, b, c


def mutual_terms(a, b, *, nonce="n-1", expires_at=3600.0):
    return relationship_terms(a, b, grants={a: [FREEBUSY], b: [FREEBUSY]},
                              issued_at=0.0, expires_at=expires_at,
                              nonce=nonce)


def both_sign(ks, terms):
    names = {i["agent_id"]: i["name"] for i in ks.identities()}
    return {agent: ks.sign(names[agent], terms) for agent in terms["parties"]}


def establish(ks, book, a, b, **kw):
    terms = mutual_terms(a, b, **kw)
    return book.establish(terms, both_sign(ks, terms), now=1.0)


# -- terms ----------------------------------------------------------------


def test_terms_are_canonical_regardless_of_party_order(town):
    _, _, _, a, b, _ = town
    one = relationship_terms(a, b, grants={a: [FREEBUSY]}, issued_at=0.0,
                             expires_at=10.0, nonce="x")
    two = relationship_terms(b, a, grants={a: [FREEBUSY]}, issued_at=0.0,
                             expires_at=10.0, nonce="x")
    assert one == two
    assert relationship_id(one) == relationship_id(two)
    assert relationship_id(one).startswith("rel:")


@pytest.mark.parametrize("kwargs, message", [
    ({"grants": {}, "self_pair": True}, "two different agents"),
    ({"grants": {"did:town:stranger": [FREEBUSY]}}, "not a party"),
    ({"grants": {"A": ["Calendar Read!"]}}, "scope"),
    ({"grants": {"A": [FREEBUSY]}, "expires_at": 0.0}, "expire"),
    ({"grants": {"A": [FREEBUSY]}, "nonce": ""}, "nonce"),
])
def test_invalid_terms_are_refused(town, kwargs, message):
    _, _, _, a, b, _ = town
    other = a if kwargs.pop("self_pair", False) else b
    grants = {(a if k == "A" else k): v
              for k, v in kwargs.pop("grants").items()}
    params = {"issued_at": 0.0, "expires_at": 10.0, "nonce": "x", **kwargs}
    with pytest.raises(RelationshipError, match=message):
        relationship_terms(a, other, grants=grants, **params)


# -- establishing ------------------------------------------------------------


def test_both_signatures_establish_a_relationship(town):
    ks, engine, book, a, b, _ = town
    rid = establish(ks, book, a, b)
    assert book.relationship(rid).state == "active"
    assert engine.kinds() == ["relationship_established"]
    assert engine.events[0]["subject"] == rid


def test_a_missing_signature_is_rejected_without_state(town):
    ks, engine, book, a, b, _ = town
    terms = mutual_terms(a, b)
    signatures = both_sign(ks, terms)
    del signatures[b]
    with pytest.raises(RelationshipError, match="signature"):
        book.establish(terms, signatures, now=1.0)
    assert book.relationships() == []
    assert engine.kinds() == ["relationship_rejected"]


def test_a_third_party_cannot_sign_for_a_party(town):
    ks, _, book, a, b, _ = town
    terms = mutual_terms(a, b)
    signatures = {**both_sign(ks, terms), b: ks.sign("openclaw", terms)}
    with pytest.raises(RelationshipError, match="signature"):
        book.establish(terms, signatures, now=1.0)


def test_terms_widened_after_signing_are_rejected(town):
    ks, _, book, a, b, _ = town
    terms = mutual_terms(a, b)
    signatures = both_sign(ks, terms)
    widened = {**terms, "grants": {**terms["grants"],
                                   a: [DETAILS, FREEBUSY]}}
    with pytest.raises(RelationshipError, match="signature"):
        book.establish(widened, signatures, now=1.0)


def test_an_unregistered_agent_cannot_form_a_relationship(town, tmp_path):
    ks, _, book, a, _, _ = town
    outsider_ks = Keystore(str(tmp_path / "elsewhere"))
    z = outsider_ks.new_identity("outsider")["agent_id"]
    terms = mutual_terms(a, z)
    signatures = {a: ks.sign("instinct", terms),
                  z: outsider_ks.sign("outsider", terms)}
    with pytest.raises(RelationshipError, match="identity"):
        book.establish(terms, signatures, now=1.0)


@pytest.mark.parametrize("now, message", [(-1.0, "not yet valid"),
                                          (3600.5, "expired")])
def test_terms_outside_their_window_are_rejected(town, now, message):
    ks, _, book, a, b, _ = town
    terms = mutual_terms(a, b)
    with pytest.raises(RelationshipError, match=message):
        book.establish(terms, both_sign(ks, terms), now=now)


def test_the_same_terms_cannot_be_established_twice(town):
    ks, _, book, a, b, _ = town
    establish(ks, book, a, b)
    with pytest.raises(RelationshipError, match="already"):
        establish(ks, book, a, b)


# -- authorizing data requests --------------------------------------------


def test_a_granted_scope_is_allowed(town):
    ks, engine, book, a, b, _ = town
    rid = establish(ks, book, a, b)
    decision = book.authorize(requester=b, owner=a, scope=FREEBUSY, now=2.0)
    assert decision.allowed
    assert decision.relationship_id == rid
    assert engine.kinds()[-1] == "data_request_allowed"


def test_a_scope_never_granted_is_an_unauthorized_request(town):
    ks, engine, book, a, b, _ = town
    establish(ks, book, a, b)
    decision = book.authorize(requester=b, owner=a, scope=DETAILS, now=2.0)
    assert not decision.allowed
    assert decision.reason == "scope_not_granted"
    last = engine.events[-1]
    assert last["kind"] == "unauthorized_data_request"
    assert last["detail"] == {"requester": b, "owner": a, "scope": DETAILS,
                              "reason": "scope_not_granted"}


def test_grants_are_directional(town):
    ks, _, book, a, b, _ = town
    terms = relationship_terms(a, b, grants={a: [FREEBUSY]}, issued_at=0.0,
                               expires_at=10.0, nonce="one-way")
    book.establish(terms, both_sign(ks, terms), now=1.0)
    assert book.authorize(requester=b, owner=a, scope=FREEBUSY,
                          now=2.0).allowed
    reverse = book.authorize(requester=a, owner=b, scope=FREEBUSY, now=2.0)
    assert reverse.reason == "scope_not_granted"


def test_a_stranger_has_no_relationship(town):
    ks, _, book, a, b, c = town
    establish(ks, book, a, b)
    decision = book.authorize(requester=c, owner=a, scope=FREEBUSY, now=2.0)
    assert decision.reason == "no_relationship"


def test_requests_after_expiry_are_refused(town):
    ks, _, book, a, b, _ = town
    establish(ks, book, a, b, expires_at=100.0)
    decision = book.authorize(requester=b, owner=a, scope=FREEBUSY,
                              now=100.5)
    assert decision.reason == "expired"


# -- revoking -------------------------------------------------------------


def revoke(ks, book, rid, name, agent_id, at=5.0):
    payload = revocation_payload(rid, agent_id, at)
    book.revoke(rid, by=agent_id, signature=ks.sign(name, payload), at=at)


def test_either_party_can_revoke_and_it_holds_on_the_next_request(town):
    ks, engine, book, a, b, _ = town
    rid = establish(ks, book, a, b)
    assert book.authorize(requester=b, owner=a, scope=FREEBUSY,
                          now=2.0).allowed
    revoke(ks, book, rid, "muse", b)
    assert book.relationship(rid).state == "revoked"
    assert "relationship_revoked" in engine.kinds()
    for requester, owner in [(b, a), (a, b)]:
        decision = book.authorize(requester=requester, owner=owner,
                                  scope=FREEBUSY, now=6.0)
        assert decision.reason == "revoked"


def test_a_non_party_cannot_revoke(town):
    ks, _, book, a, b, c = town
    rid = establish(ks, book, a, b)
    with pytest.raises(RelationshipError, match="party"):
        revoke(ks, book, rid, "openclaw", c)
    assert book.relationship(rid).state == "active"


def test_a_forged_revocation_is_refused(town):
    ks, _, book, a, b, _ = town
    rid = establish(ks, book, a, b)
    payload = revocation_payload(rid, a, 5.0)
    with pytest.raises(RelationshipError, match="signature"):
        book.revoke(rid, by=a, signature=ks.sign("muse", payload), at=5.0)
    assert book.relationship(rid).state == "active"


def test_revoked_terms_cannot_be_replayed(town):
    ks, _, book, a, b, _ = town
    rid = establish(ks, book, a, b)
    revoke(ks, book, rid, "instinct", a)
    with pytest.raises(RelationshipError, match="already"):
        establish(ks, book, a, b)
    assert book.relationship(rid).state == "revoked"


def test_fresh_terms_form_a_new_relationship_after_revocation(town):
    ks, _, book, a, b, _ = town
    rid = establish(ks, book, a, b)
    revoke(ks, book, rid, "instinct", a)
    fresh = establish(ks, book, a, b, nonce="n-2")
    assert fresh != rid
    decision = book.authorize(requester=b, owner=a, scope=FREEBUSY, now=6.0)
    assert decision.allowed and decision.relationship_id == fresh


def test_revoking_twice_is_refused(town):
    ks, _, book, a, b, _ = town
    rid = establish(ks, book, a, b)
    revoke(ks, book, rid, "instinct", a)
    with pytest.raises(RelationshipError, match="not active"):
        revoke(ks, book, rid, "muse", b, at=6.0)


def test_unknown_relationship_cannot_be_revoked(town):
    ks, _, book, a, _, _ = town
    with pytest.raises(RelationshipError, match="unknown"):
        revoke(ks, book, "rel:missing", "instinct", a)


# -- review hardening -------------------------------------------------------


def test_terms_carry_a_purpose_so_signatures_cannot_cross_over(town):
    _, _, _, a, b, _ = town
    assert mutual_terms(a, b)["purpose"] == "relationship"


def test_whole_number_timestamps_from_other_languages_establish(town):
    ks, _, book, a, b, _ = town
    terms = {**mutual_terms(a, b), "issued_at": 0, "expires_at": 3600}
    rid = book.establish(terms, both_sign(ks, terms), now=1.0)
    assert book.relationship(rid).state == "active"


def test_non_canonical_terms_are_rejected_and_recorded(town):
    ks, engine, book, a, b, _ = town
    terms = {**mutual_terms(a, b), "grants": {a: [FREEBUSY, FREEBUSY]}}
    with pytest.raises(RelationshipError, match="canonical"):
        book.establish(terms, both_sign(ks, terms), now=1.0)
    assert engine.kinds() == ["relationship_rejected"]


def test_unserializable_terms_are_still_a_recorded_rejection(town):
    _, engine, book, a, b, _ = town
    terms = {**mutual_terms(a, b), "extra": object()}
    with pytest.raises(RelationshipError):
        book.establish(terms, {}, now=1.0)
    assert engine.kinds() == ["relationship_rejected"]


@pytest.mark.parametrize("signature", [None, 7, "not-hex"])
def test_malformed_signatures_are_refusals(town, signature):
    ks, engine, book, a, b, _ = town
    terms = mutual_terms(a, b)
    signatures = {**both_sign(ks, terms), b: signature}
    with pytest.raises(RelationshipError, match="signature"):
        book.establish(terms, signatures, now=1.0)
    assert engine.kinds() == ["relationship_rejected"]


def test_an_unreadable_registry_is_a_refusal(town, tmp_path):
    ks, engine, _, a, b, _ = town
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    book = RelationshipBook(engine, partial(resolve_file, str(broken)))
    terms = mutual_terms(a, b)
    with pytest.raises(RelationshipError, match="identity"):
        book.establish(terms, both_sign(ks, terms), now=1.0)


def test_requests_before_the_window_are_not_yet_valid(town):
    ks, _, book, a, b, _ = town
    terms = relationship_terms(a, b, grants={a: [FREEBUSY]}, issued_at=10.0,
                               expires_at=20.0, nonce="later")
    book.establish(terms, both_sign(ks, terms), now=10.0)
    decision = book.authorize(requester=b, owner=a, scope=FREEBUSY, now=5.0)
    assert decision.reason == "not_yet_valid"


@pytest.mark.parametrize("at, message", [(float("nan"), "timestamp"),
                                         (0.5, "before it was established")])
def test_revocation_times_are_validated(town, at, message):
    ks, _, book, a, b, _ = town
    rid = establish(ks, book, a, b)
    with pytest.raises(RelationshipError, match=message):
        revoke(ks, book, rid, "instinct", a, at=at)
    assert book.relationship(rid).state == "active"


def test_a_request_before_the_revocation_time_replays_as_allowed(town):
    ks, _, book, a, b, _ = town
    rid = establish(ks, book, a, b)
    revoke(ks, book, rid, "instinct", a, at=5.0)
    assert book.authorize(requester=b, owner=a, scope=FREEBUSY,
                          now=4.0).allowed
    assert book.authorize(requester=b, owner=a, scope=FREEBUSY,
                          now=5.0).reason == "revoked"
