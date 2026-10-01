"""A poison request: one its consumer can never process.

Without a budget the Track redelivers it forever and never tells the
sender. With max_attempts in the profile, the town dead-letters it and
tells the sender once. Public HTTP only; on main every case here fails.
"""

import pytest
from fastapi.testclient import TestClient

from nandatown import coordinator
from nandatown.coordinator import build_app
from nandatown.records import TestProfile

ADMIN = {"X-Town-Admin": "secret"}
BODY = {"sku": "widget", "quantity": 2, "unit_price_cents": 1995}
LEASE = 5.0


class Clock:
    now = 1_000_000.0

    def time(self):
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(coordinator, "time", fake)
    return fake


@pytest.fixture()
def town(tmp_path, clock):
    with TestClient(build_app(str(tmp_path / "t.db"), "secret")) as client:
        yield client


def profile(max_attempts=3, fault="none"):
    p = TestProfile(name="poison", task={**BODY, "kind": "quote",
                                         "expected_total_cents": 3990},
                    roles={"buyer": "buyer", "seller": "seller"},
                    capabilities={"buyer": [], "seller": ["quote.read"]},
                    fault=fault, lease_seconds=LEASE,
                    evaluator="stage-evaluator").model_dump()
    return {**p, "max_attempts": max_attempts} if max_attempts else p


def start(town, max_attempts=3, fault="none"):
    r = town.post("/runs", json={"profile": profile(max_attempts, fault)},
                  headers=ADMIN)
    run = r.json()["run_id"]
    s = {n: {"X-Town-Session": town.post(
            f"/runs/{run}/join", json={"name": n, "token": t}
         ).json()["session"]} for n, t in r.json()["join_tokens"].items()}
    send(town, run, s["buyer"])
    return run, s


def send(town, run, who):
    return town.post(f"/runs/{run}/messages", headers=who, json={
        "message_id": "q-1", "to": "seller", "kind": "quote_request",
        "body": BODY})


def claim(town, run, who):
    r = town.post(f"/runs/{run}/inbox/claim", headers=who)
    return r.json() if r.status_code == 200 else None


def ack(town, run, who, c, status):
    return town.post(f"/runs/{run}/inbox/ack", headers=who, json={
        "message_id": c["message_id"], "fence": c["fence"], "status": status})


def events(town, run, kind):
    return [e for e in town.get(f"/runs/{run}/events",
                                headers=ADMIN).json()["events"]
            if e["kind"] == kind]


def fail_deliveries(town, clock, run, who, how, limit=8):
    """Fail each delivery the given way until none is handed out."""
    claims = []
    while len(claims) < limit and (c := claim(town, run, who)):
        claims.append(c)
        if how == "retryable":
            ack(town, run, who, c, "retryable")
        else:
            clock.now += LEASE + 1
            if how == "late_ack":
                assert ack(town, run, who, c, "processed").status_code == 409
    return claims


# retryable ack, lease expiry, an ack that arrives after its lease, and a
# consumer that dies (only the sender's own polling finds the expiry).
@pytest.mark.parametrize("how", ["retryable", "lease_expired", "late_ack",
                                 "abandoned"])
def test_a_poison_request_is_dead_lettered_and_its_sender_told_once(
        town, clock, how):
    run, s = start(town)
    limit = 3 if how == "abandoned" else 8
    claims = fail_deliveries(town, clock, run, s["seller"],
                             "lease_expired" if how == "abandoned" else how,
                             limit)
    assert [c["attempt"] for c in claims] == [1, 2, 3]
    notice = claim(town, run, s["buyer"])
    assert notice and notice["from"] == "town", "the buyer is never told"
    assert (notice["kind"], notice["body"]["request_id"]) == (
        "dead_letter", "q-1")
    assert [(d["subject"], d["detail"]["attempts"])
            for d in events(town, run, "message_dead_lettered")] == [
        ("q-1", 3)]
    assert ack(town, run, s["buyer"], notice, "processed").status_code == 200
    assert claim(town, run, s["buyer"]) is None
    # Ids are idempotency keys: a resend replays and delivers nothing.
    assert send(town, run, s["buyer"]).json()["replay"] is True
    assert claim(town, run, s["seller"]) is None


@pytest.mark.parametrize("refusal", ["failed", "rejected"])
def test_a_refused_request_is_dead_lettered_at_once(town, refusal):
    run, s = start(town)
    ack(town, run, s["seller"], claim(town, run, s["seller"]), refusal)
    assert claim(town, run, s["seller"]) is None
    assert claim(town, run, s["buyer"])["body"] == {
        "request_id": "q-1", "kind": "quote_request", "attempts": 1,
        "last_outcome": refusal}


def test_refusing_a_reoffer_of_done_work_changes_nothing(town):
    run, s = start(town, fault="duplicate_delivery")
    ack(town, run, s["seller"], claim(town, run, s["seller"]), "processed")
    ack(town, run, s["seller"], claim(town, run, s["seller"]), "failed")
    assert claim(town, run, s["buyer"]) is None


@pytest.mark.parametrize("how", ["retryable", "failed"])
def test_without_a_budget_nothing_changes(town, clock, how):
    run, s = start(town, max_attempts=None)
    if how == "retryable":
        assert len(fail_deliveries(town, clock, run, s["seller"], how)) == 8
    else:
        ack(town, run, s["seller"], claim(town, run, s["seller"]), how)
    assert claim(town, run, s["buyer"]) is None
    assert not events(town, run, "message_dead_lettered")


def test_a_notice_that_runs_out_is_not_noticed_again(town, clock):
    run, s = start(town)
    fail_deliveries(town, clock, run, s["seller"], "retryable")
    assert len(fail_deliveries(town, clock, run, s["buyer"],
                               "retryable")) == 3
    assert [d["detail"]["notice"] is None for d in
            events(town, run, "message_dead_lettered")] == [False, True]


@pytest.mark.parametrize("max_attempts", [3, None])
def test_participants_learn_the_budget_when_they_join(town, max_attempts):
    r = town.post("/runs", json={"profile": profile(max_attempts)},
                  headers=ADMIN).json()
    j = town.post(f"/runs/{r['run_id']}/join", json={
        "name": "seller", "token": r["join_tokens"]["seller"]})
    assert j.json()["run"]["max_attempts"] == max_attempts


def test_no_participant_can_take_the_town_s_name(town):
    p = {**profile(), "roles": {"buyer": "buyer", "town": "seller"},
         "capabilities": {"buyer": [], "town": []}}
    assert town.post("/runs", json={"profile": p},
                     headers=ADMIN).status_code == 422
