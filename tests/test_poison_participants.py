"""The stock seller fails every delivery under poison_request; the stock
buyer must stop waiting once the town dead-letters its request."""

import threading

from fastapi.testclient import TestClient

from nandatown.client import TownClient
from nandatown.coordinator import build_app
from nandatown.participants import buyer, seller
from nandatown.records import TestProfile


def test_the_buyer_stops_waiting_once_its_request_is_dead_lettered(tmp_path):
    app = build_app(str(tmp_path / "t.db"), "secret")
    profile = TestProfile(
        name="p", task={"kind": "quote", "sku": "widget", "quantity": 2,
                        "unit_price_cents": 1995,
                        "expected_total_cents": 3990},
        roles={"buyer": "buyer", "seller": "seller"},
        capabilities={"buyer": [], "seller": ["quote.read"]},
        fault="poison_request", lease_seconds=5.0,
        evaluator="stage-evaluator", max_attempts=3)
    r = TestClient(app).post("/runs", json={"profile": profile.model_dump()},
                             headers={"X-Town-Admin": "secret"}).json()

    def join(role):
        (tmp_path / role).mkdir()
        return (TownClient("http://testserver", r["run_id"],
                           http=TestClient(app)),
                role, r["join_tokens"][role], str(tmp_path / role))

    threading.Thread(target=seller.run, daemon=True,
                     args=(*join("seller"), "poison_request"),
                     kwargs={"deadline_seconds": 20.0}).start()
    # Before this change the buyer rejected the notice and waited out its
    # deadline (EXIT_NO_RESPONSE).
    assert buyer.run(*join("buyer"),
                     deadline_seconds=20.0) == buyer.EXIT_DEAD_LETTER
