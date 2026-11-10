"""FairClear in Nanda Town: sealed second-price procurement as a Town service.

FairClear is a clearing service that AI agents call instead of haggling.
A buyer agent opens a request for quotes (RFQ); each seller agent submits
one sealed, signed bid; the lowest bid wins and is paid the second-lowest
bid (the buyer's reserve if only one bid is eligible); the result is
signed so anyone can check it afterwards.

This file plugs that workflow into the Town Lab:

- roles ``fairclear_clearing``, ``fairclear_buyer`` and ``fairclear_seller``,
  which use Town's registry, auth, payments (escrow) and data_facts layers;
- a validator, ``fairclear_procurement``, that checks the run from the
  recorded events only.

Two clearing backends, same roles and same checks:

- ``reference`` (default): the public textbook rule implemented below,
  so anyone can reproduce the run with no FairClear code.
- ``external``: the clearing decision is delegated to a separately hosted
  FairClear deployment through a small JSON contract on stdin/stdout.
  Set ``FAIRCLEAR_CLEARING_CMD`` to the command that runs your bridge.
  Contract: input ``{"rfq": {...}, "bids": [{"bidder", "cents"}]}``;
  output ``{"winner", "price_cents", "result_hash", "verified"}``.
  The deployment itself does not live in this repository.

Run:  nandatown run integrations/fairclear/fairclear_procurement.yaml
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from typing import Any

from nandatown.sim.agents import ROLES, SimAgent, role
from nandatown.sim.validators import _check, _missing, validator

CAPABILITY = "clearing.sealed2p"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


# -- clearing rule ------------------------------------------------------


def clear_reference(rfq: dict[str, Any],
                    bids: list[dict[str, Any]]) -> dict[str, Any]:
    """Sealed second-price reverse auction (Vickrey RFQ).

    Lowest eligible bid wins (ties: name order); the winner is paid the
    second-lowest eligible bid, or the reserve if it is the only one.
    Bids above the reserve are ineligible.
    """
    reserve = rfq["reserve_cents"]
    eligible = sorted((b for b in bids if b["cents"] <= reserve),
                      key=lambda b: (b["cents"], b["bidder"]))
    if not eligible:
        return {"winner": None, "price_cents": None}
    price = eligible[1]["cents"] if len(eligible) > 1 else reserve
    return {"winner": eligible[0]["bidder"], "price_cents": price}


def clear_external(rfq: dict[str, Any],
                   bids: list[dict[str, Any]]) -> dict[str, Any]:
    cmd = os.environ.get("FAIRCLEAR_CLEARING_CMD")
    if not cmd:
        raise RuntimeError("backend 'external' needs FAIRCLEAR_CLEARING_CMD")
    out = subprocess.run(shlex.split(cmd), input=canonical(
        {"rfq": rfq, "bids": bids}), capture_output=True, text=True,
        timeout=60, check=True)
    res = json.loads(out.stdout)
    return {"winner": res.get("winner"), "price_cents": res.get("price_cents"),
            "external_result_hash": res.get("result_hash"),
            "external_verified": res.get("verified")}


BACKENDS = {"reference": clear_reference, "external": clear_external}


# -- roles --------------------------------------------------------------


@role("fairclear_clearing")
class Clearing(SimAgent):
    """The clearing house: collects sealed bids, clears, signs the result."""

    def on_start(self):
        self.api.register([CAPABILITY], {"mechanism": "vickrey-rfq-v1"})
        self.rfqs: dict[str, dict[str, Any]] = {}

    def _auth(self):
        return self.api._engine.layers["auth"]

    def handle_rfq_open(self, msg):
        b = msg["body"]
        rfq_id = f"rfq-{b['sku']}-{len(self.rfqs) + 1}"
        rfq = {"rfq_id": rfq_id, "buyer": msg["sender"], "sku": b["sku"],
               "quantity": b["quantity"], "reserve_cents": b["reserve_cents"],
               "mechanism": "vickrey-rfq-v1"}
        self.rfqs[rfq_id] = {"rfq": rfq, "bids": {}, "open": True}
        # The reserve stays private: sellers see the RFQ without it.
        public = {k: v for k, v in rfq.items() if k != "reserve_cents"}
        self.api.observe("rfq_opened", rfq_id, public)
        sellers = [s["name"] for s in self.api.lookup(f"sell.{b['sku']}")]
        for s in sellers:
            self.api.send(s, "rfq_invite", public)
        self.api.reply(msg, "rfq_ack", {"rfq_id": rfq_id})
        self.api.later(self.config.get("close_after", 3.0),
                       lambda: self.close(rfq_id))

    def handle_sealed_bid(self, msg):
        b = msg["body"]
        rfq_id, bidder = b["rfq_id"], b["bidder"]
        state = self.rfqs.get(rfq_id)
        payload = {"rfq_id": rfq_id, "bidder": bidder,
                   "commitment": b["commitment"]}
        reason = None
        if state is None:
            reason = "unknown rfq"
        elif not state["open"]:
            reason = "rfq closed"
        elif bidder != msg["sender"] or not self._auth().verify(
                bidder, payload, b["signature"], subject=rfq_id):
            reason = "bad signature"
        elif bidder in state["bids"]:
            reason = "duplicate bid"
        elif digest({"cents": b["cents"], "salt": b["salt"]}) != b["commitment"]:
            reason = "commitment mismatch"
        if reason:
            self.api.observe("bid_rejected", rfq_id,
                             {"sender": msg["sender"], "claimed": bidder,
                              "reason": reason})
            return
        state["bids"][bidder] = b
        # Only the commitment is recorded while the RFQ is open.
        self.api.observe("bid_sealed", rfq_id,
                         {"bidder": bidder, "commitment": b["commitment"]})

    def close(self, rfq_id):
        state = self.rfqs[rfq_id]
        state["open"] = False
        bids = [{"bidder": n, "cents": b["cents"]}
                for n, b in sorted(state["bids"].items())]
        backend = os.environ.get("FAIRCLEAR_BACKEND",
                                 self.config.get("backend", "reference"))
        res = BACKENDS[backend](state["rfq"], bids)
        result = {"rfq": state["rfq"], "backend": backend,
                  "opened_bids": bids,
                  "commitments": {n: b["commitment"]
                                  for n, b in sorted(state["bids"].items())},
                  **res}
        signature = self._auth().sign_as(self.name, result)
        self.api.observe("rfq_cleared", rfq_id,
                         {**result, "result_hash": digest(result)})
        receipt = {"result": result, "signature": signature,
                   "issuer": self.name}
        self.api.send(state["rfq"]["buyer"], "rfq_result", receipt)
        for n in state["bids"]:
            self.api.send(n, "rfq_result", receipt)


@role("fairclear_buyer")
class Buyer(SimAgent):
    """Finds FairClear in the Town index, opens an RFQ, pays from escrow."""

    def on_start(self):
        self.api.later(0.5, self.open)

    def open(self):
        c = self.config
        found = self.api.lookup(CAPABILITY)
        if not found:
            self.api.observe("clearing_not_found", CAPABILITY, {})
            return
        self.clearing = found[0]["name"]
        self.api.observe("clearing_discovered", self.clearing,
                         {"capability": CAPABILITY})
        self.ref = f"escrow-{self.name}-{c['sku']}"
        self.api.escrow_hold(c["reserve_cents"] * c["quantity"], self.ref)
        self.api.send(self.clearing, "rfq_open",
                      {"sku": c["sku"], "quantity": c["quantity"],
                       "reserve_cents": c["reserve_cents"]})

    def handle_rfq_ack(self, msg):
        self.api.remember("rfq_id", msg["body"]["rfq_id"])

    def handle_rfq_result(self, msg):
        r = msg["body"]
        auth = self.api._engine.layers["auth"]
        ok = (r["issuer"] == self.clearing
              and auth.verify(r["issuer"], r["result"], r["signature"],
                              subject=r["result"]["rfq"]["rfq_id"])
              and clear_reference(r["result"]["rfq"], r["result"]["opened_bids"])
              == {"winner": r["result"]["winner"],
                  "price_cents": r["result"]["price_cents"]})
        self.api.observe("receipt_checked", r["result"]["rfq"]["rfq_id"],
                         {"valid": bool(ok)})
        # A result that does not verify is not paid.
        if not ok or r["result"]["winner"] is None:
            self.api.escrow_refund(self.ref)
            return
        q = self.config["quantity"]
        total = r["result"]["price_cents"] * q
        held = self.config["reserve_cents"] * q
        self.api.escrow_refund(self.ref)
        self.api.escrow_hold(total, self.ref + "-final")
        self.api.escrow_release(self.ref + "-final", r["result"]["winner"])
        self.api.observe("settled", r["result"]["rfq"]["rfq_id"],
                         {"winner": r["result"]["winner"], "total_cents": total,
                          "refunded_cents": held - total})


@role("fairclear_seller")
class Seller(SimAgent):
    """Bids its cost once (truthful bidding is optimal under second price).

    Optional ``misbehave``: ``late`` bids after close; ``duplicate`` bids
    twice; ``forge`` bids pretending to be ``forge_as``.
    """

    def on_start(self):
        self.api.register([f"sell.{self.config['sku']}"])

    def _bid(self, rfq_id, cents, salt, claimed=None):
        bidder = claimed or self.name
        commitment = digest({"cents": cents, "salt": salt})
        payload = {"rfq_id": rfq_id, "bidder": bidder,
                   "commitment": commitment}
        sig = self.api._engine.layers["auth"].sign_as(self.name, payload)
        self.api.send(self.clearing, "sealed_bid",
                      {**payload, "signature": sig, "cents": cents,
                       "salt": salt})

    def handle_rfq_invite(self, msg):
        c = self.config
        self.clearing = msg["sender"]
        rfq_id = msg["body"]["rfq_id"]
        salt = f"{self.name}-{rfq_id}"
        mode = c.get("misbehave")
        delay = c.get("bid_delay", 1.0)
        if mode == "forge":
            self.api.later(delay, lambda: self._bid(
                rfq_id, c["cost_cents"], salt, claimed=c["forge_as"]))
            return
        self.api.later(delay, lambda: self._bid(rfq_id, c["cost_cents"], salt))
        if mode == "duplicate":
            self.api.later(delay + 0.2, lambda: self._bid(
                rfq_id, c["cost_cents"] - 1, salt + "-2"))

    def handle_rfq_result(self, msg):
        self.api.remember("rfq_result", msg["body"]["result"])


# -- validator ------------------------------------------------------------


@validator("fairclear_procurement")
def fairclear_procurement(spec, trace) -> list:
    stages = []
    sellers = {a.name: a.config for a in spec.agents
               if a.role == "fairclear_seller"}
    honest = {n: c for n, c in sellers.items() if not c.get("misbehave")}
    buyer = next((a for a in spec.agents if a.role == "fairclear_buyer"), None)
    if buyer is None or not honest:
        return [_missing("setup", "needs one fairclear_buyer and sellers")]

    disc = trace.find("clearing_discovered")
    stages.append(_check("discovery", bool(disc), [e.event_id for e in disc],
                         "buyer did not find FairClear in the Town index"))

    sealed = trace.find("bid_sealed")
    leaked = [e for e in sealed if "cents" in e.detail]
    stages.append(_check(
        "sealed_bids",
        {e.detail["bidder"] for e in sealed} >= set(honest) and not leaked,
        [e.event_id for e in sealed],
        "every honest seller needs one sealed bid, and no price may be"
        " recorded before close"))

    rejected = trace.find("bid_rejected")
    expect = {c["misbehave"] for c in sellers.values() if c.get("misbehave")}
    reasons = {"late": "rfq closed", "duplicate": "duplicate bid",
               "forge": "bad signature"}
    got = {e.detail["reason"] for e in rejected}
    stages.append(_check(
        "rejections", all(reasons[m] in got for m in expect),
        [e.event_id for e in rejected],
        f"expected rejections for {sorted(expect)}, saw {sorted(got)}"))

    cleared = trace.find("rfq_cleared")
    res = cleared[0].detail if cleared else {}
    reserve = buyer.config["reserve_cents"]
    costs = sorted((c["cost_cents"], n) for n, c in honest.items()
                   if c["cost_cents"] <= reserve)
    stages.append(_check(
        "efficient_award", bool(res) and res.get("winner") == costs[0][1],
        [e.event_id for e in cleared],
        "the lowest-cost seller must win"))
    want = costs[1][0] if len(costs) > 1 else reserve
    stages.append(_check(
        "second_price", bool(res) and res.get("price_cents") == want,
        [e.event_id for e in cleared],
        f"winner must be paid the second-lowest bid ({want})"))

    checked = trace.find("receipt_checked")
    stages.append(_check(
        "receipt_verified", bool(checked) and all(e.detail["valid"]
                                                  for e in checked),
        [e.event_id for e in checked],
        "the buyer could not verify the signed result"))

    settled = trace.find("settled")
    ok = (len(settled) == 1 and bool(res)
          and settled[0].detail["winner"] == res.get("winner")
          and settled[0].detail["total_cents"]
          == res.get("price_cents", 0) * buyer.config["quantity"])
    stages.append(_check(
        "settlement", ok, [e.event_id for e in settled],
        "exactly one escrow release to the winner at price x quantity"))
    return stages


__all__ = ["ROLES", "clear_reference", "fairclear_procurement"]
