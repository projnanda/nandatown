#!/usr/bin/env python3
"""Validate a qualification card (standard library only).

    python examples/known_truth/check_card.py cards/some_card.json

Prints one line: VALID-QUALIFYING, VALID-NOT-QUALIFYING, or INVALID plus
the reason. Exit code 0 for a qualifying card, 6 for a valid card that
does not qualify, 7 for an invalid one. The buyer imports the same rules.
"""

import hashlib
import json
import sys

FIELDS = {
    "card_version": int, "issuer": str, "benchmark_id": str,
    "evaluation_run": str, "candidate_id": str, "model": str,
    "checks_total": int, "checks_passed": int, "failed_checks": list,
    "exact_pass": bool, "issued_at": str, "card_sha256": str,
}


def card_hash(card):
    body = {k: v for k, v in card.items() if k != "card_sha256"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_card(path):
    """Return (status, reason, card). status: qualifying | not_qualified |
    card_malformed | card_hash_mismatch."""
    try:
        with open(path, encoding="utf-8") as handle:
            card = json.load(handle)
    except (OSError, ValueError) as exc:
        return "card_malformed", f"cannot read card: {exc}", None
    if not isinstance(card, dict):
        return "card_malformed", "card is not a JSON object", None
    for key, kind in FIELDS.items():
        value = card.get(key)
        # bool is a subclass of int; do not let True pass as a count
        if (key not in card or not isinstance(value, kind)
                or (kind is int and isinstance(value, bool))):
            return "card_malformed", f"field {key!r} missing or wrong type", card
    if not all(isinstance(x, str) for x in card["failed_checks"]):
        return "card_malformed", "failed_checks must be a list of strings", card
    if card_hash(card) != card["card_sha256"]:
        return "card_hash_mismatch", "recomputed hash differs", card
    if card["exact_pass"] is not (card["checks_total"] > 0
                                  and card["checks_passed"]
                                  == card["checks_total"]):
        return "card_malformed", "exact_pass disagrees with the counts", card
    if not card["exact_pass"]:
        return "not_qualified", "exact_pass is false", card
    return "qualifying", "ok", card


if __name__ == "__main__":
    status, reason, _ = check_card(sys.argv[1])
    label = {"qualifying": "VALID-QUALIFYING",
             "not_qualified": "VALID-NOT-QUALIFYING"}.get(
                 status, f"INVALID ({status})")
    print(f"{label}: {reason}")
    sys.exit({"qualifying": 0, "not_qualified": 6}.get(status, 7))
