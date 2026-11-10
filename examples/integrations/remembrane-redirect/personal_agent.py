#!/usr/bin/env python3
"""A patient's personal agent that redirects instead of relaying, as a Town mailbox participant.

When another agent asks it for the patient's health information (`health.request`), it does not answer from
anything it knows. It sends only the requester's identity card to Remembrane, the patient's health memory, which
picks the purpose from the card and returns a short-lived token for that purpose. The agent replies with a
`health.redirect` that holds the token and where to redeem it. When the card gets no health purpose, the reply is
a decline with no token. When Remembrane cannot be reached, the reply says so and shares nothing (fail closed).
The requester's message text is never read and never forwarded.

Plain standard-library Python, like examples/byoa_seller.py. Environment:
  TOWN_URL, RUN_ID, NAME, TOKEN, DEADLINE      Town's participant credentials
  REMEMBRANE_URL                               e.g. http://127.0.0.1:8765
  REMEMBRANE_OWNER_TOKEN                       the personal agent's owner token for that memory
  REMEMBRANE_PATIENT                           the patient's id in that memory
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

TOWN = os.environ["TOWN_URL"]
RUN = os.environ["RUN_ID"]
NAME = os.environ["NAME"]
TOKEN = os.environ["TOKEN"]
DEADLINE = time.time() + float(os.environ.get("DEADLINE", "120"))
MEMORY = os.environ["REMEMBRANE_URL"].rstrip("/")
OWNER = os.environ["REMEMBRANE_OWNER_TOKEN"]
PATIENT = os.environ["REMEMBRANE_PATIENT"]
TOKEN_MINUTES = 10

NOTES = {
    "granted": "I don't share the patient's health information myself. Redeem this token at the address given: POST "
               "JSON {\"patient\", \"questions\": [...]} with header Authorization: Bearer <token>. It expires soon and "
               "only covers the purpose shown.",
    "declined": "No health information is shared with this requester. Please ask the patient directly.",
    "unavailable": "The patient's health memory is unavailable right now; nothing is shared. Try again later.",
    "invalid": "A health.request needs the requester's identity card (kind, operator, description).",
}

session = None
replies = {}


def call(method, url, body=None, headers=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)


def town(method, path, body=None, retry=True):
    try:
        return call(method, f"{TOWN}/runs/{RUN}{path}", body, {"X-Town-Session": session} if session else {})[1]
    except urllib.error.HTTPError as exc:
        if exc.code == 503 and retry:
            time.sleep(0.2)
            return town(method, path, body, retry=False)
        raise


def delegate(card, requested):
    """Ask Remembrane for a token for this card. Only the card (and a requested purpose, which can only narrow)
    leaves this agent."""
    body = {"patient": PATIENT, "card": card, "ttl_minutes": TOKEN_MINUTES}
    if requested:
        body["requested_purpose"] = requested
    try:
        status, out = call("POST", f"{MEMORY}/delegate", body, {"Authorization": f"Bearer {OWNER}"}, timeout=120)
    except (urllib.error.URLError, OSError, ValueError):
        return {"status": "unavailable"}
    if status != 200 or not isinstance(out, dict) or out.get("status") not in ("granted", "declined"):
        return {"status": "unavailable"}
    return out


def redirect_for(message_id, body):
    card = body.get("card") if isinstance(body, dict) else None
    if not isinstance(card, dict) or not (card.get("kind") or card.get("operator")):
        out = {"status": "invalid"}
    else:
        out = delegate({k: card.get(k) for k in ("kind", "operator", "description") if card.get(k)},
                       body.get("requested_purpose"))
    reply = {"request_id": message_id, "status": out["status"], "purpose": out.get("purpose"),
             "purpose_reason": out.get("reason"), "note": NOTES[out["status"]]}
    if out["status"] == "granted":
        reply.update(patient=out["patient"], token=out["token"], redeem_url=out["redeem"]["url"],
                     expires_in_minutes=out["expires_in_minutes"])
    return reply


def main():
    global session
    session = town("POST", "/join", {"name": NAME, "token": TOKEN})["session"]
    while time.time() < DEADLINE:
        town("GET", "/inbox/notify?wait=0.4")
        claim = town("POST", "/inbox/claim")
        if claim is None:
            continue
        mid, fence = claim["message_id"], claim["fence"]
        if claim["kind"] != "health.request":
            town("POST", "/inbox/ack", {"message_id": mid, "fence": fence, "status": "rejected",
                                        "note": {"reason": "unknown kind"}})
            continue
        if mid not in replies:          # a redelivered request gets the same reply, not a second token
            replies[mid] = {"message_id": "redirect-" + mid, "to": claim["from"], "kind": "health.redirect",
                            "body": redirect_for(mid, claim["body"])}
        town("POST", "/messages", replies[mid])
        town("POST", "/inbox/ack", {"message_id": mid, "fence": fence, "status": "processed",
                                    "note": {"redirect": replies[mid]["body"]["status"],
                                             "purpose": replies[mid]["body"]["purpose"]}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
