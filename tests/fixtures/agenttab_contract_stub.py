"""A stand-in for the AgentTab service, for offline tests only.

This is a Town test fixture, not AgentTab. It implements the handful of
REST calls examples/agenttab_seller.py makes, with AgentTab's request and
reply shapes, so the adapter and the check script can be exercised in CI
without the network. It verifies both IOU signatures, as AgentTab does;
its credit decision is a fixed limit and one owner-policy cap, nothing
like AgentTab's engine. A pass against this stub says the adapter follows
the contract. It says nothing about AgentTab itself; the live test in
tests/test_agenttab_integration.py does that.

retry_rule selects how a repeated IOU nonce is answered:
    "stored_reply"  AgentTab from commit 14d5403 on: the same nonce and
                    body returns the first reply; a different body gets
                    409 nonce_reused; lookup_charge finds it by nonce
    "iou_replay"    AgentTab before that: every repeat is refused as
                    iou_replay and lookup_charge does not exist
"""

import base64
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

LIMIT_CENTS = 2500


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _verified(public_key, raw, signature):
    try:
        Ed25519PublicKey.from_public_bytes(_b64d(public_key)).verify(
            _b64d(signature), raw)
        return True
    except (InvalidSignature, ValueError):
        return False


class Stub:
    def __init__(self, retry_rule="stored_reply"):
        self.retry_rule = retry_rule
        self.lock = threading.Lock()
        self.keys = {}        # api key -> (kind, id)
        self.agents = {}      # agent id -> {public_key, policy_cap, balance, holds}
        self.sellers = {}     # seller id -> {public_key, prices}
        self.replies = {}     # (agent id, nonce) -> (IOU text, reply)
        self.auths = {}       # authorization id -> {agent, seller, cents, charge_id}
        self.calls = []       # every tool name called, in order

    def _id(self, prefix):
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def handle(self, tool, args, key):
        """Returns (HTTP status, body) for one POST /v1/<tool>."""
        self.calls.append(tool)
        who = self.keys.get(key, ("anon", None))
        if tool == "register_owner":
            owner, api = self._id("own"), "atk_own_" + uuid.uuid4().hex
            self.keys[api] = ("owner", owner)
            return 200, {"owner_id": owner, "api_key": api,
                         "verification_link": "/v1/verify_email?token=t"}
        if tool == "register_agent" and who[0] == "owner":
            agent, api = self._id("agt"), "atk_agt_" + uuid.uuid4().hex
            self.keys[api] = ("agent", agent)
            self.agents[agent] = {"public_key": args["public_key"],
                                  "policy_cap": None, "balance": 0, "holds": 0}
            return 200, {"agent_id": agent, "api_key": api,
                         "limit_cents": LIMIT_CENTS}
        if tool == "set_policy" and who[0] == "owner":
            self.agents[args["agent_id"]]["policy_cap"] = args.get(
                "max_per_purchase_cents")
            return 200, {"agent_id": args["agent_id"]}
        if tool == "register_seller" and who[0] == "owner":
            seller, api = self._id("sel"), "atk_sel_" + uuid.uuid4().hex
            self.keys[api] = ("seller", seller)
            self.sellers[seller] = {"public_key": args["public_key"],
                                    "prices": args["price_list"]}
            return 200, {"seller_id": seller, "api_key": api}
        if tool == "check_credit":
            a = self.agents[args["agent_id"]]
            ok, _ = self._decide(a, args["amount_cents"])
            return 200, {"decision": "accept" if ok else "decline",
                         "credit_available_cents":
                             LIMIT_CENTS - a["balance"] - a["holds"]}
        if tool == "charge":
            return self._charge(args)
        if tool == "lookup_charge" and self.retry_rule == "stored_reply":
            stored = self.replies.get((args["agent_id"], args["nonce"]))
            if not stored:
                return 200, {"found": False}
            return 200, {"found": True, "reply": stored[1]}
        if tool == "capture" and who[0] == "seller":
            au = self.auths.get(args["authorization_id"])
            if not au or au["seller"] != who[1]:
                return 404, {"error": "unknown_authorization"}
            if au["charge_id"] is None:      # a second capture changes nothing
                au["charge_id"] = self._id("chg")
                a = self.agents[au["agent"]]
                a["holds"] -= au["cents"]
                a["balance"] += au["cents"]
            return 200, {"charge_id": au["charge_id"], "status": "captured"}
        if tool == "get_statement" and who == ("agent", args.get("agent_id")):
            return 200, {"agent_id": who[1],
                         "balance_cents": self.agents[who[1]]["balance"]}
        return 404, {"error": "unknown_tool", "message": f"no tool {tool}"}

    def _decide(self, agent, cents):
        if agent["policy_cap"] is not None and cents > agent["policy_cap"]:
            return False, ("owner_policy", ["owner_policy:max_per_purchase"])
        if agent["balance"] + agent["holds"] + cents > LIMIT_CENTS:
            return False, ("limit", ["limit"])
        return True, (None, [])

    def _charge(self, args):
        iou = args["iou"]
        raw = _b64d(iou.split(".")[0])
        body = json.loads(raw)
        agent = self.agents[body["agent_id"]]
        seller = self.sellers[body["seller_id"]]
        key = (body["agent_id"], body["nonce"])
        stored = self.replies.get(key)
        if stored and self.retry_rule == "stored_reply":
            if stored[0] != iou:
                return 409, {"error": "nonce_reused"}
            return 200, stored[1]

        def refused(rule, refusals):
            return {"decision": "refused", "approved": False,
                    "binding_rule": rule, "refusals": refusals,
                    "reasons": refusals, "decision_id": self._id("dec"),
                    "authorization_id": None, "hold_cents": 0}

        if not _verified(agent["public_key"], raw, iou.split(".")[1]):
            reply = refused("iou", ["iou_bad_signature"])
        elif not _verified(seller["public_key"], raw, args["seller_signature"]):
            reply = refused("iou", ["seller_bad_signature"])
        elif stored:
            return 200, refused("iou", ["iou_replay"])
        elif body["amount_cents"] not in seller["prices"].values():
            reply = refused("iou", ["iou_amount_mismatch"])
        else:
            ok, (rule, refusals) = self._decide(agent, body["amount_cents"])
            if ok:
                auth = self._id("auth")
                agent["holds"] += body["amount_cents"]
                self.auths[auth] = {"agent": body["agent_id"],
                                    "seller": body["seller_id"],
                                    "cents": body["amount_cents"],
                                    "charge_id": None}
                reply = {"decision": "approved", "approved": True,
                         "binding_rule": None, "refusals": [], "reasons": [],
                         "decision_id": self._id("dec"),
                         "authorization_id": auth,
                         "hold_cents": body["amount_cents"],
                         "engine_version": "stub"}
            else:
                reply = refused(rule, refusals)
        self.replies[key] = (iou, reply)
        return 200, reply


def serve(retry_rule="stored_reply"):
    """Start the stub on a free loopback port. Returns (base URL, the Stub,
    a function that stops it)."""
    stub = Stub(retry_rule)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/v1/clock":
                self._send(200, {"ok": True, "epoch": int(time.time())})
            elif self.path == "/health":
                self._send(200, {"ok": True})
            elif self.path == "/readyz" and stub.retry_rule == "stored_reply":
                self._send(200, {"ok": True})
            elif self.path.startswith("/v1/verify_email"):
                self._send(200, {"ok": True, "verified": True})
            else:
                self._send(404, {"detail": "Not Found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(length) or b"{}")
            auth = self.headers.get("Authorization", "")
            key = auth[7:] if auth.startswith("Bearer ") else None
            with stub.lock:
                status, body = stub.handle(self.path.removeprefix("/v1/"),
                                           args, key)
            self._send(status, {"ok": status == 200 and "error" not in body,
                                **body, "test_money": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", stub, server.shutdown
