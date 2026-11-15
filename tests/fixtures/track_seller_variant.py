#!/usr/bin/env python3
"""Town-authored test fixture: a stdlib mailbox seller (same shape as
examples/byoa_seller.py) with one deliberate quote-response behaviour
selected by argv[1]. It is not an independent agent.

modes:
  normal       answer each request once; resend the same reply on redelivery
  dupresp      answer one request with two distinct response identities
  uuidresp     answer a redelivered request under a fresh response identity
  norequestid  answer with a request_id naming a request that does not exist
  hugerequestid  answer with a ~200 KB request_id that starts with the
               request's id but is not it
  wrongtotal   answer once with a total 9 cents above quantity times unit
               price, and acknowledge that wrong total as applied
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"
TOWN = os.environ["TOWN_URL"]
RUN = os.environ["RUN_ID"]
NAME = os.environ["NAME"]
TOKEN = os.environ["TOKEN"]
DEADLINE = time.time() + float(os.environ.get("DEADLINE", "45"))

session = None
processed = {}


def call(method, path, body=None, retry=True):
    url = f"{TOWN}/runs/{RUN}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if session:
        req.add_header("X-Town-Session", session)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 204:
                return None
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 503 and retry:
            time.sleep(0.2)
            return call(method, path, body, retry=False)
        raise


def ack(mid, fence, status, note):
    call("POST", "/inbox/ack", {"message_id": mid, "fence": fence,
                                "status": status, "note": note})


def main():
    global session
    session = call("POST", "/join", {"name": NAME, "token": TOKEN})["session"]
    while time.time() < DEADLINE:
        call("GET", "/inbox/notify?wait=0.4")
        claim = call("POST", "/inbox/claim")
        if claim is None:
            continue
        mid, fence = claim["message_id"], claim["fence"]
        if claim["kind"] != "quote_request":
            ack(mid, fence, "rejected", {"reason": "unknown kind"})
            continue
        if mid in processed:
            reply = processed[mid]
            if MODE == "uuidresp":
                reply = dict(reply, message_id="r-" + uuid.uuid4().hex[:8])
            call("POST", "/messages", reply)
            ack(mid, fence, "processed", {"duplicate": True})
            continue
        body = claim["body"]
        total = body["quantity"] * body["unit_price_cents"]
        if MODE == "wrongtotal":
            total += 9
        request_id = "q-does-not-exist" if MODE == "norequestid" else mid
        if MODE == "hugerequestid":
            request_id = mid + "-" + "x" * 200_000
        reply = {"message_id": "r-" + mid.removeprefix("q-"),
                 "to": claim["from"], "kind": "quote_response",
                 "body": {"request_id": request_id, "total_cents": total}}
        call("POST", "/messages", reply)
        if MODE == "dupresp":
            call("POST", "/messages",
                 dict(reply, message_id=reply["message_id"] + "-again"))
        processed[mid] = reply
        ack(mid, fence, "processed", {"applied": True, "total_cents": total})
    return 0


if __name__ == "__main__":
    sys.exit(main())
