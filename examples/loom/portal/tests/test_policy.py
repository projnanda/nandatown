import json
import threading
import urllib.request

import pytest

from loom_portal.policy import Portal, BASELINE_CREDIT
from loom_portal.record import synthetic_record
from loom_portal.server import serve


def portal(**seed):
    return Portal(record=synthetic_record(), credit=dict(seed))


def req(rid, **kw):
    base = {"request_id": rid, "agent_id": "a1", "publisher": "freshcart",
            "category": "agent_for_user", "task": "shopping_list",
            "purpose": "weekly groceries", "tier": "L3",
            "fields": ["constraints"], "at": 0}
    base.update(kw)
    return base


def test_shopping_list_gets_l3_once():
    p = portal()
    out = p.request(req("r1"))
    assert out["decision"] == "granted"
    assert out["tier_granted"] == "L3"
    assert out["single_use"] is True
    assert set(out["data"]) == {"constraints"}
    again = p.fetch({"request_id": "f1", "grant_id": out["grant_id"],
                     "agent_id": "a1", "at": 0.1})
    assert again["decision"] == "denied" and again["reason"] == "expired"


def test_overreach_blocked_and_publisher_penalized():
    p = portal()
    out = p.request(req("r1", tier="L1", fields=["readings"]))
    assert out["decision"] == "blocked" and out["reason"] == "overreach"
    assert out["credit_after"] == BASELINE_CREDIT - 20
    assert "data" not in out


def test_extra_field_is_overreach_even_at_allowed_tier():
    p = portal()
    out = p.request(req("r1", fields=["constraints", "alerts"]))
    assert out["decision"] == "blocked" and out["extra_fields"] == ["alerts"]


def test_marketing_default_denied_without_penalty():
    p = portal()
    out = p.request(req("r1", category="commercial", task="targeted_ads",
                        publisher="adco"))
    assert out["decision"] == "denied" and out["reason"] == "no_task_basis"
    assert out["credit_after"] == out["credit_before"]


def test_meal_plan_valid_seven_days_then_expires():
    p = portal()
    g = p.request(req("r1", task="meal_plan", tier="L2",
                      fields=["glucose_patterns", "constraints"], at=1))
    assert g["decision"] == "granted" and g["expires_at"] == 8
    ok = p.fetch({"request_id": "f1", "grant_id": g["grant_id"],
                  "agent_id": "a1", "at": 5})
    assert ok["decision"] == "granted"
    late = p.fetch({"request_id": "f2", "grant_id": g["grant_id"],
                    "agent_id": "a1", "at": 9})
    assert late["decision"] == "denied" and late["reason"] == "expired"


def test_low_credit_changes_friction_not_depth():
    p = portal(freshcart=40)
    g = p.request(req("r1", task="meal_plan", tier="L2",
                      fields=["glucose_patterns"]))
    assert g["decision"] == "granted" and g["tier_granted"] == "L2"
    assert g["friction"]["confirmation"] == "every_time"
    assert g["single_use"] is True


def test_retry_is_idempotent_and_conflict_refused():
    p = portal()
    first = p.request(req("r1", tier="L1", fields=["readings"]))
    second = p.request(req("r1", tier="L1", fields=["readings"]))
    assert second["replay"] is True
    assert second["credit_after"] == first["credit_after"]
    assert p.credit_of("freshcart") == BASELINE_CREDIT - 20
    clash = p.request(req("r1", tier="L3"))
    assert clash["decision"] == "conflict"


def test_retry_arriving_later_is_still_a_replay():
    # Found in the first Nanda Town run: a duplicated message reaches the
    # gateway later, which stamps a later receive time. Same request.
    p = portal()
    first = p.request(req("r1", at=0.11))
    copy = p.request(req("r1", at=0.21))
    assert copy["replay"] is True and copy["decision"] == "granted"
    assert copy["credit_after"] == first["credit_after"]
    assert p.credit_of("freshcart") == BASELINE_CREDIT + 1
    # Single-use grant: the original was the one delivery.
    assert "data" not in copy and copy["data_withheld"] == "expired"


def test_replay_inside_window_still_carries_data():
    p = portal()
    g = p.request(req("r1", task="meal_plan", tier="L2",
                      fields=["glucose_patterns", "constraints"], at=1))
    ok = p.fetch({"request_id": "f1", "grant_id": g["grant_id"],
                  "agent_id": "a1", "at": 5})
    again = p.fetch({"request_id": "f1", "grant_id": g["grant_id"],
                     "agent_id": "a1", "at": 6})
    assert again["replay"] is True
    assert again["data"] == ok["data"]


def test_replay_after_expiry_withholds_data_and_keeps_credit():
    # Retry is idempotent (credit moves once), but whether data may be
    # sent is decided now, not taken from the cache.
    p = portal()
    g = p.request(req("r1", task="meal_plan", tier="L2",
                      fields=["glucose_patterns", "constraints"], at=1))
    ok = p.fetch({"request_id": "f1", "grant_id": g["grant_id"],
                  "agent_id": "a1", "at": 5})
    credit = p.credit_of("freshcart")
    late = p.fetch({"request_id": "f1", "grant_id": g["grant_id"],
                    "agent_id": "a1", "at": 10})
    assert late["replay"] is True and late["decision"] == ok["decision"]
    assert late["credit_after"] == ok["credit_after"]
    assert p.credit_of("freshcart") == credit
    assert "data" not in late and late["data_withheld"] == "expired"
    late_req = p.request(req("r1", task="meal_plan", tier="L2",
                             fields=["glucose_patterns", "constraints"],
                             at=10))
    assert late_req["replay"] is True and "data" not in late_req


def test_http_round_trip():
    server = serve(port=0, seed_credit={"cambridge-clinic": 85})
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/t1/requests"
        body = json.dumps(req("r1", publisher="cambridge-clinic",
                              category="care_provider",
                              task="clinical_visit", tier="L2",
                              fields=["glucose_patterns", "alerts"])).encode()
        r = urllib.request.Request(url, data=body, method="POST",
                                   headers={"Content-Type": "application/json"})
        out = json.loads(urllib.request.urlopen(r, timeout=5).read())
        assert out["decision"] == "granted"
        assert out["friction"]["auto_renew"] is True
        assert out["valid_days"] == 14
    finally:
        server.shutdown()
