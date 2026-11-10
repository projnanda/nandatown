"""Tests for the bazaroo.v1 registry plugin (offline: uses the local Bazaroo mock).

Run from the repo root:  python -m unittest discover -s integrations/bazaroo -v
Requires nandatown to be installed (pip install -e ".[dev]").
"""
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bazaroo_client import BazarooClient  # noqa: E402
from mock_bazaroo import MockBazaroo  # noqa: E402
from registry_bazaroo_v1 import BazarooRegistry, key_env_name  # noqa: E402

KEY_SELLER, KEY_RIVAL, KEY_LOOKUP = "k-seller", "k-rival", "k-lookup"
ENV = {
    key_env_name("seller-a"): KEY_SELLER,
    key_env_name("rival"): KEY_RIVAL,
    "BAZAROO_KEY_LOOKUP": KEY_LOOKUP,
}


class FakeAuth:
    def verify(self, signer, payload, signature, subject=None):
        return signature == "good-signature"


class FakeTrust:
    def score(self, name):
        return 0


class FakeEngine:
    """Just the parts of Town's engine that the registry plugins touch."""

    def __init__(self):
        self.layers = {"auth": FakeAuth(), "trust": FakeTrust()}
        self.events = []

    def emit(self, observer, kind, subject, detail):
        self.events.append((observer, kind, subject, detail))

    def kinds(self):
        return [e[1] for e in self.events]


def card(name="widget-seller", caps=("widgets",), description="Sells widgets"):
    return {"name": name, "capabilities": list(caps), "description": description}


class BazarooRegistryTests(unittest.TestCase):
    def setUp(self):
        self.server = MockBazaroo({KEY_SELLER, KEY_RIVAL, KEY_LOOKUP}).start()
        self.addCleanup(self.server.stop)
        patcher = mock.patch.dict("os.environ", ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = FakeEngine()
        self.registry = self._registry(self.server.url)

    def _registry(self, url, timeout=2):
        return BazarooRegistry(
            self.engine, client_factory=lambda key: BazarooClient(key, url, timeout))

    def test_publish_is_accepted_and_discoverable(self):
        self.assertTrue(self.registry.publish("seller-a", card(), "good-signature"))
        self.assertIn("card_registered", self.engine.kinds())
        self.assertIn("bazaroo_card_accepted", self.engine.kinds())
        found = self.registry.lookup("widgets")
        hit = [e for e in self.engine.events if e[1] == "bazaroo_lookup_hit"][0]
        self.assertEqual(hit[3]["found"], ["widget-seller"])
        self.assertEqual([c["name"] for c in found], ["widget-seller"])
        self.assertEqual(self.registry.names_with("widgets"), ["widget-seller"])

    def test_fake_listing_is_refused_and_real_listing_survives(self):
        """Peaches' failure case: Bazaroo, not Town, refuses the impostor."""
        self.assertTrue(self.registry.publish(
            "seller-a", card(description="The real seller"), "good-signature"))
        ok = self.registry.publish(
            "rival", card(description="FAKE listing"), "good-signature")
        self.assertFalse(ok)
        refused = [e for e in self.engine.events if e[1] == "card_publish_refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0][3]["reason"], "bazaroo: name_taken")
        self.assertEqual(refused[0][3]["outcome"], "refused")
        self.assertIn("bazaroo_name_taken", self.engine.kinds())
        found = self.registry.lookup("widgets")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["description"], "The real seller")

    def test_town_signature_does_not_override_bazaroo(self):
        """Documented design: Bazaroo's key is the authority; Town's check is recorded."""
        self.assertTrue(self.registry.publish("seller-a", card(), "bad-signature"))
        registered = [e for e in self.engine.events if e[1] == "card_registered"][0]
        self.assertIs(registered[3]["town_signature_verified"], False)
        self.assertEqual(registered[3]["authority"], "bazaroo")

    def test_missing_key_is_refused_without_calling_bazaroo(self):
        ok = self.registry.publish("stranger", card("other"), "good-signature")
        self.assertFalse(ok)
        refused = [e for e in self.engine.events if e[1] == "card_publish_refused"][0]
        self.assertIn("BAZAROO_KEY_STRANGER", refused[3]["reason"])
        self.assertEqual(self.server.rows, {})

    def test_bazaroo_down_is_unknown_and_nothing_is_stored(self):
        """Boundary case: no answer from Bazaroo must never look like an accept."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        down = self._registry(f"http://127.0.0.1:{port}", timeout=1)
        self.assertFalse(down.publish("seller-a", card(), "good-signature"))
        refused = [e for e in self.engine.events if e[1] == "card_publish_refused"][0]
        self.assertEqual(refused[3]["outcome"], "unknown")
        self.assertNotIn("card_registered", self.engine.kinds())
        self.assertEqual(down.cards, {})

    def test_lookup_failure_is_reported_not_hidden(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        down = self._registry(f"http://127.0.0.1:{port}", timeout=1)
        self.assertEqual(down.lookup("widgets"), [])
        failed = [e for e in self.engine.events if e[1] == "registry_lookup_failed"]
        self.assertEqual(failed[0][3]["outcome"], "unknown")

    def test_empty_lookup_is_recorded_as_empty_not_failed(self):
        self.registry.publish("seller-a", card(), "good-signature")
        self.assertEqual(self.registry.lookup("nothing-sells-this"), [])
        self.assertIn("bazaroo_lookup_empty", self.engine.kinds())
        self.assertNotIn("registry_lookup_failed", self.engine.kinds())

    def test_lookup_is_exact_on_capability(self):
        self.registry.publish("seller-a", card(caps=("widgets-deluxe",)), "good-signature")
        self.assertEqual(self.registry.lookup("widgets"), [])
        self.assertEqual(len(self.registry.lookup("widgets-deluxe")), 1)

    def test_key_env_name(self):
        self.assertEqual(key_env_name("seller-a"), "BAZAROO_KEY_SELLER_A")
        self.assertEqual(key_env_name("buyer.1"), "BAZAROO_KEY_BUYER_1")


if __name__ == "__main__":
    unittest.main()
