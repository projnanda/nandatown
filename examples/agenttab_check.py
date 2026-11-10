#!/usr/bin/env python3
"""Compare what Town recorded with what AgentTab charged.

Town's stages say whether the quote was delivered, applied once and
correct. They cannot see AgentTab's ledger. A seller that pays twice for
one quote still passes every Town stage. This check reads a Town
evidence bundle produced with examples/agenttab_seller.py, asks AgentTab
for the agent's balance, and compares:

    expected   one 40-cent price lookup for each quote request the seller
               acknowledged as processed, and none for a request it
               declined or never finished
    observed   the agent's AgentTab balance divided by 40

    python examples/agenttab_check.py runs/run-XXXX

It needs the bundle's state/ directory (which holds the test agent's
AgentTab keys; do not share it) and the same AGENTTAB_URL the run used.
Exit code 0 when expected equals observed, 1 when not, 2 when AgentTab
cannot be asked.
"""

import json
import os
import sys

import httpx

LOOKUP_CENTS = 40


def read_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    bundle = argv[1]
    events = read_jsonl(os.path.join(bundle, "events.jsonl"))
    result = json.load(open(os.path.join(bundle, "result.json")))
    acks = [e for e in events if e["kind"] == "ack_recorded"
            and e["observer"] == "seller"]
    paid = sorted({e["subject"] for e in acks
                   if e["detail"].get("status") == "processed"})
    declined = [e["detail"].get("note", {}) for e in acks
                if e["detail"].get("status") in ("rejected", "retryable")]
    evidence = [e["detail"]["note"]["agenttab"] for e in acks
                if isinstance(e["detail"].get("note", {}).get("agenttab"), dict)
                and e["detail"].get("status") == "processed"]
    out = {"town_verdict": result.get("verdict"),
           "requests_processed": paid,
           "expected_lookups_charged": len(paid),
           "town_recorded_charge_ids": sorted({x.get("charge_id")
                                               for x in evidence}),
           "declined_reasons": sorted({str(n.get("reason")) for n in declined})}
    identity_path = os.path.join(bundle, "state", "seller",
                                 "agenttab_identity.json")
    url = os.environ.get("AGENTTAB_URL", "").rstrip("/")
    if not os.path.exists(identity_path):
        out["observed_lookups_charged"] = 0
        out["note"] = "the seller never enrolled with AgentTab"
    elif not url:
        print("AGENTTAB_URL is not set", file=sys.stderr)
        return 2
    else:
        agent = json.load(open(identity_path))["agent"]
        try:
            r = httpx.post(f"{url}/v1/get_statement", timeout=20.0,
                           json={"agent_id": agent["id"]},
                           headers={"Authorization":
                                    f"Bearer {agent['api_key']}"})
            body = r.json()
        except (httpx.TransportError, ValueError) as exc:
            print(f"AgentTab could not be asked: {type(exc).__name__}",
                  file=sys.stderr)
            return 2
        if not body.get("ok"):
            print(f"AgentTab refused: {body.get('error')}", file=sys.stderr)
            return 2
        out["agenttab_balance_cents"] = body["balance_cents"]
        out["observed_lookups_charged"] = body["balance_cents"] // LOOKUP_CENTS
    out["match"] = (out["expected_lookups_charged"]
                    == out["observed_lookups_charged"])
    print(json.dumps(out, indent=2))
    return 0 if out["match"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
