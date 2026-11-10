# SPDX-License-Identifier: Apache-2.0
"""An independent verifier for AgentSchool credentials.

AgentSchool (https://github.com/justinspar/agentschool) certifies agents in a
skill and issues an Ed25519 JWT credential that other agents check before they
delegate work. This module is Town's own reader of that format, written against
the wire format rather than against AgentSchool's code: Town imports nothing
from AgentSchool and adds no dependency, since ``cryptography`` is already a
Town dependency.

Two implementations reading the same credential is the point. When Town's
verdict here and AgentSchool's own ``check_identity_proof`` verdict agree on the
same credential, that agreement is the compatibility evidence. When they
disagree, the disagreement is the finding. Nothing here establishes that
AgentSchool's exam was fair or that its score means what it says; this checks
only that a credential was signed by the named issuer, is current, is not
revoked, and that the agent presenting it holds the key the credential names.

Example::

    verdict = verify_credential(token, jwks=jwks_doc,
                                revocations_jwt=rl_jwt, now=time.time())
    if verdict.ok and verdict.claims["level"] >= 2:
        ...
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

#: The issuer AgentSchool puts in every credential it signs.
ISSUER = "https://agentschool.local"

#: Clock skew allowed on ``nbf``, matching AgentSchool's own leeway.
LEEWAY_S = 30

#: Default freshness limit for a signed revocation list, used when the list
#: itself declares no ``max_age_seconds``.
REVOCATION_MAX_AGE_S = 300

#: Claims AgentSchool requires in every credential.
REQUIRED_CLAIMS = ("iss", "sub", "jti", "exp", "nbf")

#: The three checks that decide a credential, in AgentSchool's own order.
CREDENTIAL_CHECKS = ("issuer_signature", "not_expired", "not_revoked")

#: Prefix of the message a holder signs to prove it holds the credential's key.
PROOF_PREFIX = "agentschool-proof"


class CredentialFormatError(Exception):
    """The bytes offered were not a credential this verifier can read."""


@dataclass(frozen=True)
class Verdict:
    """The outcome of one verification.

    ``ok`` is true only when every check in ``checks`` is true. ``claims`` are
    the verified claims, or the unverified claims for display only when the
    signature failed. ``reasons`` names every failure, in the order found.

    Example::

        verdict = Verdict(ok=False, checks={"not_revoked": False},
                          claims={}, reasons=["revoked"])
    """

    ok: bool
    checks: dict[str, bool]
    claims: dict[str, Any]
    reasons: list[str] = field(default_factory=list)

    def as_detail(self) -> dict[str, Any]:
        """A JSON-safe summary small enough to record on a Town event.

        Example::

            event_detail = verdict.as_detail()
        """
        return {
            "ok": self.ok,
            "checks": dict(self.checks),
            "reasons": list(self.reasons),
            "subject": self.claims.get("sub"),
            "jti": self.claims.get("jti"),
            "skill": self.claims.get("skill"),
            "skill_version": self.claims.get("skill_version"),
            "level": self.claims.get("level"),
            "score": self.claims.get("score"),
            "exam_version": self.claims.get("exam_version"),
            "operator": self.claims.get("operator"),
        }


# -- JWS reading --------------------------------------------------------


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64url(data: bytes) -> str:
    """Base64url without padding, as every AgentSchool field uses.

    Example::

        text = b64url(b"\\x00\\x01")
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _split(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    if not isinstance(token, str):
        raise CredentialFormatError("token must be a string")
    parts = token.split(".")
    if len(parts) != 3:
        raise CredentialFormatError("a JWS has exactly three segments")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except Exception as exc:  # noqa: BLE001 - any malformed segment is one case
        raise CredentialFormatError(f"malformed segment: {exc}") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise CredentialFormatError("header and payload must be JSON objects")
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    return header, payload, signing_input, signature


def public_key_from_jwk(jwk: dict[str, Any]) -> Ed25519PublicKey:
    """Read an Ed25519 OKP JWK. Raises ``CredentialFormatError`` otherwise.

    Example::

        key = public_key_from_jwk({"kty": "OKP", "crv": "Ed25519", "x": "..."})
    """
    if (not isinstance(jwk, dict) or jwk.get("kty") != "OKP"
            or jwk.get("crv") != "Ed25519" or not isinstance(jwk.get("x"), str)):
        raise CredentialFormatError(
            "expected an Ed25519 OKP JWK with a base64url x")
    try:
        raw = _b64url_decode(jwk["x"])
    except Exception as exc:  # noqa: BLE001
        raise CredentialFormatError(f"x is not base64url: {exc}") from exc
    if len(raw) != 32:
        raise CredentialFormatError("x must encode 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def _verify_jws(token: str, jwks: dict[str, Any]
                ) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Return (header, payload, failure reason or None)."""
    header, payload, signing_input, signature = _split(token)
    if header.get("alg") != "EdDSA":
        return header, payload, "issuer_signature"
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list):
        return header, payload, "jwks_unreadable"
    matching = [k for k in keys
                if isinstance(k, dict) and k.get("kid") == header.get("kid")]
    if not matching:
        return header, payload, "unknown_kid"
    try:
        public_key_from_jwk(matching[0]).verify(signature, signing_input)
    except (InvalidSignature, CredentialFormatError):
        return header, payload, "issuer_signature"
    return header, payload, None


# -- the signed revocation list ----------------------------------------


def verify_revocation_list(token: str, jwks: dict[str, Any], *,
                           now: float | None = None,
                           max_age: int | None = None,
                           issuer: str = ISSUER) -> dict[str, Any]:
    """Check a signed revocation list and its freshness, returning its body.

    Raises ``CredentialFormatError`` when the signature, the issuer or the
    freshness limit fails, because a list Town cannot trust must not be read
    as "nothing is revoked".

    Example::

        body = verify_revocation_list(rl_jwt, jwks_doc, now=time.time())
    """
    now = time.time() if now is None else now
    _, body, reason = _verify_jws(token, jwks)
    if reason is not None:
        raise CredentialFormatError(f"revocation list: {reason}")
    if body.get("iss") != issuer:
        raise CredentialFormatError("revocation list: wrong_issuer")
    try:
        issued_at = float(body["issued_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CredentialFormatError(
            "revocation list: no readable issued_at") from exc
    declared = body.get("max_age_seconds")
    limit = max_age if max_age is not None else (
        int(declared) if isinstance(declared, int) else REVOCATION_MAX_AGE_S)
    age = now - issued_at
    if age > limit:
        raise CredentialFormatError(
            f"revocation list: stale ({int(age)} s > {limit} s)")
    for key in ("revoked", "suspended"):
        if not isinstance(body.get(key), list):
            raise CredentialFormatError(f"revocation list: {key} must be a list")
    return body


def _status_reader(revocations_jwt: str | None, jwks: dict[str, Any],
                   now: float, max_age: int | None):
    """Return a function from jti to status string.

    A list Town cannot verify yields ``revocation_list_unusable`` for every
    credential, which fails ``not_revoked``. That is deliberate: without a
    current list, Town does not know whether a credential still stands, and an
    unknown is not a pass.
    """
    if revocations_jwt is None:
        return lambda jti: "revocation_list_absent"
    try:
        body = verify_revocation_list(revocations_jwt, jwks, now=now,
                                      max_age=max_age)
    except CredentialFormatError:
        return lambda jti: "revocation_list_unusable"
    revoked = {j for j in body["revoked"] if isinstance(j, str)}
    suspended = {j for j in body["suspended"] if isinstance(j, str)}

    def status_of(jti: str) -> str:
        if jti in revoked:
            return "revoked"
        if jti in suspended:
            return "suspended"
        return "valid"

    return status_of


# -- credentials -------------------------------------------------------


def verify_credential(token: str, *, jwks: dict[str, Any],
                      revocations_jwt: str | None = None,
                      now: float | None = None,
                      issuer: str = ISSUER,
                      revocation_max_age: int | None = None) -> Verdict:
    """Check one AgentSchool credential: signature, currency, revocation.

    A revocation list that is missing, unsigned or stale fails ``not_revoked``
    rather than being read as an absence of revocations.

    Example::

        verdict = verify_credential(token, jwks=jwks_doc,
                                    revocations_jwt=rl_jwt)
    """
    now = time.time() if now is None else now
    checks = dict.fromkeys(CREDENTIAL_CHECKS, False)
    reasons: list[str] = []

    try:
        _, claims, reason = _verify_jws(token, jwks)
    except CredentialFormatError:
        return Verdict(ok=False, checks=checks, claims={},
                       reasons=["malformed"])
    if reason is not None:
        reasons.append(reason)
    elif claims.get("iss") != issuer:
        reasons.append("wrong_issuer")
    elif any(c not in claims for c in REQUIRED_CLAIMS):
        missing = [c for c in REQUIRED_CLAIMS if c not in claims]
        reasons.append("missing_claims:" + ",".join(missing))
    else:
        checks["issuer_signature"] = True

    try:
        nbf = float(claims.get("nbf", 0))
        exp = float(claims["exp"])
        if now + LEEWAY_S < nbf:
            reasons.append("not_yet_valid")
        elif now >= exp:
            reasons.append("expired")
        else:
            checks["not_expired"] = True
    except (KeyError, TypeError, ValueError):
        reasons.append("expired")

    jti = claims.get("jti")
    status = _status_reader(revocations_jwt, jwks, now, revocation_max_age)(
        jti if isinstance(jti, str) else "")
    if status == "valid":
        checks["not_revoked"] = True
    else:
        reasons.append(status)

    return Verdict(ok=all(checks.values()), checks=checks, claims=claims,
                   reasons=list(dict.fromkeys(reasons)))


# -- holder proofs -----------------------------------------------------


def proof_message(nonce: str, audience: str) -> bytes:
    """The exact bytes a holder signs: ``agentschool-proof:{nonce}:{audience}``.

    Example::

        message = proof_message("n_abc", "nandatown")
    """
    return f"{PROOF_PREFIX}:{nonce}:{audience}".encode()


def verify_holder_proof(holder_jwk: Any, nonce: str, audience: str,
                        signature: str) -> bool:
    """Check that the holder key named in the credential signed this nonce.

    Example::

        ok = verify_holder_proof(claims["holder_key"], nonce, "nandatown", sig)
    """
    if not isinstance(holder_jwk, dict) or not isinstance(signature, str):
        return False
    try:
        key = public_key_from_jwk(holder_jwk)
        key.verify(_b64url_decode(signature), proof_message(nonce, audience))
    except (InvalidSignature, CredentialFormatError, ValueError, TypeError):
        return False
    return True


class NonceLedger:
    """Nonces Town issued itself, so a replayed proof can be refused.

    Town is the relying party here, so the nonce must be Town's own: a nonce
    the subject chose proves nothing about when it signed. Single use is
    enforced on first read, whether or not the rest of the presentation passes.

    Example::

        ledger = NonceLedger()
        nonce = ledger.issue("agt_1", "nandatown", now=0.0)
    """

    def __init__(self, ttl_s: float = 120.0) -> None:
        self.ttl_s = ttl_s
        self._issued: dict[str, dict[str, Any]] = {}

    def issue(self, subject: str, audience: str, *, now: float,
              nonce: str | None = None) -> str:
        """Record and return a nonce bound to one subject and audience."""
        nonce = nonce or f"town-n-{len(self._issued) + 1}"
        self._issued[nonce] = {"subject": subject, "audience": audience,
                               "expires_at": now + self.ttl_s, "used": False}
        return nonce

    def check(self, nonce: Any, subject: Any, audience: str, *,
              now: float) -> tuple[bool, bool, list[str]]:
        """Return (fresh, unused, reasons) and consume the nonce.

        ``fresh`` and ``unused`` are separate claims: a nonce can be within
        its window and correctly bound yet already spent, which is the replay
        case, and the reasons name which of the two failed.
        """
        record = self._issued.get(nonce) if isinstance(nonce, str) else None
        if record is None:
            return False, False, ["nonce_unknown"]
        freshness = [
            reason for reason, bad in (
                ("nonce_expired", now >= record["expires_at"]),
                ("nonce_subject_mismatch", record["subject"] != subject),
                ("nonce_audience_mismatch", record["audience"] != audience),
            ) if bad
        ]
        unused = not record["used"]
        record["used"] = True
        reasons = list(freshness) + ([] if unused else ["nonce_used"])
        return not freshness, unused, reasons


def verify_presentation(token: str, *, nonce: Any, signature: Any,
                        audience: str, jwks: dict[str, Any],
                        revocations_jwt: str | None = None,
                        nonces: NonceLedger | None = None,
                        now: float | None = None,
                        issuer: str = ISSUER,
                        revocation_max_age: int | None = None) -> Verdict:
    """Check a whole presentation: the credential and the holder's proof.

    With a ``NonceLedger``, ``nonce_fresh`` and ``nonce_unused`` are decided
    against nonces Town issued. Without one, both are left out of ``checks``
    and named in ``reasons`` as ``nonce_freshness_not_checked``, because a
    recorded proof replayed from a fixture says nothing about freshness and
    must not be reported as if it did.

    Example::

        verdict = verify_presentation(token, nonce=n, signature=s,
                                      audience="nandatown", jwks=jwks_doc,
                                      nonces=ledger)
    """
    now = time.time() if now is None else now
    base = verify_credential(token, jwks=jwks, revocations_jwt=revocations_jwt,
                             now=now, issuer=issuer,
                             revocation_max_age=revocation_max_age)
    checks = dict(base.checks)
    reasons = list(base.reasons)

    holder = base.claims.get("holder_key")
    signed = verify_holder_proof(holder, nonce if isinstance(nonce, str) else "",
                                 audience, signature)
    checks["holder_signature"] = signed
    if not signed:
        reasons.append("holder_signature")

    if nonces is None:
        reasons.append("nonce_freshness_not_checked")
    else:
        fresh, unused, nonce_reasons = nonces.check(
            nonce, base.claims.get("sub"), audience, now=now)
        checks["nonce_fresh"] = fresh
        checks["nonce_unused"] = unused
        reasons.extend(nonce_reasons)

    return Verdict(ok=all(checks.values()), checks=checks, claims=base.claims,
                   reasons=list(dict.fromkeys(reasons)))
