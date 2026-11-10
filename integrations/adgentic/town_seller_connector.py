"""Adgentic plugin: a Town mailbox seller becomes a seller on the Adgentic exchange.

Loaded by the Adgentic exchange through ADGENTIC_PLUGINS (see README.md). The
exchange process joins the Town run as the buyer participant, discovers the
seller through the Town participant index (capability ``quote.read``), and
registers it as external seller ``town-seller``. When a buyer agent posts an
Adgentic intent for widgets, the exchange sends a Town ``quote_request`` over the
mailbox, waits up to ADGENTIC_TOWN_TIMEOUT seconds for the ``quote_response``,
and turns it into a signed Adgentic offer that goes through Offer Review like
every other offer.

Standard library plus PyNaCl (already an Adgentic dependency); no nandatown import.

Environment (Town sets the first five for a cmd: participant):
  TOWN_URL, RUN_ID, NAME, TOKEN, STATE_DIR
  ADGENTIC_TOWN_TIMEOUT        seconds to wait for the Town seller's quote (default 10)
  ADGENTIC_TOWN_LIST_PRICE     listed unit price in cents sent with each request (default 1995)
  ADGENTIC_TOWN_DELIVERY_DAYS  delivery term registered for the Town seller (default 3)
  ADGENTIC_TOWN_RETURN_DAYS    return term registered for the Town seller (default 0)
  ADGENTIC_TOWN_MARKET         "solo": only the Town seller carries widgets (default);
                               "mixed": Adgentic's own five sellers list widgets too

The Town quote contract carries only a total. Delivery and return terms come from
the registration above (operator-declared), not from the Town seller. The offer
signature is the connector's key: Town authenticates the seller by its run session,
not by an Ed25519 offer signature, so the signature proves the exchange-side
bridge committed to these bytes, not that the Town seller signed them.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from nacl.encoding import HexEncoder
from nacl.signing import SigningKey

SELLER_ID = "town-seller"
CATEGORY = "widget"
SIZE = "ONE SIZE"
OFFER_TTL = timedelta(minutes=15)


class TownMailbox:
    """Minimal Town participant client (same HTTP contract as examples/byoa_seller.py)."""

    def __init__(self, url: str, run_id: str):
        self.base = f"{url}/runs/{run_id}"
        self.session: str | None = None

    def call(self, method: str, path: str, body: dict | None = None, retry: bool = True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.session:
            req.add_header("X-Town-Session", self.session)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 204:
                    return None
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 503 and retry:
                time.sleep(0.2)
                return self.call(method, path, body, retry=False)
            raise


class Connector:
    def __init__(self, exchange, env):
        self.ex = exchange
        self.state_dir = env["STATE_DIR"]
        self.timeout = float(env.get("ADGENTIC_TOWN_TIMEOUT", "10"))
        self.list_price_cents = int(env.get("ADGENTIC_TOWN_LIST_PRICE", "1995"))
        self.delivery_days = int(env.get("ADGENTIC_TOWN_DELIVERY_DAYS", "3"))
        self.return_days = int(env.get("ADGENTIC_TOWN_RETURN_DAYS", "0"))
        self.market = env.get("ADGENTIC_TOWN_MARKET", "solo")
        self.key = SigningKey.generate()
        self.town = TownMailbox(env["TOWN_URL"], env["RUN_ID"])
        joined = self.town.call("POST", "/join", {"name": env["NAME"], "token": env["TOKEN"]})
        self.town.session = joined["session"]
        self.run_context = joined.get("run", {})
        self.lock = threading.Condition()
        self.requests: dict[str, dict] = {}   # Town request message id -> request record
        self.seen_messages: set[str] = set()  # Town message ids already handled (journal)
        self.acks: list[dict] = []
        self.seller_name = self._discover()
        self._write_status()
        threading.Thread(target=self._inbox_loop, daemon=True).start()

    # -- discovery and registration --------------------------------------------
    def _discover(self, wait_s: float = 15.0) -> str:
        deadline = time.time() + wait_s
        while time.time() < deadline:
            for p in self.town.call("GET", "/participants") or []:
                if "quote.read" in p.get("capabilities", []) and p.get("name") != self.town_name():
                    self.ex.log("town_seller_discovered", seller=p["name"],
                                capabilities=p.get("capabilities"))
                    return p["name"]
            time.sleep(0.3)
        raise RuntimeError("no Town participant with capability quote.read")

    def town_name(self) -> str:
        return os.environ.get("NAME", "buyer")

    def register(self) -> None:
        self.ex.register_category(
            CATEGORY, sizes=[SIZE], model="Widget", aliases=("widgets",),
            base_price=self.list_price_cents / 100 if self.market == "mixed" else None)
        self.ex.register_external_seller(
            SELLER_ID, name=f"Town:{self.seller_name}",
            public_key_hex=self.key.verify_key.encode(HexEncoder).decode(),
            registered_return_days=self.return_days, delivery_days=self.delivery_days,
            sponsored=False,
            catalog=[{"sku": "town-widget", "category": CATEGORY,
                      "name": f"Widget (Town:{self.seller_name})",
                      "price": self.list_price_cents / 100, "shipping": 0.0,
                      "sizes_in_stock": [SIZE]}],
            make_offer=self.make_offer)

    # -- request path (called by the exchange inside post_intent) --------------
    def make_offer(self, intent: dict) -> dict | None:
        if intent["category"] != CATEGORY:
            return None
        qty = int(intent.get("quantity", 1))
        if qty * self.list_price_cents > round(intent["max_total_price"] * 100):
            return None  # same rule as Adgentic's own sellers: no offer over budget
        mid = f"q-{intent['intent_id']}"
        body = {"sku": CATEGORY, "quantity": qty, "unit_price_cents": self.list_price_cents}
        with self.lock:
            self.requests[mid] = {"intent_id": intent["intent_id"], "body": body,
                                  "sent_at": time.time(), "responses": [], "timed_out": False}
        self.town.call("POST", "/messages",
                       {"message_id": mid, "to": self.seller_name, "kind": "quote_request",
                        "body": body})
        self.ex.log("town_quote_requested", intent_id=intent["intent_id"],
                    town_message_id=mid, to=self.seller_name, body=body)
        with self.lock:
            self.lock.wait_for(lambda: self.requests[mid]["responses"], timeout=self.timeout)
            rec = self.requests[mid]
            if not rec["responses"]:
                rec["timed_out"] = True
                self._write_status()
                self.ex.log("town_quote_timeout", intent_id=intent["intent_id"],
                            town_message_id=mid, timeout_s=self.timeout)
                return None
            first = rec["responses"][0]
        total = first["body"].get("total_cents")
        if not isinstance(total, int) or isinstance(total, bool):
            self.ex.log("town_quote_unusable", intent_id=intent["intent_id"],
                        town_message_id=first["message_id"], reason="total_cents not an integer")
            return None
        offer = {
            "offer_id": f"off-{uuid.uuid4().hex[:8]}",
            "intent_id": intent["intent_id"],
            "seller_id": SELLER_ID,
            "product": {"sku": "town-widget", "name": f"Widget (Town:{self.seller_name})",
                        "size": intent["size"], "quantity": qty},
            "all_in_price": round(total / 100, 2),
            "delivery_days": self.delivery_days,
            "return_days_claimed": self.return_days,
            "in_stock": True,
            "expires_at": (datetime.now(timezone.utc) + OFFER_TTL).isoformat(),
            "sponsored": False,
            "pitch_text": (f"Quote from Town participant {self.seller_name} "
                           f"(Town message {first['message_id']})."),
        }
        sig = self.key.sign(self.ex.canonical(offer)).signature.hex()
        offer["signature"] = sig
        self.ex.log("town_quote_converted", intent_id=intent["intent_id"],
                    town_request=mid, town_response=first["message_id"],
                    total_cents=total, offer_id=offer["offer_id"])
        return offer

    # -- Town inbox: claim, judge, acknowledge ---------------------------------
    def _inbox_loop(self) -> None:
        while True:
            try:
                self.town.call("GET", "/inbox/notify?wait=0.4")
                claim = self.town.call("POST", "/inbox/claim")
                if claim is None:
                    continue
                status, note = self._handle(claim)
                try:
                    self.town.call("POST", "/inbox/ack",
                                   {"message_id": claim["message_id"], "fence": claim["fence"],
                                    "status": status, "note": note})
                except urllib.error.HTTPError as exc:
                    if exc.code == 409:  # stale fence: Town will redeliver
                        continue
                    raise
                with self.lock:
                    self.acks.append({"message_id": claim["message_id"], "status": status,
                                      "note": note})
                    self._write_status()
            except Exception as exc:  # keep serving; record and continue
                self.ex.log("town_inbox_error", error=repr(exc))
                time.sleep(0.5)

    def _handle(self, claim: dict) -> tuple[str, dict]:
        mid = claim["message_id"]
        if claim["kind"] != "quote_response":
            return "rejected", {"reason": "unknown kind"}
        with self.lock:
            if mid in self.seen_messages:
                return "processed", {"duplicate": True}
            self.seen_messages.add(mid)
            body = claim.get("body") or {}
            req = self.requests.get(body.get("request_id"))
            if req is None:
                return "rejected", {"reason": "response names no request this buyer sent"}
            expected = req["body"]["quantity"] * req["body"]["unit_price_cents"]
            total = body.get("total_cents")
            correct = isinstance(total, int) and not isinstance(total, bool) and total == expected
            first = not req["responses"]
            late = req["timed_out"]
            req["responses"].append({"message_id": mid, "body": body})
            self.lock.notify_all()
        self.ex.log("town_quote_received", town_message_id=mid, request_id=body.get("request_id"),
                    total_cents=total, correct=correct, first=first, late=late)
        note = {"correct": correct, "total_cents": total, "expected_total_cents": expected,
                "adgentic_used": first and not late}
        if not first:
            note["second_distinct_response"] = True
        if late:
            note["reason"] = "arrived after the Adgentic seller deadline; seller was skipped"
        return "processed", note

    def _write_status(self) -> None:
        responded = sum(1 for r in self.requests.values() if r["responses"])
        status = {"seller": self.seller_name, "requests": len(self.requests),
                  "requests_with_response": responded,
                  "requests_timed_out": sum(1 for r in self.requests.values() if r["timed_out"]),
                  "acks": len(self.acks), "run_context": self.run_context}
        path = os.path.join(self.state_dir, "adgentic_connector.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f, indent=1)
        os.replace(tmp, path)


CONNECTOR: Connector | None = None


def setup(exchange) -> None:
    global CONNECTOR
    needed = ("TOWN_URL", "RUN_ID", "NAME", "TOKEN", "STATE_DIR")
    if not all(k in os.environ for k in needed):
        print("town_seller_connector: Town environment not set; plugin inactive", file=sys.stderr)
        return
    CONNECTOR = Connector(exchange, os.environ)
    CONNECTOR.register()
