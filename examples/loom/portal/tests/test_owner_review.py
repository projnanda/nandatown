"""0.2.0: an unsupported task from an agent that works for the person
becomes a proposal the person decides on (HW9 boundary case)."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from loom_portal.policy import Portal
from loom_portal.record import synthetic_record
from loom_portal.server import serve


def portal():
    return Portal(record=synthetic_record())


def walk(rid, **kw):
    base = {"request_id": rid, "agent_id": "coach-1", "publisher": "stridefit",
            "category": "agent_for_user", "task": "walk_schedule",
            "purpose": "walks after the meals that spike most", "tier": "L1",
            "fields": ["readings"], "at": 0}
    base.update(kw)
    return base


def test_unsupported_task_becomes_proposal_without_data_or_penalty():
    p = portal()
    out = p.request(walk("w1"))
    assert out["decision"] == "pending_owner"
    assert out["reason"] == "needs_owner_approval"
    assert "data" not in out
    assert out["credit_before"] == out["credit_after"] == 60
    assert set(out["supported_tasks"]) == {"shopping_list", "meal_plan", "clinical_visit"}
    assert p.proposal(out["proposal_id"])["status"] == "pending"
    # asking again while pending does not open a second proposal
    again = p.request(walk("w2"))
    assert again["proposal_id"] == out["proposal_id"]
    assert len(p.proposals) == 1


def test_commercial_unsupported_task_is_still_denied_by_default():
    p = portal()
    out = p.request(walk("ad1", publisher="adco", category="commercial", task="targeted_ads"))
    assert out["decision"] == "denied" and out["reason"] == "no_task_basis"
    assert not p.proposals and "data" not in out


def test_owner_approval_grants_exactly_the_approved_scope():
    p = portal()
    pid = p.request(walk("w1"))["proposal_id"]
    p.owner_decide({"proposal_id": pid, "approve": True, "tier": "L1",
                    "fields": ["readings"], "days": 7, "at": 0.1})
    out = p.request(walk("w2", at=0.2))
    assert out["decision"] == "granted" and out["reason"] == "owner_approved"
    assert set(out["data"]) == {"readings"}
    assert out["expires_at"] == pytest.approx(7.2)
    # the rule belongs to this publisher and task only
    other = p.request(walk("x1", agent_id="coach-9", publisher="otherfit", at=0.3))
    assert other["decision"] == "pending_owner"


def test_owner_can_narrow_and_asking_beyond_it_is_overreach():
    p = portal()
    pid = p.request(walk("w1"))["proposal_id"]
    p.owner_decide({"proposal_id": pid, "approve": True, "tier": "L2",
                    "fields": ["glucose_patterns"], "at": 0.1})
    deep = p.request(walk("w2", at=0.2))           # still asks for L1 readings
    assert deep["decision"] == "blocked" and deep["reason"] == "overreach"
    assert deep["credit_after"] == 40
    ok = p.request(walk("w3", tier="L2", fields=["glucose_patterns"], at=0.3))
    assert ok["decision"] == "granted" and set(ok["data"]) == {"glucose_patterns"}


def test_declined_proposal_stays_denied():
    p = portal()
    pid = p.request(walk("w1"))["proposal_id"]
    p.owner_decide({"proposal_id": pid, "approve": False, "at": 0.1})
    out = p.request(walk("w2", at=0.2))
    assert out["decision"] == "denied" and out["reason"] == "owner_declined"
    assert "data" not in out


def test_missing_fields_are_named():
    p = portal()
    with pytest.raises(ValueError, match="missing required field.*at"):
        p.request({k: v for k, v in walk("w1").items() if k != "at"})


def _call(base, method, path, body=None, token=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"X-Loom-Owner-Token": token} if token else {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_owner_endpoints_need_the_token():
    srv = serve(port=0, owner_token="s3cret")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        assert _call(base, "GET", "/v1/policy")[1]["tasks"]["meal_plan"]["tier"] == "L2"
        code, out = _call(base, "POST", "/v1/t/requests", walk("w1"))
        pid = out["proposal_id"]
        assert _call(base, "GET", "/v1/t/owner/proposals")[0] == 403
        assert _call(base, "POST", "/v1/t/owner/decide",
                     {"proposal_id": pid, "approve": True, "at": 0}, token="wrong")[0] == 403
        code, dec = _call(base, "POST", "/v1/t/owner/decide",
                          {"proposal_id": pid, "approve": True, "at": 0}, token="s3cret")
        assert code == 200 and dec["status"] == "approved"
        assert _call(base, "GET", f"/v1/t/proposals/{pid}")[1]["status"] == "approved"
        code, err = _call(base, "POST", "/v1/t/requests", {"request_id": "z"})
        assert code == 400 and "agent_id" in err["error"]
    finally:
        srv.shutdown()


def test_audit_log_is_owner_only_and_holds_no_health_data():
    """Found in HW9 run 2: GET /log returned every agent's delivered data to
    any caller (an agent read it after its own grant)."""
    srv = serve(port=0, owner_token="s3cret")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        clinic = walk("c1", agent_id="clinic-1", publisher="cambridge-clinic", category="care_provider",
                      task="clinical_visit", tier="L2", fields=["glucose_patterns", "alerts"])
        code, out = _call(base, "POST", "/v1/t/requests", clinic)
        assert out["decision"] == "granted" and "alerts" in out["data"]
        assert _call(base, "GET", "/v1/t/log")[0] == 403
        code, log = _call(base, "GET", "/v1/t/log", token="s3cret")
        assert code == 200 and log and all("data" not in e for e in log)
        assert log[0]["fields_granted"] == ["glucose_patterns", "alerts"]
    finally:
        srv.shutdown()
