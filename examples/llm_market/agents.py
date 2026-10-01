"""Bring-your-own buyers for a sealed-data market: decisions by a model.

Loaded by llm_market.yaml through plugin_files. Each llm-data-buyer asks
a language model, by tool call, which seller to buy a dataset from and
what to do after an order fails. The sellers are the town's data-seller
role; the buyers are the participants under test.

The model runs behind an OpenAI-compatible endpoint (a local Ollama by
default). Set LLM_MARKET_MODEL to pick the model and TOWN_MODEL_URL to
point elsewhere. `scripted:v1` is a fixed policy with no inference, for
tests; it is labeled as such in every recorded decision.

A real model is not reproducible from the seed. Every decision is
recorded as a model_decision event (tool, arguments, whether the
guardrail accepted it), so the evidence shows what the model chose even
though a rerun may choose differently.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from nandatown.layers.payments import digest, unseal
from nandatown.sim.agents import SimAgent, role

MAX_INVALID = 2


def _tool(name: str, description: str, properties: dict[str, Any]):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": sorted(properties)}}}


REASON = {"type": "string", "description": "one short sentence"}

TOOLS = {
    "choose": [
        _tool("buy_from", "Buy the dataset from one listed seller.",
              {"seller": {"type": "string"}, "reason": REASON}),
        _tool("give_up", "Stop shopping.", {"reason": REASON}),
    ],
    "failed_order": [
        _tool("shop_again", "Try another seller.", {"reason": REASON}),
        _tool("give_up", "Stop shopping.", {"reason": REASON}),
    ],
}

SYSTEM = (
    "You are a purchasing agent buying the dataset {sku} for your"
    " principal from other agents. Never pay more than {cap} cents."
    " Sellers deliver the data sealed; you pay into escrow and the escrow"
    " releases the key only if the data matches the listing. Reputation"
    " is the town's public score: each failed order lowers it. A lower"
    " price is better, but an order that fails costs time. Always answer"
    " by calling exactly one of the tools you are given."
)


class ScriptedBrain:
    """No inference: a fixed policy (cheapest listing), for tests and CI."""

    model = "scripted:v1"

    def decide(self, messages, tools):
        state = json.loads(messages[1]["content"])
        if "buy_from" in [t["function"]["name"] for t in tools]:
            best = min(state["listings"],
                       key=lambda x: (x["price_cents"], x["seller"]))
            return "buy_from", {"seller": best["seller"],
                                "reason": "cheapest listing"}
        return "shop_again", {"reason": "the order failed"}


class ModelBrain:
    """Tool calls from an OpenAI-compatible chat endpoint."""

    def __init__(self, model: str):
        self.model = model
        base = os.environ.get("TOWN_MODEL_URL", "http://localhost:11434/v1")
        headers = {}
        if os.environ.get("TOWN_MODEL_KEY"):
            headers["Authorization"] = f"Bearer {os.environ['TOWN_MODEL_KEY']}"
        self.http = httpx.Client(base_url=base, headers=headers,
                                 timeout=180.0)

    def decide(self, messages, tools):
        r = self.http.post("/chat/completions", json={
            "model": self.model, "messages": messages, "tools": tools,
            "tool_choice": "required", "temperature": 0, "seed": 7})
        r.raise_for_status()
        message = r.json()["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            return None, {"text": (message.get("content") or "")[:200]}
        fn = calls[0]["function"]
        try:
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args or "{}")
        except json.JSONDecodeError:
            return fn.get("name"), {"unparseable": True}
        return fn.get("name"), args if isinstance(args, dict) else {}


def make_brain():
    model = os.environ.get("LLM_MARKET_MODEL", "llama3.1:8b")
    return ScriptedBrain() if model == "scripted:v1" else ModelBrain(model)


@role("llm-data-buyer")
class LLMDataBuyer(SimAgent):
    """Chooses sellers and recovers from failed orders by tool call.

    The model proposes; code disposes. A call naming a seller that is
    not listed, or one priced above the principal's cap, is rejected,
    recorded, and asked again, at most MAX_INVALID times. The sealed-box
    protocol itself (verify the box, pay, open with the released key)
    is code, never left to the model.
    """

    def on_start(self):
        self.brain = make_brain()
        c = self.config
        self.cap = c["max_cents"]
        self.system = SYSTEM.format(sku=c["sku"], cap=self.cap)
        self.excluded: list[str] = []
        self.history: list[str] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.attempt = 0
        self.satisfied = False
        self.api.register(["buy"])
        self.api.observe("model_dependency", self.name,
                         {"model": self.brain.model,
                          "note": "mutable dependency; decisions are"
                                  " recorded, not reproducible from seed"})
        self.api.later(c.get("start_at", 0.5), self.shop)

    # decisions

    def _ask(self, stage: str, state: dict[str, Any]):
        messages = [{"role": "system", "content": self.system},
                    {"role": "user", "content": json.dumps(
                        dict(state, history=self.history[-6:]))}]
        for _ in range(MAX_INVALID + 1):
            name, args = self.brain.decide(messages, TOOLS[stage])
            problem = self._check(stage, state, name, args)
            self.api.observe("model_decision", self.name,
                             {"model": self.brain.model, "stage": stage,
                              "tool": name, "args": args,
                              "accepted": problem is None,
                              "problem": problem or ""})
            if problem is None:
                self.history.append(f"{stage}: {name} {json.dumps(args)}")
                return name, args
            messages.append({"role": "user", "content":
                             f"That call was rejected: {problem}."
                             " Call one tool again."})
        return None, {}

    def _check(self, stage, state, name, args) -> str | None:
        allowed = [t["function"]["name"] for t in TOOLS[stage]]
        if name not in allowed:
            return f"tool {name!r} is not one of {allowed}"
        if name == "buy_from":
            listing = {x["seller"]: x for x in state["listings"]}.get(
                args.get("seller"))
            if listing is None:
                return f"seller {args.get('seller')!r} is not listed"
            if listing["price_cents"] > self.cap:
                return (f"{listing['price_cents']} cents is above your cap"
                        f" {self.cap}")
        return None

    # shopping

    def shop(self):
        cards = [c for c in self.api.lookup(f"data.{self.config['sku']}")
                 if c["name"] not in self.excluded]
        listings = [{"seller": c["name"],
                     "price_cents": c["facts"]["price_cents"],
                     "reputation": self.api.reputation(c["name"])}
                    for c in cards]
        if not listings:
            self.api.observe("buyer_gave_up", self.name,
                             {"reason": "no sellers left"})
            return
        name, args = self._ask("choose", {
            "situation": "choose a seller", "listings": listings,
            "cap_cents": self.cap})
        if name != "buy_from":
            self.api.observe("buyer_gave_up", self.name,
                             {"reason": args.get("reason", "no decision")})
            return
        card = next(c for c in cards if c["name"] == args["seller"])
        self.attempt += 1
        order_id = f"order-{self.name}-{self.attempt}"
        self.orders[order_id] = {
            "seller": card["name"], "price": card["facts"]["price_cents"],
            "listing": card["facts"]["content_digest"], "box": None,
            "failed": False, "opened": False}
        self.api.send(card["name"], "box_request",
                      {"order_id": order_id, "sku": self.config["sku"]})
        self.api.later(self.config.get("patience", 2.0),
                       lambda: self._no_box(order_id))

    def _no_box(self, order_id):
        if self.orders[order_id]["box"] is None:
            self._failed(order_id, "no sealed box arrived")

    def handle_sealed_box(self, msg):
        body = msg["body"]
        order = self.orders.get(body["order_id"])
        if order is None or order["box"] is not None or order["failed"]:
            self.api.observe("duplicate_recognized", body["order_id"],
                             {"kind": "sealed_box"})
            return
        box = bytes.fromhex(body["box_hex"])
        if digest(box) != body["box_digest"]:
            self.api.observe("box_rejected", body["order_id"],
                             {"reason": "box digest mismatch",
                              "box_digest": digest(box)})
            self._failed(body["order_id"], "the sealed box was damaged")
            return
        if (body["content_digest"] != order["listing"]
                or self.api.balance() < order["price"]):
            self._failed(body["order_id"], "the box does not match the"
                         " listing or funds are short")
            return
        order["box"] = box
        order["nonce"] = bytes.fromhex(body["nonce_hex"])
        self.api.observe("box_accepted", body["order_id"],
                         {"seller": order["seller"],
                          "box_digest": body["box_digest"],
                          "key_digest": body["key_digest"]})
        if self.api.supports_hashlock():
            self.api.hashlock_hold(order["seller"], order["price"],
                                   body["order_id"], body["key_digest"],
                                   order["listing"], box, order["nonce"])
        else:
            self.api.escrow_hold(order["price"], ref=body["order_id"])
        self.api.send(order["seller"], "escrow_locked",
                      {"order_id": body["order_id"],
                       "key_digest": body["key_digest"]})
        self.api.later(0.5, lambda: self._poll(body["order_id"]))

    def _poll(self, order_id):
        order = self.orders[order_id]
        if order["opened"] or order["failed"]:
            return
        key = self.api.revealed_key(order_id)
        if key is not None:
            self._open(order_id, key)
        elif self.api.escrow_state(order_id) == "refunded":
            self._failed(order_id, "the escrow deadline passed and the"
                         " payment was refunded; the box stays sealed")
        else:
            self.api.later(0.5, lambda: self._poll(order_id))

    def handle_key_release(self, msg):
        # Without a hashlock the seller sends the key itself. A buyer
        # that holds a box and a key opens it, whenever the key arrives.
        order_id = msg["body"]["order_id"]
        order = self.orders.get(order_id)
        if order is None or order["box"] is None or order["opened"]:
            return
        self._open(order_id, bytes.fromhex(msg["body"]["key_hex"]))
        if order["opened"] and self.api.escrow_state(order_id) == "held":
            self.api.escrow_release(order_id, order["seller"])

    def _open(self, order_id, key: bytes):
        order = self.orders[order_id]
        plaintext = unseal(key, order["nonce"], order["box"])
        if plaintext is None or digest(plaintext) != order["listing"]:
            self.api.observe("goods_rejected", order_id,
                             {"reason": "opened content does not match"
                                        " the listing"})
            return
        order["opened"] = True
        self.api.observe("goods_opened", order_id,
                         {"content_digest": digest(plaintext),
                          "escrow": self.api.escrow_state(order_id)})
        if not order["failed"]:
            self.satisfied = True
            self.api.rate(order["seller"], "good")

    # coping when something breaks

    def _failed(self, order_id, what: str):
        order = self.orders[order_id]
        if order["failed"]:
            return
        order["failed"] = True
        self.api.observe("order_failed", order_id,
                         {"seller": order["seller"], "what": what})
        self.api.rate(order["seller"], "bad")
        self.excluded.append(order["seller"])
        if self.satisfied:
            return
        name, args = self._ask("failed_order", {
            "situation": what, "seller": order["seller"],
            "balance_cents": self.api.balance()})
        if name == "shop_again":
            self.api.later(0.5, self.shop)
        else:
            self.api.observe("buyer_gave_up", self.name,
                             {"reason": args.get("reason", "no decision")})
