"""A buyer must not take delivery and then take its escrow back.

This file imports only what upstream main already has, so it runs on
main too: there it fails by behaviour (the buyer gets its money back),
and with bound.v1 registered it passes.
"""

import pytest

from nandatown.layers import plugins
from nandatown.layers.payments import PaymentError
from nandatown.sim.agents import ROLES, Buyer
from nandatown.sim.runner import build_engine
from nandatown.sim.scenario import ScenarioSpec


class _TakesEscrowBack(Buyer):
    """Funds escrow, takes delivery, then tries to keep the money."""

    def take_back(self, order_id):
        raise NotImplementedError

    def handle_delivery(self, msg):
        order_id = msg["body"]["order_id"]
        if order_id in self.released:
            return
        self.released.add(order_id)
        try:
            self.take_back(order_id)
        except PaymentError:
            self.api.observe("take_back_refused", order_id, {})


class ReleasesToItself(_TakesEscrowBack):
    def take_back(self, order_id):
        self.api.escrow_release(order_id, self.name)


class RefundsToItself(_TakesEscrowBack):
    def take_back(self, order_id):
        self.api.escrow_refund(order_id)


def _payments_plugin():
    registered = {entry["plugin_id"] for entry in plugins()["payments"]}
    return "bound.v1" if "bound.v1" in registered else "ledger.v1"


@pytest.mark.parametrize("attacker", [ReleasesToItself, RefundsToItself],
                         ids=["release-to-self", "refund-to-self"])
def test_buyer_cannot_take_delivery_and_keep_its_escrow(monkeypatch, attacker):
    role = "test_" + attacker.__name__
    monkeypatch.setattr(attacker, "role", role, raising=False)
    monkeypatch.setitem(ROLES, role, attacker)
    spec = ScenarioSpec.model_validate({
        "name": "escrow_theft",
        "seed": 42,
        "layers": {"payments": _payments_plugin()},
        "agents": [
            {"name": "seller-a", "role": "seller",
             "config": {"sku": "widget", "ask_cents": 2000,
                        "floor_cents": 1800, "stock": 10,
                        "balance_cents": 0}},
            {"name": "mallory", "role": role,
             "config": {"sku": "widget", "quantity": 2, "cap_cents": 1900,
                        "balance_cents": 10000, "rounds": 1}},
        ],
        "max_time": 60,
    })
    engine = build_engine(spec)
    engine.run()
    payments = engine.layers["payments"]

    delivered = [e for e in engine.events
                 if e.kind == "message_delivered"
                 and e.detail.get("kind") == "delivery"
                 and e.detail.get("to") == "mallory"]
    assert delivered, "the attack needs the goods to have been delivered"

    assert payments.balance("mallory") == 10000 - 3600, (
        f"mallory took delivery and kept its money under"
        f" {spec.layers['payments']}: balance {payments.balance('mallory')},"
        f" escrow {payments.escrow}")
