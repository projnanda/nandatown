"""Tests for the Bazaroo validators, using a stand-in for Town's trace object.

Town's _check returns a StageResult(name, status, evidence, note); status is "passed"
for a pass. A check with an empty evidence list is "missing" (inconclusive), so the
validators attach failure events as evidence and a failure is reported as failed.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validators_bazaroo as v  # noqa: E402


class FakeTrace:
    def __init__(self, **kinds):
        self.kinds = kinds

    def ids(self, kind):
        return self.kinds.get(kind, [])


def statuses(checks):
    return {c.name: c.status for c in checks}


def passed(checks):
    return {name: status == "passed" for name, status in statuses(checks).items()}


class DiscoveryValidatorTests(unittest.TestCase):
    def test_passes_when_accepted_and_found(self):
        t = FakeTrace(bazaroo_card_accepted=["ev-6", "ev-9"], bazaroo_lookup_hit=["ev-10"])
        self.assertEqual(passed(v.bazaroo_discovery(None, t)),
                         {"bazaroo_registration": True, "bazaroo_discovery": True})

    def test_registration_fails_if_anyone_was_refused(self):
        t = FakeTrace(bazaroo_card_accepted=["ev-6"], card_publish_refused=["ev-9"],
                      bazaroo_lookup_hit=["ev-10"])
        result = v.bazaroo_discovery(None, t)
        self.assertFalse(passed(result)["bazaroo_registration"])
        self.assertIn("ev-9", result[0].evidence)  # the refusal is cited

    def test_discovery_fails_when_lookup_empty_or_failed(self):
        empty = FakeTrace(bazaroo_card_accepted=["ev-6"], bazaroo_lookup_empty=["ev-10"])
        r1 = v.bazaroo_discovery(None, empty)
        self.assertFalse(passed(r1)["bazaroo_discovery"])
        self.assertEqual(r1[1].evidence, ["ev-10"])
        failed = FakeTrace(bazaroo_card_accepted=["ev-6"], bazaroo_lookup_hit=["ev-10"],
                           registry_lookup_failed=["ev-11"])
        self.assertFalse(passed(v.bazaroo_discovery(None, failed))["bazaroo_discovery"])

    def test_nothing_recorded_is_not_a_pass(self):
        self.assertFalse(any(passed(v.bazaroo_discovery(None, FakeTrace())).values()))


class NameTakenValidatorTests(unittest.TestCase):
    def test_passes_when_refused_and_real_listing_found(self):
        t = FakeTrace(bazaroo_name_taken=["ev-6"], bazaroo_lookup_hit=["ev-10"])
        self.assertEqual(passed(v.bazaroo_name_taken(None, t)),
                         {"bazaroo_refuses_taken_name": True, "real_listing_survives": True})

    def test_fails_when_no_refusal_happened(self):
        t = FakeTrace(bazaroo_card_accepted=["ev-6"], bazaroo_lookup_hit=["ev-10"])
        result = v.bazaroo_name_taken(None, t)
        self.assertFalse(passed(result)["bazaroo_refuses_taken_name"])
        self.assertEqual(result[0].evidence, ["ev-6"])  # cites the unexpected accept

    def test_fails_when_real_listing_is_gone(self):
        t = FakeTrace(bazaroo_name_taken=["ev-6"], bazaroo_lookup_empty=["ev-10"])
        self.assertFalse(passed(v.bazaroo_name_taken(None, t))["real_listing_survives"])


if __name__ == "__main__":
    unittest.main()
