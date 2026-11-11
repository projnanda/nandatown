"""GridPact A2A adapter (examples/gridpact). The unit tests run anywhere; the integration test needs a
GridPact checkout and is skipped unless GRIDPACT_DIR is set (see examples/gridpact/README.md)."""
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gridpact_a2a", ROOT / "examples" / "gridpact" / "gridpact_a2a.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class FakeGridPact:
    """In-memory stand-in for GridPact's MCP tools, with the request_id behaviour of tag hw8-v1."""

    def __init__(self, url=None, idempotent=True):
        self.idempotent, self.offers, self.commits, self.next, self.challenges = idempotent, {}, {}, 1, []
        self.tools = {"propose": {"inputSchema": {"properties": {"request_id": {}} if idempotent else {}}}}

    def supports_request_id(self):
        return self.idempotent

    def call(self, name, **a):
        if name == "register_agent":
            return {"ok": True, "token": "t-" + a["agent_id"]}
        if name == "propose":
            rid = a.get("request_id")
            terms = tuple(a[k] for k in ("to", "step_from", "step_to", "kwh_per_step", "price_usd_mwh", "penalty_usd_mwh"))
            for oid, (t, r, status) in self.offers.items():
                if rid is not None and r == rid:
                    if t != terms:
                        return {"ok": False, "reason": f"request_id {rid} was already used with different terms"}
                    return {"ok": True, "offer_id": oid, "status": status, "duplicate": True}
            oid, self.next = self.next, self.next + 1
            self.offers[oid] = (terms, rid, "open")
            self.pending = a
            return {"ok": True, "offer_id": oid}
        if name == "respond":
            oid = a["offer_id"]
            terms, rid, _ = self.offers[oid]
            self.offers[oid] = (terms, rid, "accepted")
            self.commits[oid] = {"commitment_id": oid, "offer_id": oid, "seller": terms[0], "kwh_per_step": terms[3],
                                 "price_usd_mwh": terms[4], "stake_usd": 1.0}
            return {"ok": True, "commitment_id": oid}
        if name == "find_counterparties":     # both sellers declare 5000 kW; the nameplates are 5000 and 2500
            return {"ok": True, "counterparties": [{"agent_id": "PVB_17", "declared_flex_kw": 5000.0},
                                                   {"agent_id": "PVB_37", "declared_flex_kw": 5000.0}]}
        if name == "challenge":
            self.challenges.append(a["commitment_id"])
            return {"ok": True, "verdict": "valid", "bounty_usd": 1.0}
        if name == "get_ledger":
            return {"ok": True, "entries": [{"action": "commit", "payload": p} for p in self.commits.values()]}
        raise AssertionError(name)


def make(monkeypatch, idempotent=True, **opts):
    fake = FakeGridPact(idempotent=idempotent)
    monkeypatch.setattr(adapter, "GridPactClient", lambda url: fake)
    return TestClient(adapter.build_app("http://unused", "http://testserver", **opts)), fake


def send(client, body):
    import json
    r = client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
        "role": "user", "messageId": "m", "parts": [{"kind": "text", "text": json.dumps(body)}]}}})
    task = r.json()["result"]
    text = task["artifacts"][0]["parts"][0]["text"]
    try:
        return task["status"]["state"], json.loads(text)
    except json.JSONDecodeError:      # a failed task carries a plain-text reason
        return task["status"]["state"], text


ORDER = {"request_id": "o-1", "sku": "widget", "quantity": 2, "unit_price_cents": 1995}


def test_quote_total_comes_from_the_commitment(monkeypatch):
    client, fake = make(monkeypatch)
    state, quote = send(client, ORDER)
    assert state == "completed" and quote["total_cents"] == 3990 and quote["request_id"] == "o-1"
    assert len(fake.commits) == 1


def test_retry_with_an_idempotent_server_is_one_commitment(monkeypatch):
    client, fake = make(monkeypatch)
    assert send(client, ORDER) == send(client, ORDER)
    assert len(fake.commits) == 1


def test_retry_against_a_server_without_request_id_is_visible(monkeypatch):
    client, fake = make(monkeypatch, idempotent=False)
    first, second = send(client, ORDER)[1], send(client, ORDER)[1]
    assert first["commitment_id"] != second["commitment_id"] and len(fake.commits) == 2


def test_changed_terms_under_the_same_id_are_rejected(monkeypatch):
    client, fake = make(monkeypatch)
    send(client, ORDER)
    state, body = send(client, {**ORDER, "quantity": 5})
    assert state == "rejected" and "different terms" in body["error"] and len(fake.commits) == 1


def test_bad_request_fails_without_touching_gridpact(monkeypatch):
    client, fake = make(monkeypatch)
    state, _ = send(client, {"request_id": "x", "sku": "gadget", "quantity": 2, "unit_price_cents": 1})
    assert state == "failed" and not fake.offers


def test_honest_seller_is_not_challenged(monkeypatch):
    client, fake = make(monkeypatch)
    state, quote = send(client, ORDER)
    assert fake.challenges == [] and quote["audit"] == "not challenged"


def test_over_declared_seller_is_challenged_before_the_quote(monkeypatch):
    client, fake = make(monkeypatch, seller="PVB_37", declared_flex_kw=5000.0, steps=2)
    state, quote = send(client, ORDER)
    assert state == "completed" and fake.challenges == [quote["commitment_id"]] and quote["audit"] == "challenge valid"
    assert quote["total_cents"] == 3990                               # the order's value; delivery is reported separately
    assert list(fake.offers.values())[0][0][3] == 1000.0              # 2 MWh packed into 2 steps


def test_a_repeat_after_the_audit_does_not_challenge_again(monkeypatch):
    client, fake = make(monkeypatch, seller="PVB_37", declared_flex_kw=5000.0, steps=2)
    assert send(client, ORDER) == send(client, ORDER) and len(fake.challenges) == 1


@pytest.mark.skipif(not os.environ.get("GRIDPACT_DIR") or not shutil.which("nandatown"),
                    reason="set GRIDPACT_DIR to a gridpact checkout (tag hw8-v1 or later)")
def test_town_path_profiles_pass_against_real_gridpact(tmp_path):
    env = {**os.environ, "GRIDPACT_PYTHON": os.environ.get("GRIDPACT_PYTHON", "python")}
    script = str(ROOT / "examples" / "gridpact" / "run_check.sh")
    honest = subprocess.run([script, str(tmp_path / "h")], env=env, capture_output=True, text=True, timeout=300).stdout
    assert honest.count("Verdict:   PASSED") == 2, honest
    assert "duplicate_request       Passed" in honest and "conflicting_retry       Passed" in honest
    assert "settled commitment 1: committed 2000 kWh, delivered 2000, shortfall 0" in honest
    liar = subprocess.run([script, str(tmp_path / "l")], env={**env, "VARIANT": "liar"}, capture_output=True,
                          text=True, timeout=300).stdout
    assert liar.count("Verdict:   PASSED") == 2, liar
    assert "challenge on commitment 1: valid" in liar and "625 kWh/step (ordered 1000), challenged" in liar
