"""Adapter tests for examples/truemenu (no network, no TrueMenu needed).

These check how the TrueMenu seller adapter maps quote requests onto
TrueMenu's find_items answers, and how the lunch buyer judges responses.
They use canned TrueMenu answers, so they are adapter unit tests, not
evidence that TrueMenu is compatible: that evidence comes from
examples/truemenu/check.sh run against a live TrueMenu.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "truemenu"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seller = _load("truemenu_seller")
buyer = _load("lunch_buyer")


def _item(item_id: str, amount: float, category: str = "bowl",
          quote: bool = True, freshness: str = "fresh") -> dict:
    item = {"item_id": item_id, "name": item_id.title(), "category": category,
            "merchant": {"id": "cava-kendall", "name": "CAVA"},
            "price": {"amount": amount, "currency": "USD",
                      "source": "merchant_stated"},
            "freshness": {"label": freshness, "age_s": 3}}
    if quote:
        item["quote_id"] = f"tq1.{item_id}"
    else:
        item["quote_refused_reason"] = "source stale"
    return item


REQUEST = {"near": "Kendall Square", "budget_usd_max": 20,
           "exclude": ["pork"], "diet": ["low_carb"],
           "available_at": "2026-11-16T12:30"}


def test_menu_request_becomes_a_find_items_query():
    seen = {}

    def find_items(query):
        seen.update(query)
        return {"results": [_item("cava-greek-salad", 12.25)], "count": 1}

    body = seller.answer("q-1", {"menu_request": dict(REQUEST, extra="x")},
                         find_items)
    assert seen == dict(REQUEST, limit=8)          # unknown fields dropped
    assert body["status"] == "quoted"
    assert body["offers"][0] == {
        "merchant_id": "cava-kendall", "merchant_name": "CAVA",
        "item_id": "cava-greek-salad", "name": "Cava-Greek-Salad",
        "category": "bowl", "total_cents": 1225, "currency": "USD",
        "price_source": "merchant_stated", "freshness": "fresh",
        "data_age_s": 3, "quote_id": "tq1.cava-greek-salad"}


def test_no_quotable_item_is_a_decline_with_truemenus_reasons():
    body = seller.answer("q-1", {"menu_request": REQUEST}, lambda q: {
        "results": [_item("cava-greek-salad", 12.25, quote=False,
                          freshness="stale")], "count": 1})
    assert body["status"] == "declined"
    assert body["reason"] == "no_verifiable_quote"
    assert body["quote_refused"] == ["source stale"]
    assert "offers" not in body


def test_widget_request_is_declined_not_guessed():
    body = seller.answer("q-1", {"sku": "widget", "quantity": 2,
                                 "unit_price_cents": 1995},
                         lambda q: pytest.fail("TrueMenu must not be called"))
    assert body == {"request_id": "q-1", "status": "declined",
                    "reason": "unsupported_request",
                    "detail": "TrueMenu quotes menu items; send a menu_request"}


def test_unreachable_truemenu_is_a_decline():
    def down(query):
        raise seller.Unavailable("TrueMenu unreachable: URLError")

    body = seller.answer("q-1", {"menu_request": REQUEST}, down)
    assert body["status"] == "declined"
    assert body["reason"] == "truemenu_unavailable"


def test_redelivery_resends_the_first_reply(tmp_path):
    calls = []

    def find_items(query):
        calls.append(query)                     # a new signature every call
        return {"results": [_item("cava-greek-salad", 12.25)],
                "count": 1}

    handler = seller.build_handler(
        seller.Journal(str(tmp_path / "journal.db")), find_items)
    claim = {"kind": "quote_request", "message_id": "q-1", "from": "buyer",
             "body": {"menu_request": REQUEST}}
    first = handler(claim)
    second = handler(dict(claim, duplicate=True))
    assert len(calls) == 1
    assert second[0] == "processed" and second[1]["duplicate"] is True
    assert second[2] == first[2]


def _verified(offer, **changes):
    result = {"valid_signature": True, "issued_by_this_service": True,
              "expired": False, "price_unchanged": True,
              "item_id": offer["item_id"],
              "quoted_price": offer["total_cents"] / 100, "verdict": "valid"}
    result.update(changes)
    return result


def _quoted(*items):
    return seller.answer("q-1", {"menu_request": REQUEST},
                         lambda q: {"results": list(items)})


def test_buyer_prefers_a_main_dish_and_verifies_it():
    body = _quoted(_item("a4-butternut-soup", 7.0, category="soup"),
                   _item("cava-greek-salad", 12.25))
    note = buyer.judge(body, REQUEST, "quote",
                       lambda qid: _verified(body["offers"][1]))
    assert note["item_id"] == "cava-greek-salad"
    assert note["correct"] is True
    assert all(note["checks"].values())


def test_buyer_accepts_an_aging_source_between_syncs():
    # Found on the first live run: TrueMenu quotes "aging" sources, and a
    # buyer that demanded "fresh" failed whenever it landed between syncs.
    body = _quoted(_item("cava-greek-salad", 12.25, freshness="aging"))
    note = buyer.judge(body, REQUEST, "quote",
                       lambda qid: _verified(body["offers"][0]))
    assert note["correct"] is True


def test_buyer_rejects_a_price_changed_since_the_quote():
    body = _quoted(_item("cava-greek-salad", 12.25))
    note = buyer.judge(body, REQUEST, "quote", lambda qid: _verified(
        body["offers"][0], price_unchanged=False,
        verdict="price_changed_since_quote"))
    assert note["correct"] is False
    assert note["checks"]["price_unchanged_since_quote"] is False


def test_buyer_rejects_an_offer_that_misstates_the_signed_price():
    body = _quoted(_item("cava-greek-salad", 12.25))
    note = buyer.judge(body, REQUEST, "quote", lambda qid: _verified(
        body["offers"][0], quoted_price=13.45))
    assert note["correct"] is False
    assert note["checks"]["offer_price_matches_quote"] is False


@pytest.mark.parametrize("expect,correct", [("decline", True),
                                            ("quote", False)])
def test_buyer_judges_a_decline_against_what_was_expected(expect, correct):
    body = {"request_id": "q-1", "status": "declined",
            "reason": "no_verifiable_quote"}
    note = buyer.judge(body, REQUEST, expect,
                       lambda qid: pytest.fail("nothing to verify"))
    assert note["correct"] is correct
