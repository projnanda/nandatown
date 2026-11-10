"""Tests for the Bazaroo client. Offline by default; live test is opt-in.

Run offline:  python -m unittest discover -s integrations/bazaroo -v
Run live:     set BAZAROO_KEY_AGENT1 and BAZAROO_KEY_AGENT2 (and optionally
              BAZAROO_BASE_URL), then run the same command.
"""
import os
import socket
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bazaroo_client import (  # noqa: E402
    BadRequest, BazarooClient, NameTaken, Unauthorized, Unavailable,
)
from mock_bazaroo import MockBazaroo  # noqa: E402

KEY_SELLER, KEY_RIVAL = "test-key-seller", "test-key-rival"


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockBazaroo({KEY_SELLER, KEY_RIVAL}).start()
        self.addCleanup(self.mock.stop)
        self.seller = BazarooClient(KEY_SELLER, self.mock.url, timeout=2)
        self.rival = BazarooClient(KEY_RIVAL, self.mock.url, timeout=2)

    def test_register_then_discover(self):
        """Happy path: one agent registers, another finds it via Bazaroo."""
        agent_id = self.seller.register("widget-seller", ["widgets"], "Sells widgets")
        self.assertTrue(agent_id)
        found = self.rival.search("widgets")
        self.assertEqual([a.name for a in found], ["widget-seller"])
        self.assertEqual(found[0].id, agent_id)

    def test_same_key_reregister_updates(self):
        a = self.seller.register("widget-seller", ["widgets"], "v1")
        b = self.seller.register("Widget-Seller", ["widgets"], "v2")
        self.assertEqual(a, b)
        self.assertEqual(self.rival.search("widgets")[0].description, "v2")

    def test_fake_listing_is_refused_and_real_one_survives(self):
        """Failure case: a rival cannot replace a real seller's listing."""
        self.seller.register("widget-seller", ["widgets"], "The real seller")
        with self.assertRaises(NameTaken):
            self.rival.register("widget-seller", ["widgets"], "FAKE listing")
        found = self.rival.search("widgets")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "The real seller")

    def test_name_match_is_case_insensitive(self):
        self.seller.register("widget-seller", ["widgets"], "real")
        with self.assertRaises(NameTaken):
            self.rival.register("WIDGET-SELLER", ["widgets"], "fake")

    def test_reserved_builtin_name_refused(self):
        with self.assertRaises(NameTaken):
            self.seller.register("Dara", ["skin"], "pretending to be built-in")

    def test_unknown_key_is_unauthorized(self):
        stranger = BazarooClient("not-a-real-key", self.mock.url, timeout=2)
        with self.assertRaises(Unauthorized):
            stranger.search("widgets")
        with self.assertRaises(Unauthorized):
            stranger.register("x", ["y"], "z")

    def test_bad_body_is_rejected(self):
        with self.assertRaises(BadRequest):
            self.seller.register("", ["widgets"], "empty name")

    def test_unreachable_is_unavailable_not_accept_or_reject(self):
        """Boundary case: Bazaroo down. Must be 'unknown', never a silent accept."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listening on this port now
        down = BazarooClient(KEY_SELLER, f"http://127.0.0.1:{port}", timeout=1)
        with self.assertRaises(Unavailable):
            down.register("widget-seller", ["widgets"], "x")
        with self.assertRaises(Unavailable):
            down.search("widgets")

    def test_slow_response_times_out_as_unavailable(self):
        """Boundary case: Bazaroo answers too slowly."""
        slow = MockBazaroo({KEY_SELLER}, delay=1.5).start()
        self.addCleanup(slow.stop)
        client = BazarooClient(KEY_SELLER, slow.url, timeout=0.3)
        with self.assertRaises(Unavailable):
            client.search("widgets")

    def test_missing_key_env_var_fails_fast(self):
        os.environ.pop("BAZAROO_TEST_UNSET", None)
        with self.assertRaises(ValueError):
            BazarooClient.from_env("BAZAROO_TEST_UNSET")


@unittest.skipUnless(
    os.environ.get("BAZAROO_KEY_AGENT1") and os.environ.get("BAZAROO_KEY_AGENT2"),
    "set BAZAROO_KEY_AGENT1 and BAZAROO_KEY_AGENT2 to run the live test",
)
class LiveTests(unittest.TestCase):
    """Hits the real Bazaroo. Uses a unique name per run so reruns never collide."""

    def test_live_register_discover_and_duplicate_refusal(self):
        a1 = BazarooClient.from_env("BAZAROO_KEY_AGENT1")
        a2 = BazarooClient.from_env("BAZAROO_KEY_AGENT2")
        name = f"town-live-{uuid.uuid4().hex[:8]}"
        agent_id = a1.register(name, ["nandatown-live-test"], "Live test seller")
        self.assertTrue(agent_id)
        self.assertIn(name, [a.name for a in a2.search(name)])
        with self.assertRaises(NameTaken):
            a2.register(name, ["nandatown-live-test"], "impostor")


if __name__ == "__main__":
    unittest.main()
