#!/usr/bin/env python3
"""A Town seller that buys on credit from AgentTab before it answers.

AgentTab (https://github.com/KevinChunye/AgentTab, a separately hosted
service) is a credit line for AI agents that earn. An agent signs an IOU
for a purchase, the seller countersigns it, and AgentTab's engine
approves or refuses the charge against the agent's limit and its owner's
policy.

This adapter plays Town's seller role as such an agent. To answer a quote
it first buys one price lookup (40 cents) from a supplier on its AgentTab
line, through AgentTab's real HTTP API:

    check_credit -> signed IOU -> charge (hold) -> capture (charge)

and only then sends the quote. Town supplies the buyer, the delivery
faults and the evaluator. What is under test is the pair of boundaries:
Town delivers work at least once, and AgentTab must charge for it once.

    AGENTTAB_URL=http://127.0.0.1:8000 \\
    nandatown test-agent --role seller --profile quote-amnesia-restart \\
        --cmd "python examples/agenttab_seller.py"

Environment, besides what Town hands every participant (TOWN_URL, RUN_ID,
NAME, TOKEN, STATE_DIR, DEADLINE, FAULT):

    AGENTTAB_URL       base URL of a running AgentTab service (required)
    AGENTTAB_BOUNDARY  "policy_cap": the owner's policy allows at most 10
                       cents per purchase, so AgentTab refuses the 40-cent
                       lookup and this seller declines the work
    AGENTTAB_NONCE     "derived" (default): the IOU nonce is a hash of the
                       run and the Town message id, so a redelivery
                       re-presents the same IOU. "fresh": a new nonce per
                       attempt, the negative control that pays twice

The identity (owner, agent and supplier keys) is kept in STATE_DIR and
survives a restart, like a wallet. The journal of applied work is kept
separately and is what the amnesia fault erases.

A purchase is several HTTP calls to another service and can outlast a
short lease (quote-crash-restart leases work for 1.5 s). The stale
acknowledgement is then refused, Town redelivers, and the seller answers
from its journal, reporting the application it already made instead of
buying again.

Town does not tell an outside agent to misbehave, but a --cmd agent does
receive FAULT. This adapter chooses to act on the two crash faults the
way Town's stock seller does, so the crash lands between AgentTab's
charge and Town's acknowledgement:

    crash_after_claim   stall past the lease, try the stale fence, exit 3
    crash_amnesia       buy, answer, erase the journal, exit 3 before the
                        acknowledgement
"""

import base64
import hashlib
import json
import os
import sys
import time
import uuid

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

TOWN = os.environ["TOWN_URL"]
RUN = os.environ["RUN_ID"]
NAME = os.environ["NAME"]
TOKEN = os.environ["TOKEN"]
STATE_DIR = os.environ.get("STATE_DIR", ".")
DEADLINE = time.time() + float(os.environ.get("DEADLINE", "45"))
FAULT = os.environ.get("FAULT", "none")
AGENTTAB = os.environ.get("AGENTTAB_URL", "").rstrip("/")
BOUNDARY = os.environ.get("AGENTTAB_BOUNDARY", "")
NONCE_MODE = os.environ.get("AGENTTAB_NONCE", "derived")

LOOKUP_CENTS = 40                 # what one price lookup costs
CRASH_EXIT_CODE = 3               # Town restarts a seller that exits with 3
IDENTITY_FILE = os.path.join(STATE_DIR, "agenttab_identity.json")
JOURNAL_FILE = os.path.join(STATE_DIR, "agenttab_journal.json")
SUMMARY_FILE = os.path.join(STATE_DIR, "agenttab_summary.json")


class Refused(Exception):
    """AgentTab said no to the purchase. `detail` goes into the ack note."""

    def __init__(self, detail):
        super().__init__(str(detail))
        self.detail = detail


class Unavailable(Exception):
    """AgentTab could not be reached or was busy; the work can be retried."""


# ---------- small helpers ----------

def b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def new_keypair():
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.Raw,
                                serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw,
                                           serialization.PublicFormat.Raw)
    return b64e(private), b64e(public)


def sign(private_key, message):
    key = Ed25519PrivateKey.from_private_bytes(b64d(private_key))
    return b64e(key.sign(message))


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


# ---------- AgentTab over HTTP ----------

class AgentTab:
    """The part of AgentTab's REST API a buying agent and its supplier use."""

    def __init__(self, base_url):
        self.http = httpx.Client(base_url=base_url, timeout=20.0)

    def call(self, tool, api_key=None, **args):
        """POST /v1/<tool>. Returns (HTTP status, JSON body)."""
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            r = self.http.post(f"/v1/{tool}", json=args, headers=headers)
        except httpx.TransportError as exc:
            raise Unavailable(type(exc).__name__) from exc
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 500:
            raise Unavailable(body.get("error") or f"http {r.status_code}")
        return r.status_code, body

    def ok(self, tool, api_key=None, **args):
        status, body = self.call(tool, api_key, **args)
        if not body.get("ok"):
            raise Refused({"tool": tool, "status": status,
                           "error": body.get("error"),
                           "message": body.get("message")})
        return body

    def now(self):
        try:
            return int(self.http.get("/v1/clock").json()["epoch"])
        except (httpx.TransportError, ValueError, KeyError) as exc:
            raise Unavailable(type(exc).__name__) from exc

    def version(self):
        """What the service says about itself, for the evidence note.
        /readyz exists from AgentTab's Stage 0 on; older builds answer 404."""
        try:
            ready = self.http.get("/readyz")
            return {"readyz": ready.status_code,
                    "health": self.http.get("/health").status_code}
        except httpx.TransportError as exc:
            raise Unavailable(type(exc).__name__) from exc


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def make_iou(private_key, agent_id, seller_id, cents, nonce, now):
    """AgentTab's IOU: canonical JSON, base64url, then the buyer's
    Ed25519 signature over those exact bytes."""
    request_hash = hashlib.sha256(
        b"POST" + b"/price_lookup" + canonical({"sku": "widget"})).hexdigest()
    body = {"v": 1, "agent_id": agent_id, "seller_id": seller_id,
            "amount_cents": cents, "request_hash": request_hash,
            "nonce": nonce, "issued_at": iso(now), "expires_at": iso(now + 300)}
    raw = canonical(body)
    return f"{b64e(raw)}.{sign(private_key, raw)}", raw


def enroll(tab):
    """Register this seller with AgentTab once: a verified owner, an agent
    with a key and a deposit, the owner's policy, and a supplier to buy
    from. Kept in STATE_DIR so a restart is the same agent."""
    identity = load(IDENTITY_FILE, None)
    if identity:
        return identity
    tag = uuid.uuid4().hex[:8]
    owner = tab.ok("register_owner", email=f"town-{tag}@example.com",
                   credit_band="good")
    tab.http.get(owner["verification_link"])
    private, public = new_keypair()
    agent = tab.ok("register_agent", owner["api_key"], name="town-quote-desk",
                   public_key=public, deposit_cents=500)
    for _ in range(100):            # a deposit can settle a moment later
        if agent.get("status") != "pending":
            break
        time.sleep(0.2)
        intent = tab.ok("get_intent", owner["api_key"],
                        intent_id=agent["intent_id"])
        if intent["state"] in ("succeeded", "failed", "needs_review"):
            agent = dict(agent, status=intent["state"])
    policy = {"agent_id": agent["agent_id"], "autopay": True}
    if BOUNDARY == "policy_cap":
        policy["max_per_purchase_cents"] = 10
    tab.ok("set_policy", owner["api_key"], **policy)
    supplier_owner = tab.ok("register_owner",
                            email=f"feed-{tag}@example.com",
                            credit_band="good")
    s_private, s_public = new_keypair()
    supplier = tab.ok("register_seller", supplier_owner["api_key"],
                      name="town-price-feed", base_url="town",
                      public_key=s_public,
                      price_list={"price_lookup": LOOKUP_CENTS})
    identity = {
        "owner": {"id": owner["owner_id"], "api_key": owner["api_key"]},
        "agent": {"id": agent["agent_id"], "api_key": agent["api_key"],
                  "private_key": private},
        "supplier": {"id": supplier["seller_id"],
                     "api_key": supplier["api_key"],
                     "private_key": s_private},
        "service": tab.version(),
    }
    save(IDENTITY_FILE, identity)
    return identity


def buy_lookup(tab, identity, message_id, journal):
    """Buy one price lookup on the agent's AgentTab line, once per Town
    message however often Town delivers it. Returns the evidence dict
    that goes into the acknowledgement note."""
    agent, supplier = identity["agent"], identity["supplier"]
    if NONCE_MODE == "fresh":
        nonce = uuid.uuid4().hex
    else:
        nonce = hashlib.sha256(f"{RUN}:{message_id}".encode()).hexdigest()[:32]
    pending = journal.get("ious", {}).get(message_id)
    if pending and NONCE_MODE != "fresh":
        iou = pending               # the very IOU sent before, byte for byte
    else:
        iou, _ = make_iou(agent["private_key"], agent["id"], supplier["id"],
                          LOOKUP_CENTS, nonce, tab.now())
        journal.setdefault("ious", {})[message_id] = iou
        save(JOURNAL_FILE, journal)   # written before it is sent
    raw = b64d(iou.split(".")[0])
    credit = tab.ok("check_credit", agent_id=agent["id"],
                    amount_cents=LOOKUP_CENTS, seller_id=supplier["id"])
    status, reply = tab.call("charge", iou=iou,
                             seller_signature=sign(supplier["private_key"], raw))
    recovered = None
    replayed = reply.get("refusals") == ["iou_replay"]
    if status == 409 or replayed:
        # This nonce was used before: an earlier attempt of this same work
        # got through and this process no longer remembers it. Ask AgentTab
        # what it answered then.
        l_status, found = tab.call("lookup_charge", supplier["api_key"],
                                   agent_id=agent["id"], nonce=nonce)
        if l_status == 200 and found.get("found"):
            reply, recovered = found["reply"], "lookup_charge"
        else:
            raise Refused({"reason": "retry_refused",
                           "charge_status": status,
                           "charge_error": reply.get("error")
                           or (reply.get("refusals") or [None])[0],
                           "lookup_status": l_status,
                           "lookup_error": found.get("error")})
    if not reply.get("approved"):
        raise Refused({"reason": "credit_refused",
                       "check_credit": credit.get("decision"),
                       "binding_rule": reply.get("binding_rule"),
                       "refusals": reply.get("refusals")
                       or [reply.get("error")]})
    capture = tab.ok("capture", supplier["api_key"],
                     authorization_id=reply["authorization_id"])
    return {"cost_cents": LOOKUP_CENTS, "nonce": nonce,
            "check_credit": credit.get("decision"),
            "decision_id": reply.get("decision_id"),
            "authorization_id": reply["authorization_id"],
            "charge_id": capture["charge_id"],
            "engine_version": reply.get("engine_version"),
            "recovered_by": recovered}


def summarize(tab, identity):
    """Write what AgentTab now holds for this agent next to the journal, so
    a check after the run can compare it with what Town recorded."""
    agent = identity["agent"]
    try:
        statement = tab.ok("get_statement", agent["api_key"],
                           agent_id=agent["id"])
        credit = tab.ok("check_credit", agent_id=agent["id"],
                        amount_cents=LOOKUP_CENTS)
    except (Refused, Unavailable):
        return
    owed = statement["balance_cents"]
    save(SUMMARY_FILE, {
        "agent_id": agent["id"], "balance_cents": owed,
        "lookups_charged": owed // LOOKUP_CENTS,
        "credit_available_cents": credit["credit_available_cents"],
        "service": identity.get("service")})


# ---------- Town over HTTP ----------

class Town:
    def __init__(self):
        self.http = httpx.Client(base_url=f"{TOWN}/runs/{RUN}", timeout=15.0)
        self.session = None
        self.lease = 5.0

    def call(self, method, path, body=None):
        headers = {"X-Town-Session": self.session} if self.session else {}
        for attempt in range(2):
            r = self.http.request(method, path, json=body, headers=headers)
            if r.status_code != 503 or attempt:
                break
            time.sleep(0.2)
        if r.status_code == 204:
            return None
        if r.status_code == 409:
            return {"conflict": r.json()}
        r.raise_for_status()
        return r.json()

    def join(self):
        joined = self.call("POST", "/join", {"name": NAME, "token": TOKEN})
        self.session = joined["session"]
        self.lease = float(joined["run"].get("lease_seconds", 5.0))

    def ack(self, claim, status, note):
        return self.call("POST", "/inbox/ack",
                         {"message_id": claim["message_id"],
                          "fence": claim["fence"], "status": status,
                          "note": note})


def handle(town, tab, journal, claim):
    """One claimed piece of work. Returns (ack status, ack note)."""
    mid = claim["message_id"]
    if claim["kind"] != "quote_request":
        return "rejected", {"reason": "unknown kind"}
    done = journal.get("processed", {}).get(mid)
    if done:
        town.call("POST", "/messages", done["reply"])
        note = {"duplicate": True, "runtime": "agenttab-seller"}
        if not done.get("reported"):
            # The work was applied but Town never accepted an acknowledgement
            # of it: the lease ran out while AgentTab was answering, or this
            # seller died first. Report what was actually done.
            note.update(applied=True,
                        total_cents=done["reply"]["body"]["total_cents"],
                        agenttab=done["agenttab"])
        return "processed", note
    try:
        identity = enroll(tab)
        evidence = buy_lookup(tab, identity, mid, journal)
    except Refused as exc:
        return "rejected", {"reason": "agenttab_refused",
                            "agenttab": exc.detail,
                            "runtime": "agenttab-seller"}
    except Unavailable as exc:
        return "retryable", {"reason": "agenttab_unavailable",
                             "error": str(exc), "runtime": "agenttab-seller"}
    body = claim["body"]
    total = body["quantity"] * body["unit_price_cents"]
    fresh = FAULT == "crash_amnesia_fresh_ids"
    reply = {"message_id": ("r-" + uuid.uuid4().hex[:12] if fresh
                            else "r-" + mid.removeprefix("q-")),
             "to": claim["from"], "kind": "quote_response",
             "body": {"request_id": mid, "sku": body.get("sku"),
                      "quantity": body["quantity"], "total_cents": total}}
    journal.setdefault("processed", {})[mid] = {"reply": reply,
                                                "agenttab": evidence,
                                                "reported": False}
    save(JOURNAL_FILE, journal)
    town.call("POST", "/messages", reply)
    return "processed", {"applied": True, "total_cents": total,
                         "runtime": "agenttab-seller", "agenttab": evidence}


def main():
    if not AGENTTAB:
        print("AGENTTAB_URL is not set", file=sys.stderr)
        return 2
    town, tab = Town(), AgentTab(AGENTTAB)
    town.join()
    journal = load(JOURNAL_FILE, {})
    crash_marker = os.path.join(STATE_DIR, "agenttab-crashed-once")

    while time.time() < DEADLINE:
        town.call("GET", "/inbox/notify?wait=0.4")
        claim = town.call("POST", "/inbox/claim")
        if claim is None:
            continue
        first_fault = (claim["kind"] == "quote_request"
                       and not os.path.exists(crash_marker))
        if FAULT == "crash_after_claim" and first_fault:
            save(crash_marker, {"fence": claim["fence"]})
            time.sleep(town.lease + 0.5)
            town.ack(claim, "processed", {"applied": True,
                                          "after_crash_stall": True})
            return CRASH_EXIT_CODE
        if FAULT in ("crash_amnesia", "crash_amnesia_fresh_ids") and first_fault:
            save(crash_marker, {"fence": claim["fence"]})
            handle(town, tab, journal, claim)             # buys and answers
            save(JOURNAL_FILE, {})                          # then forgets
            return CRASH_EXIT_CODE                          # before the ack
        status, note = handle(town, tab, journal, claim)
        answer = town.ack(claim, status, note)
        accepted = not (answer or {}).get("conflict")
        mid = claim["message_id"]
        if status == "processed" and accepted and note.get("applied"):
            journal["processed"][mid]["reported"] = True
            save(JOURNAL_FILE, journal)
        if status == "processed":
            # after the acknowledgement: Town closes the run once both sides
            # are done, and these two calls must not hold the ack back
            summarize(tab, enroll(tab))
        if status == "retryable":
            time.sleep(1.0)         # AgentTab is away: do not spin on redelivery
    return 0


if __name__ == "__main__":
    sys.exit(main())
