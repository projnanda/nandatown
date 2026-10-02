"""Communication layer: envelopes, conversation ids, correlation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import register


@register("communication", "envelope.v1")
class EnvelopeComms:
    """
    Base envelope communication layer (v1).
    
    Provides basic message envelopes with conversation IDs, sender/recipient,
    and message kinds. No integrity checksums.
    """

    def __init__(self, engine):
        self.engine = engine
        self._message_seq = 0
        self._conversation_seq = 0

    def new_conversation(self) -> str:
        self._conversation_seq += 1
        return f"c-{self._conversation_seq}"

    def envelope(self, sender: str, to: str, kind: str, body: dict[str, Any],
                 conversation: str | None = None) -> dict[str, Any]:
        """Create a basic envelope (no integrity checksum)."""
        self._message_seq += 1
        return {
            "message_id": f"m-{self._message_seq}",
            "conversation": conversation or self.new_conversation(),
            "sender": sender,
            "to": to,
            "kind": kind,
            "body": body,
        }

    def reply(self, original: dict[str, Any], sender: str, kind: str,
              body: dict[str, Any]) -> dict[str, Any]:
        return self.envelope(sender, original["sender"], kind, body,
                             conversation=original["conversation"])

    def verify(self, envelope: dict[str, Any]) -> bool:
        """
        Verify envelope - always returns True for base version.
        
        No integrity checking in envelope.v1.
        """
        return True


@register("communication", "envelope_integrity.v1")
class EnvelopeIntegrityComms:
    """
    Extends envelope.v1 with SHA256 body integrity checksums.
    
    Detects message body corruption in transit by:
    1. Sender: calculating SHA256(body) when creating envelope
    2. Receiver: recalculating SHA256 and verifying it matches
    3. Rejecting messages with mismatched checksums
    
    Invariant: Every message delivered to an agent has a valid checksum
    matching its body. Corrupted messages are rejected before processing.
    """

    def __init__(self, engine):
        self.engine = engine
        self._message_seq = 0
        self._conversation_seq = 0

    def _integrity(self, body: dict[str, Any]) -> str:
        """
        Calculate SHA256 checksum of message body.
        
        Uses deterministic JSON serialization (sort_keys=True) to ensure
        the same logical data always produces the same checksum, regardless
        of key ordering in the dictionary.
        """
        # Canonical JSON: sorted keys, no spaces
        canonical = json.dumps(body, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def new_conversation(self) -> str:
        self._conversation_seq += 1
        return f"c-{self._conversation_seq}"

    def envelope(self, sender: str, to: str, kind: str, body: dict[str, Any],
                 conversation: str | None = None) -> dict[str, Any]:
        """Create an envelope with integrity checksum (extends envelope.v1)."""
        self._message_seq += 1
        return {
            "message_id": f"m-{self._message_seq}",
            "conversation": conversation or self.new_conversation(),
            "sender": sender,
            "to": to,
            "kind": kind,
            "body": body,
            "integrity": self._integrity(body),  # NEW: SHA256 checksum
        }

    def reply(self, original: dict[str, Any], sender: str, kind: str,
              body: dict[str, Any]) -> dict[str, Any]:
        return self.envelope(sender, original["sender"], kind, body,
                             conversation=original["conversation"])

    def verify(self, envelope: dict[str, Any]) -> bool:
        """
        Verify that envelope body matches its integrity checksum.
        
        Called by engine.deliver() before delivering to agent.
        Returns True if valid, False if corrupted.
        
        Permissive: envelopes without integrity field return True (backward compatible)
        but emit advisory event for visibility in tests.
        """
        # No integrity field: older plugin or legacy message
        if "integrity" not in envelope:
            # Log advisory event for strict compliance checking in tests
            if self.engine:
                self.engine.emit(
                    "town",
                    "integrity_not_present",
                    envelope.get("message_id", "unknown"),
                    {"reason": "envelope missing integrity field"},
                )
            return True  # Permissive: don't reject, just warn
        
        # Missing body: malformed
        if "body" not in envelope:
            return False
        
        # Recalculate and compare
        expected = self._integrity(envelope["body"])
        recorded = envelope.get("integrity")
        
        return expected == recorded
