"""Pearl Street's flexibility exchange as a Town Lab participant.

Loaded by pearl_street_peak.yaml through plugin_files. It registers four
roles. The market itself is not reimplemented here: the `pearl_exchange` role
hosts the real `exchange.core.Exchange` from Pearl Street (installed from the
pinned public snapshot, see requirements.txt) and passes every Town message
through `Exchange.handle()`.

Bridge rules:
- A Town message's kind is the Pearl Street msg_type and its body is the
  payload. The bridge builds the Pearl Street envelope itself: `sender` is the
  Town envelope sender that Town's auth layer already verified, and `t_utc` is
  the arrival time (Town logical minutes after t0_utc, truncated to the
  minute). Neither can be chosen by the sending agent.
- Everything the exchange emits goes back out through the Town API, one Town
  message per recipient, with `pearl_msg_id` and `pearl_to` added.
- The bridge records each inbound message's outcome and each need, award and
  settlement as attributed Town events, which the validator reads.
- Clock ticks come from the bridge itself every tick_every_min minutes.

Shared Town layers used: registry (the exchange and the meter publish cards,
and everyone else finds them by capability), auth (sender binding), data_facts
(asset limits and every meter reading are attested), payments (credits and
shortfall charges move as ledger transfers in cents) and trust (the grid
operator rates each provider from its delivery record).

Physics is deliberately simple and lives in the meter: an asset follows its
award up to its attested power, so meter_kw = min(accredited_kw, power_kw).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nandatown.layers.payments import PaymentError
from nandatown.sim.agents import SimAgent, role

EXCHANGE_CAP = "energy.flex_exchange"
METER_CAP = "energy.metering"
PROVIDER_CAP = "energy.flex_provider"
RUN_ID = "town-lab"          # the exchange's own run id (Pearl Street ids are short)
CLOCK_ID = "clock"           # ticks are the bridge's own, never an agent's


def _utc(t0: datetime, minute: float) -> str:
    return (t0 + timedelta(minutes=int(minute))).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(t: str) -> datetime:
    return datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)


def _cents(usd: float) -> int:
    return int(round(usd * 100))


def _find(api, capability: str) -> str | None:
    """The first verified card offering capability, or None (recorded as
    discovery_failed, so the agent stops instead of crashing the run)."""
    hits = api.lookup(capability)
    if not hits:
        api.observe("discovery_failed", capability, {"reason": "no verified card"})
        return None
    return hits[0]["name"]


@role("pearl_exchange")
class PearlStreetExchange(SimAgent):
    """Hosts Pearl Street's Exchange and bridges it to Town messaging."""

    def on_start(self):
        from exchange import messages as M
        from exchange.core import Exchange

        c = self.config
        self.M = M
        self.t0 = _parse(c["t0_utc"])
        self.ex = Exchange(RUN_ID, c["operator"], c["metering"], CLOCK_ID,
                           [c["zone"]], step_minutes=c.get("step_minutes", 15),
                           **c.get("exchange_options", {}))
        self.n_ticks = 0
        self.excluded: set[str] = set()   # the operator's Town-side exclusion list
        self.api.register([EXCHANGE_CAP], {
            "zone": c["zone"], "operator": c["operator"],
            "metering": c["metering"], "implementation": c["implementation"]})
        every = c["tick_every_min"]
        for minute in range(every, c["last_tick_min"] + 1, every):
            self.api.later(minute, self._tick)

    def on_message(self, msg):
        sender, kind = msg["sender"], msg["kind"]
        msg_id = f"{sender}:{msg['message_id']}"
        t_utc = _utc(self.t0, self.api.now)
        body = msg["body"] if isinstance(msg["body"], dict) else {}
        if kind == "operator_policy":
            # A Town-side gate, not a Pearl Street rule: only the operator may set it.
            if sender == self.config["operator"]:
                self.excluded = set(body.get("exclude", []))
                self.api.observe("town_policy", sender, {"exclude": sorted(self.excluded)})
            return
        if kind == "offer" and sender in self.excluded:
            reason = "the operator excludes this provider (Town reputation below zero)"
            self.api.observe("pearl_message", msg_id, {
                "msg_type": kind, "from": sender, "t_utc": t_utc, "payload": body,
                "outcome": "rejected", "code": "town_policy_excluded", "reason": reason,
                "source": "town"})
            self.api.send(sender, "reject", {"ref_msg_id": msg_id, "code": "town_policy_excluded",
                                             "detail": reason, "source": "town"})
            return
        outs = self._handle(self.M.make(kind, sender, "exchange", t_utc, body,
                                        msg_id, RUN_ID))
        rejects = [o for o in outs if o["msg_type"] == "reject"
                   and o["payload"].get("ref_msg_id") == msg_id]
        detail = {"msg_type": kind, "from": sender, "t_utc": t_utc,
                  "payload": body,
                  "outcome": "rejected" if rejects else "accepted"}
        if rejects:
            detail["code"] = rejects[0]["payload"]["code"]
            detail["reason"] = rejects[0]["payload"]["detail"]
        self.api.observe("pearl_message", msg_id, detail)
        self._forward(outs)

    def _tick(self):
        self.n_ticks += 1
        t_utc = _utc(self.t0, self.api.now)
        self._forward(self._handle(self.M.make(
            "tick", CLOCK_ID, "exchange", t_utc, {},
            f"{CLOCK_ID}:{self.n_ticks}", RUN_ID)))

    def _handle(self, envelope: dict) -> list[dict]:
        raw = self.M.encode(envelope)
        return [self.M.decode(r) for r in self.ex.handle(raw)]

    def _forward(self, outs: list[dict]):
        for o in outs:
            to = o["to"] if isinstance(o["to"], list) else [o["to"]]
            p = o["payload"]
            if o["msg_type"] == "post_need":
                self.api.observe("pearl_need", p["need_id"], {
                    "to": to, "kw_needed": p["kw_needed"],
                    "price_cap": p["price_cap"], "window": p["window"],
                    "gate_utc": p["gate_utc"]})
            elif o["msg_type"] == "award":
                self.api.observe("pearl_award", to[0], {
                    "award_id": o["msg_id"], "offer_id": p["offer_id"],
                    "need_id": p["need_id"], "accredited_kw": p["accredited_kw"],
                    "price_usd_mwh": p["price_usd_mwh"], "window": p["window"]})
            elif o["msg_type"] == "settle":
                self.api.observe("pearl_settle", to[0], {
                    "award_id": p["award_id"], "need_id": p["need_id"],
                    "delivered_kw": p["delivered_kw"],
                    "credit_usd": p["credit_usd"],
                    "charge_usd": p["charge_usd"],
                    "new_delivery_record": p["new_delivery_record"]})
            for recipient in to:
                self.api.send(recipient, o["msg_type"],
                              dict(p, pearl_msg_id=o["msg_id"], pearl_to=to))


@role("grid_operator")
class GridOperator(SimAgent):
    """The utility: posts each peak need, pays credits, rates providers.

    Config holds one need (post_at_min, window, kw_needed, price_cap,
    gate_utc) or a list under `needs`. With skip_negative_reputation, before
    each need it asks the exchange's Town gateway to refuse offers from
    providers whose Town reputation is below zero."""

    def on_start(self):
        for need in self.config.get("needs") or [self.config]:
            self.api.later(need["post_at_min"], lambda n=need: self.post_need(n))

    def post_need(self, n):
        c = self.config
        self.exchange = _find(self.api, EXCHANGE_CAP)
        self.meter = _find(self.api, METER_CAP)
        if self.exchange is None or self.meter is None:
            return
        if c.get("skip_negative_reputation"):
            providers = [card["name"] for card in self.api.lookup(PROVIDER_CAP)]
            self.api.send(self.exchange, "operator_policy", {
                "exclude": sorted(p for p in providers if self.api.reputation(p) < 0)})
        self.api.send(self.exchange, "post_need", {
            "zone": c["zone"], "window": n["window"], "kw_needed": n["kw_needed"],
            "mode": "scheduled", "price_cap": n["price_cap"],
            "gate_utc": n["gate_utc"]})

    def handle_award(self, msg):
        b = msg["body"]
        self.api.send(self.meter, "meter_request", {
            "agent": b["pearl_to"][0], "window": b["window"],
            "accredited_kw": b["accredited_kw"]})

    def handle_settle(self, msg):
        b = msg["body"]
        agent = b["pearl_to"][0]
        credit = _cents(b["credit_usd"])
        if credit > 0:
            try:
                self.api.pay(agent, credit, f"pearl-credit:{b['award_id']}")
            except PaymentError:
                pass             # recorded by the ledger as payment_rejected
        good = b["new_delivery_record"] >= self.config["good_record"]
        self.api.rate(agent, "good" if good else "bad")

    def handle_reject(self, msg):
        pass                     # recorded by the exchange as pearl_message


@role("meter")
class MeteringService(SimAgent):
    """Attests asset limits and reads delivery. The only source of meter_kw."""

    def on_start(self):
        self.t0 = _parse(self.config["t0_utc"])
        self.assets = {a["agent"]: a for a in self.config["assets"]}
        self.api.later(self.config.get("start_at_min", 1), self.attest_limits)

    def attest_limits(self):
        self.api.register([METER_CAP], {"zone": self.config["zone"]})
        self.exchange = _find(self.api, EXCHANGE_CAP)
        if self.exchange is None:
            return
        for agent, a in self.assets.items():
            limits = {"asset_id": a["asset_id"], "usable_kwh": a["usable_kwh"],
                      "power_kw": a["power_kw"], "pv_kwh_per_day": 0.0,
                      "owner": a["owner"], "agent_id": agent}
            self.api.send(self.exchange, "asset_limits", limits)
            self.api.attest(agent, "asset_limits", limits)

    def handle_meter_request(self, msg):
        b = msg["body"]
        a = self.assets.get(b["agent"])
        if a is None:
            self.api.observe("meter_request_refused", b["agent"],
                             {"reason": "no metered asset"})
            return
        kw = float(min(b["accredited_kw"], a["power_kw"]))
        start, end = _parse(b["window"]["start_utc"]), _parse(b["window"]["end_utc"])
        step = timedelta(minutes=self.config.get("step_minutes", 15))
        t = start
        while t < end:
            minute = (t - self.t0).total_seconds() / 60
            self.api.later(minute - self.api.now,
                           lambda agent=b["agent"], t=t, kw=kw: self.read(agent, t, kw))
            t += step

    def read(self, agent: str, t: datetime, kw: float):
        a = self.assets[agent]
        t_utc = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.api.send(self.exchange, "telemetry",
                      {"asset_id": a["asset_id"], "soc_kwh": 0.0, "meter_kw": kw})
        self.api.attest(agent, "meter_kw",
                        {"asset_id": a["asset_id"], "t_utc": t_utc, "kw": kw})


@role("flex_provider")
class FlexProvider(SimAgent):
    """A data center or PV + battery site that sells peak reduction.

    Config chooses its honesty: declared_max_kw may exceed its attested power
    (an overpromiser), offer.kw may exceed its own declared limit, offer.at_min
    may fall after the gate, and self_report may claim delivery it never made.
    """

    def on_start(self):
        self.needs_seen = 0
        self.api.later(self.config.get("join_at_min", 2), self.join)

    def offer_for(self, i: int):
        """The configured offer for the i-th need: `offers` is one per need;
        a single `offer` applies to the first need only."""
        c = self.config
        if "offers" in c:
            return c["offers"][i] if i < len(c["offers"]) else None
        return c.get("offer") if i == 0 else None

    def join(self):
        c = self.config
        self.api.register([PROVIDER_CAP], {
            "engie_entity": c["engie_entity"], "asset_id": c["asset_id"],
            "declared_max_kw": c["declared_max_kw"]})
        self.exchange = _find(self.api, EXCHANGE_CAP)
        if self.exchange is None:
            return
        self.api.send(self.exchange, "register", {
            "agent_id": self.name, "legal_entity": c["legal_entity"],
            "assets": [{"asset_id": c["asset_id"], "zone": c["zone"],
                        "type": c["asset_type"]}],
            "mandate": {"max_kw": c["declared_max_kw"],
                        "price_floor": c.get("price_floor", 0.0),
                        "workload_limits": None}})
        self.api.send(self.exchange, "subscribe",
                      {"signals": ["post_need"], "zones": [c["zone"]]})

    def handle_post_need(self, msg):
        offer = self.offer_for(self.needs_seen)
        self.needs_seen += 1
        if offer:
            self.api.later(max(0.0, offer["at_min"] - self.api.now),
                           lambda n=msg["body"], o=offer: self.make_offer(n, o))

    def make_offer(self, n, o):
        self.api.send(self.exchange, "offer", {
            "need_id": n["need_id"], "mode": n["mode"], "zone": n["zone"],
            "window": n["window"], "kw": o["kw"],
            "floor_usd_mwh": o["floor_usd_mwh"], "standing": False})

    def handle_award(self, msg):
        lie = self.config.get("self_report")
        if lie:
            self.api.later(max(0.0, lie["at_min"] - self.api.now), lambda: self.api.send(
                self.exchange, "telemetry", {"asset_id": self.config["asset_id"],
                                             "soc_kwh": 0.0, "meter_kw": lie["kw"]}))

    def handle_settle(self, msg):
        b = msg["body"]
        charge = _cents(b["charge_usd"])
        operator = [r for r in b["pearl_to"] if r != self.name][0]
        if charge > 0:
            try:
                self.api.pay(operator, charge, f"pearl-charge:{b['award_id']}")
            except PaymentError:
                pass             # recorded by the ledger as payment_rejected

    def handle_reject(self, msg):
        pass                     # recorded by the exchange as pearl_message


@role("listing_forger")
class ListingForger(SimAgent):
    """A rival that republishes the exchange's Town card under the exchange's
    name, signed with its own key, to take the market out of discovery.

    Like Town's own rival_forger role, it reaches the registry directly: the
    Town API has no call for publishing someone else's card. It sends no
    messages and takes no part in the market."""

    def on_start(self):
        self.api.later(self.config.get("forge_at_min", 0.5), self.forge)

    def forge(self):
        victim = self.config["victim"]
        engine = self.api._engine
        registry = engine.layers["registry"]
        entry = registry.cards.get(victim)
        if entry is None:
            self.api.observe("forgery_skipped", victim, {"reason": "victim has no listing"})
            return
        engine.layers["identity"].create(self.name)
        forged = dict(entry["card"], facts={"note": "exchange closed"})
        signature = engine.layers["auth"].sign_as(self.name, forged)
        engine.record_intent(self.name, "register",
                             {"forged_name": victim, "capabilities": forged["capabilities"]})
        registry.publish(self.name, forged, signature)
