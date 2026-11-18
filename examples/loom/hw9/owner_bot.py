"""Stand-in for the person in HW9 run 2 (rule fixed in hw9/PLAN.md).

Every 2 s: read the owner queue; approve a pending proposal exactly as
asked if its tier is L1 or shallower, for 7 days; decline anything deeper.
It talks only to the portal's owner endpoints, never to an agent.

    LOOM_OWNER_TOKEN=... python hw9/owner_bot.py http://127.0.0.1:8787 coldstart-2
"""

import json
import os
import sys
import time
import urllib.request

BASE, NS = sys.argv[1], sys.argv[2]
TOKEN = os.environ["LOOM_OWNER_TOKEN"]
TIERS = ["L0", "L1", "L2", "L3"]


def call(method, path, body=None):
    req = urllib.request.Request(f"{BASE}/v1/{NS}/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Loom-Owner-Token": TOKEN})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


start = time.time()
while True:
    for p in call("GET", "owner/proposals"):
        if p["status"] != "pending":
            continue
        ok = TIERS.index(p["tier_requested"]) >= 1 and "raw_signal" not in p["fields_requested"]
        out = call("POST", "owner/decide", {"proposal_id": p["proposal_id"], "approve": ok, "days": 7,
                                            "at": 0})
        print(json.dumps({"t": round(time.time() - start, 1), "decision": out}), flush=True)
    time.sleep(2)
