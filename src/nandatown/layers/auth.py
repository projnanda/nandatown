"""Auth layer: message and card signing plus verification."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from . import register
from ..records import canonical_json


@register("auth", "plain.v1")
class PlainAuth:
    """Trusts any claimed sender: no signatures, no verification.

    Exists to show what the auth layer is for. Swap it into
    capability_spoofing and watch the town fail its own report.
    """

    def __init__(self, engine):
        self.engine = engine

    def sign_as(self, name: str, payload: Any) -> str:
        return "unsigned"

    def verify(self, claimed_name: str, payload: Any, signature: str,
               subject: str = "") -> bool:
        return True


@register("auth", "hmac.v1")
class HmacAuth:
    """Signs payloads with the sender's identity key; forged senders fail."""

    def __init__(self, engine):
        self.engine = engine

    def _identity(self):
        return self.engine.layers["identity"]

    def sign_as(self, name: str, payload: Any) -> str:
        key = self._identity().key(name)
        if key is None:
            raise KeyError(f"no identity for {name}")
        return hmac.new(key.encode(), canonical_json(payload).encode(),
                        hashlib.sha256).hexdigest()

    def verify(self, claimed_name: str, payload: Any, signature: str,
               subject: str = "") -> bool:
        key = self._identity().key(claimed_name)
        ok = key is not None and hmac.compare_digest(
            hmac.new(key.encode(), canonical_json(payload).encode(),
                     hashlib.sha256).hexdigest(),
            signature,
        )
        if not ok:
            self.engine.emit("town", "signature_invalid",
                             subject or claimed_name,
                             {"claimed": claimed_name})
        return ok


ENVELOPE_FIELDS = ("message_id", "conversation", "sender", "to", "kind",
                   "body")


@register("auth", "hmac.bound.v1")
class BoundHmacAuth(HmacAuth):
    """Signs the whole envelope, so a signed message is good for one recipient and one kind.

    hmac.v1 signs the body alone. Whoever receives a signed message
    can put the same body and signature in a new envelope, under the
    original sender's name, addressed to anyone and labelled as any
    kind, and it verifies. Here the signature covers the message id,
    conversation, sender, recipient, kind and body together, and the
    envelope is refused unless it arrives at the recipient it names.
    Cards and attestations are not envelopes and are signed as before.
    """

    @staticmethod
    def _bound(envelope: dict[str, Any]) -> dict[str, Any] | None:
        if any(field not in envelope for field in ENVELOPE_FIELDS):
            return None
        return {field: envelope[field] for field in ENVELOPE_FIELDS}

    def sign_envelope(self, envelope: dict[str, Any]) -> str:
        bound = self._bound(envelope)
        if bound is None:
            raise ValueError("an envelope needs every signed field")
        return self.sign_as(envelope["sender"], bound)

    def verify_envelope(self, envelope: dict[str, Any], to: str) -> bool:
        bound = self._bound(envelope)
        signature = envelope.get("signature", "")
        claimed = envelope.get("sender")
        subject = envelope.get("message_id")
        key = (self._identity().key(claimed)
               if isinstance(claimed, str) else None)
        ok = (bound is not None and key is not None
              and isinstance(signature, str)
              and envelope["to"] == to
              and hmac.compare_digest(
                  hmac.new(key.encode(), canonical_json(bound).encode(),
                           hashlib.sha256).hexdigest(),
                  signature))
        if not ok:
            self.engine.emit(
                "town", "signature_invalid",
                subject if isinstance(subject, str) and subject else to,
                {"claimed": claimed, "to": to,
                 "kind": envelope.get("kind"), "bound": "envelope"})
        return ok
