"""bazaroo.v1: a registry plugin where Bazaroo, not Town, decides who owns a name.

Town's default index.v1 stores whatever card arrives under a name. This plugin
sends every publish to Bazaroo's register endpoint and every lookup to its
search endpoint, and passes Bazaroo's answer through unchanged:

    Bazaroo 201            -> listing accepted
    Bazaroo 409 name_taken -> listing REFUSED; the existing listing is untouched
    Bazaroo 401 / 400      -> listing refused (config or malformed card)
    Bazaroo unreachable    -> listing NOT stored and recorded as "unknown",
                              never silently accepted

Evidence events (besides Town's standard card_registered / card_publish_refused):
bazaroo_card_accepted, bazaroo_name_taken, bazaroo_lookup_hit, bazaroo_lookup_empty.
The validators in validators_bazaroo.py check these.

Keys: each Town agent publishes under its own Bazaroo API key, read from an
environment variable named BAZAROO_KEY_<PUBLISHER> (publisher upper-cased, any
non-alphanumeric character replaced by "_"), e.g. seller-a -> BAZAROO_KEY_SELLER_A.
Lookups use BAZAROO_KEY_LOOKUP, or the key of the last successful publisher.
BAZAROO_BASE_URL overrides the server (default https://bazaaro.ryan-teo.com) and
BAZAROO_TIMEOUT sets the per-request timeout in seconds (default 10).
Keys are never stored in code or in the evidence bundle.

Design note: Town's own signature check is still run and recorded as
`town_signature_verified` in the event, but it does not gate the decision.
Bazaroo's API key is the authority for name ownership here.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:  # lets the plugin import bazaroo_client from its own folder
    sys.path.insert(0, _HERE)

from nandatown.layers import register  # noqa: E402
from nandatown.layers.registry_layer import TownIndex  # noqa: E402

from bazaroo_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    BazarooClient,
    BazarooError,
    NameTaken,
    Unavailable,
)


def key_env_name(publisher: str) -> str:
    """BAZAROO_KEY_<PUBLISHER>, e.g. 'seller-a' -> 'BAZAROO_KEY_SELLER_A'."""
    return "BAZAROO_KEY_" + re.sub(r"[^A-Za-z0-9]", "_", publisher).upper()


@register("registry", "bazaroo.v1")
class BazarooRegistry(TownIndex):
    """bazaroo.v1: Bazaroo decides name ownership; a taken name is refused."""

    def __init__(self, engine, client_factory=None):
        super().__init__(engine)
        self._client_factory = client_factory or self._default_client
        self._lookup_key = ""

    @staticmethod
    def _default_client(key: str) -> BazarooClient:
        return BazarooClient(
            key,
            os.environ.get("BAZAROO_BASE_URL", DEFAULT_BASE_URL),
            float(os.environ.get("BAZAROO_TIMEOUT", "10")),
        )

    def _town_verified(self, name: str, card: dict[str, Any], signature: str) -> bool:
        try:
            auth = self.engine.layers["auth"]
            return bool(auth.verify(name, card, signature, subject=name))
        except Exception:  # recorded only; never gates the decision
            return False

    def _refuse(self, card: dict[str, Any], publisher: str, reason: str,
                outcome: str = "refused") -> bool:
        self.engine.emit(
            "town", "card_publish_refused", card["name"],
            {"capabilities": card.get("capabilities", []), "publisher": publisher,
             "reason": reason, "outcome": outcome, "authority": "bazaroo"},
        )
        return False

    def publish(self, publisher: str, card: dict[str, Any], signature: str) -> bool:
        name = card["name"]
        capabilities = list(card["capabilities"])
        description = str(card.get("description") or f"{name} (Nanda Town agent)")
        env = key_env_name(publisher)
        key = os.environ.get(env, "")
        if not key:
            return self._refuse(card, publisher, f"no Bazaroo key configured ({env})")
        town_ok = self._town_verified(name, card, signature)
        try:
            agent_id = self._client_factory(key).register(name, capabilities, description)
        except NameTaken:
            self.engine.emit("town", "bazaroo_name_taken", name,
                             {"publisher": publisher, "authority": "bazaroo"})
            return self._refuse(card, publisher, "bazaroo: name_taken")
        except Unavailable as e:
            return self._refuse(card, publisher, f"bazaroo unavailable: {e}", outcome="unknown")
        except BazarooError as e:
            return self._refuse(card, publisher, f"bazaroo: {e}")
        self.cards[name] = {"card": dict(card), "verified": True,
                            "publisher": publisher, "agent_id": agent_id}
        self._lookup_key = key
        self.engine.emit(
            "town", "card_registered", name,
            {"capabilities": capabilities, "verified": True, "authority": "bazaroo",
             "agent_id": agent_id, "town_signature_verified": town_ok},
        )
        self.engine.emit("town", "bazaroo_card_accepted", name,
                         {"agent_id": agent_id, "publisher": publisher})
        return True

    def lookup(self, capability: str, include_unverified: bool = False) -> list[dict[str, Any]]:
        key = os.environ.get("BAZAROO_KEY_LOOKUP", "") or self._lookup_key
        if not key:
            self.engine.emit("town", "registry_lookup_failed", capability,
                             {"reason": "no Bazaroo key configured for lookups",
                              "outcome": "unknown"})
            return []
        try:
            records = self._client_factory(key).search(capability)
        except BazarooError as e:  # [] here means "unknown", not "nobody"; see the event
            self.engine.emit("town", "registry_lookup_failed", capability,
                             {"reason": str(e), "outcome": "unknown"})
            return []
        cards = [
            {"name": r.name, "capabilities": list(r.capabilities),
             "description": r.description, "agent_id": r.id, "source": r.source}
            for r in records
            if capability in r.capabilities  # Bazaroo search is substring; Town wants exact
        ]
        self.engine.emit(
            "town", "bazaroo_lookup_hit" if cards else "bazaroo_lookup_empty", capability,
            {"found": [c["name"] for c in cards], "authority": "bazaroo"},
        )
        trust = self.engine.layers["trust"]
        return sorted(cards, key=lambda c: (-trust.score(c["name"]), c["name"]))
