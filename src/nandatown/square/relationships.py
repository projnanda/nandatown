"""Relationships: mutually signed, scoped consent between two agents.

Both parties sign the same terms with their controller keys: who they
are, which scopes each grants the other, the validity window and a
nonce. The relationship id is the fingerprint of those terms. Every data
request is authorized against the relationships in force at that moment,
so a revocation by either party holds from the very next request, and a
book never establishes signed terms it has seen before, revoked or not.
Revocations live in the book; persisting them is the caller's job.

Time is passed in by the caller, never read from the wall clock.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from ..identity_portable import IdentityError, verify_signature
from ..records import canonical_json, fingerprint

SCOPE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
OBSERVER = "square"


class RelationshipError(Exception):
    pass


@dataclass(frozen=True)
class Relationship:
    relationship_id: str
    terms: dict[str, Any]
    state: str
    established_at: float
    revoked_at: float | None = None
    revoked_by: str | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    relationship_id: str | None = None


def _timestamp(value: Any, label: str) -> float:
    """The value itself, unconverted: 0 and 0.0 serialize differently,
    and signatures cover the exact bytes the agent signed."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise RelationshipError(f"{label} must be a finite timestamp")
    return value


def _scopes(grantor: str, scopes: Any) -> list[str]:
    if not isinstance(scopes, list) or not all(
            isinstance(s, str) and SCOPE.fullmatch(s) for s in scopes):
        raise RelationshipError(
            f"grants from {grantor} must be dotted lowercase scope names"
            " such as 'calendar.freebusy'")
    return sorted(set(scopes))


def relationship_terms(a: str, b: str, *, grants: dict[str, list[str]],
                       issued_at: float, expires_at: float,
                       nonce: str) -> dict[str, Any]:
    """Canonical terms: the same whichever party is named first.

    grants maps a grantor to the scopes it grants the other party."""
    if not (isinstance(a, str) and isinstance(b, str) and a and b) or a == b:
        raise RelationshipError("a relationship needs two different agents")
    parties = sorted([a, b])
    strangers = sorted(set(grants) - set(parties))
    if strangers:
        raise RelationshipError(f"{strangers[0]} is not a party")
    issued = _timestamp(issued_at, "issued_at")
    expires = _timestamp(expires_at, "expires_at")
    if expires <= issued:
        raise RelationshipError("terms must expire after they are issued")
    if not isinstance(nonce, str) or not nonce:
        raise RelationshipError("terms need a nonce")
    return {
        "purpose": "relationship",
        "parties": parties,
        "grants": {g: _scopes(g, grants[g]) for g in sorted(grants)},
        "issued_at": issued,
        "expires_at": expires,
        "nonce": nonce,
    }


def relationship_id(terms: dict[str, Any]) -> str:
    return "rel:" + fingerprint(terms).removeprefix("sha256:")[:32]


def revocation_payload(rid: str, by: str, at: float) -> dict[str, Any]:
    return {"purpose": "revoke", "relationship_id": rid, "by": by,
            "at": at}


def _normalized(terms: Any) -> dict[str, Any]:
    try:
        a, b = terms["parties"]
        again = relationship_terms(
            a, b, grants=terms["grants"], issued_at=terms["issued_at"],
            expires_at=terms["expires_at"], nonce=terms["nonce"])
        canonical = canonical_json(again) == canonical_json(terms)
    except (KeyError, TypeError, ValueError) as exc:
        raise RelationshipError("malformed relationship terms") from exc
    if not canonical:
        raise RelationshipError("relationship terms are not canonical")
    return again


def _subject(terms: Any) -> str:
    try:
        return relationship_id(terms)
    except (TypeError, ValueError):
        return "unknown"


def _in_force(r: Relationship, now: float) -> bool:
    """In its window and not yet revoked at now, so a recorded request
    replays to the same decision after a later revocation."""
    if r.revoked_at is not None and now >= r.revoked_at:
        return False
    return r.terms["issued_at"] <= now <= r.terms["expires_at"]


class RelationshipBook:
    """The town's record of relationships, and the authorization check.

    resolve maps an agent id to its registered controller public key, as
    identity_portable.resolve_file does for the town registry."""

    def __init__(self, engine, resolve: Callable[[str], str]):
        self.engine = engine
        self.resolve = resolve
        self._book: dict[str, Relationship] = {}

    def relationship(self, rid: str) -> Relationship:
        if rid not in self._book:
            raise RelationshipError(f"unknown relationship {rid}")
        return self._book[rid]

    def relationships(self) -> list[Relationship]:
        return [self._book[rid] for rid in sorted(self._book)]

    def _controller(self, agent_id: str) -> str:
        try:
            return self.resolve(agent_id)
        except (IdentityError, OSError, KeyError, ValueError) as exc:
            raise RelationshipError(
                f"no registered identity for {agent_id}") from exc

    def _check_signatures(self, terms: dict[str, Any],
                          signatures: dict[str, str]) -> None:
        extra = sorted(set(signatures) - set(terms["parties"]))
        if extra:
            raise RelationshipError(f"signature from non-party {extra[0]}")
        for party in terms["parties"]:
            if party not in signatures:
                raise RelationshipError(f"missing signature from {party}")
            if not isinstance(signatures[party], str) or not verify_signature(self._controller(party), terms,
                                    signatures[party]):
                raise RelationshipError(
                    f"signature from {party} does not verify against its"
                    " registered controller key")

    def _check_window(self, terms: dict[str, Any], now: float) -> None:
        if now < terms["issued_at"]:
            raise RelationshipError("terms are not yet valid")
        if now > terms["expires_at"]:
            raise RelationshipError("terms have expired")

    def establish(self, terms: dict[str, Any], signatures: dict[str, str],
                  now: float) -> str:
        try:
            canonical = _normalized(terms)
            rid = relationship_id(canonical)
            self._check_signatures(canonical, signatures)
            if rid in self._book:
                raise RelationshipError(
                    f"relationship {rid} already established"
                    f" (now {self._book[rid].state})")
            self._check_window(canonical, _timestamp(now, "now"))
        except RelationshipError as exc:
            self.engine.emit(OBSERVER, "relationship_rejected",
                             _subject(terms), {"reason": str(exc)})
            raise
        self._book = {**self._book, rid: Relationship(
            relationship_id=rid, terms=canonical, state="active",
            established_at=now)}
        self.engine.emit(OBSERVER, "relationship_established", rid,
                         {"parties": canonical["parties"],
                          "grants": canonical["grants"],
                          "expires_at": canonical["expires_at"]})
        return rid

    def revoke(self, rid: str, by: str, signature: str, at: float) -> None:
        current = self.relationship(rid)
        if current.state != "active":
            raise RelationshipError(f"relationship {rid} is not active")
        if by not in current.terms["parties"]:
            raise RelationshipError(f"{by} is not a party to {rid}")
        if _timestamp(at, "at") < current.established_at:
            raise RelationshipError(
                f"{rid} cannot be revoked before it was established")
        if not isinstance(signature, str) or not verify_signature(self._controller(by),
                                revocation_payload(rid, by, at), signature):
            raise RelationshipError(
                f"revocation signature from {by} does not verify")
        self._book = {**self._book, rid: replace(
            current, state="revoked", revoked_at=at, revoked_by=by)}
        self.engine.emit(OBSERVER, "relationship_revoked", rid,
                         {"by": by, "at": at})

    def _decide(self, requester: str, owner: str, scope: str,
                now: float) -> Decision:
        between = [r for r in self.relationships()
                   if {requester, owner} == set(r.terms["parties"])]
        if requester == owner or not between:
            return Decision(False, "no_relationship")
        live = [r for r in between if _in_force(r, now)]
        granting = [r for r in live
                    if scope in r.terms["grants"].get(owner, [])]
        if granting:
            return Decision(True, "granted", granting[0].relationship_id)
        if live:
            return Decision(False, "scope_not_granted")
        if any(r.state == "revoked" and now >= r.revoked_at
               for r in between):
            return Decision(False, "revoked")
        if any(now < r.terms["issued_at"] for r in between):
            return Decision(False, "not_yet_valid")
        return Decision(False, "expired")

    def authorize(self, requester: str, owner: str, scope: str,
                  now: float) -> Decision:
        """Whether requester may read owner's data under scope, recorded
        either way; a refusal is an unauthorized_data_request."""
        decision = self._decide(requester, owner, scope,
                                _timestamp(now, "now"))
        detail = {"requester": requester, "owner": owner, "scope": scope}
        if decision.allowed:
            self.engine.emit(OBSERVER, "data_request_allowed",
                             decision.relationship_id, detail)
        else:
            self.engine.emit(OBSERVER, "unauthorized_data_request", owner,
                             {**detail, "reason": decision.reason})
        return decision
