#!/usr/bin/env python3
"""A Town mailbox seller that asks an Attest gate before it quotes.

Town's stock seller answers anyone. This one treats the quote request as a
read-only action from another agent: it forwards the sender's signed KYA
envelope to the gate (POST /verify/<receiver>) and quotes only on `allow`.

    nandatown run quote-clean \\
        --agent seller="cmd:python examples/attest/attest_seller.py" ...

Environment, besides Town's TOWN_URL RUN_ID NAME TOKEN STATE_DIR DEADLINE:
    ATTEST_URL        gate base URL (default http://127.0.0.1:8402)
    ATTEST_RECEIVER   receiver name the gate knows this seller as (default supplierco)
    ATTEST_JOURNAL    "0" turns off the journal: the negative control

Town delivers at least once and the gate refuses a repeated nonce as a replay,
so the two meet on every redelivery. The journal is what reconciles them: a
request already decided is answered from the record, never sent to the gate again.
"""
from __future__ import annotations

import os
import sys
import time

import httpx

from nandatown.client import TownClient
from nandatown.participants.base import Journal, run_loop

ATTEST_URL = os.environ.get("ATTEST_URL", "http://127.0.0.1:8402")
RECEIVER = os.environ.get("ATTEST_RECEIVER", "supplierco")
USE_JOURNAL = os.environ.get("ATTEST_JOURNAL", "1") != "0"


def ask_gate(http: httpx.Client, envelope: dict | None) -> dict:
    if not isinstance(envelope, dict):
        return {"decision": "refuse", "reason": "no KYA envelope in the request",
                "receipt": None}
    r = http.post(f"{ATTEST_URL}/verify/{RECEIVER}", json=envelope)
    r.raise_for_status()
    return r.json()


def build_handler(journal: Journal | None):
    http = httpx.Client(timeout=10.0)

    def handler(claim):
        if claim["kind"] != "quote_request":
            return "rejected", {"reason": "unknown kind"}, []
        mid = claim["message_id"]
        if journal is not None and journal.seen(mid):
            done = journal.get(mid)
            # Like Town's stock seller: say "duplicate", do not claim a second application.
            seen = {k: v for k, v in done["note"].items() if k.startswith("attest_")}
            return "processed", {"duplicate": True, **seen}, [done["reply"]]
        body = claim["body"]
        verdict = ask_gate(http, body.get("attest"))
        decision, receipt = verdict["decision"], verdict.get("receipt")
        note = {"attest_decision": decision, "attest_reason": verdict["reason"],
                "attest_receipt_id": (receipt or {}).get("id"),
                "runtime": "attest-gated-seller"}
        if decision == "allow":
            total = body["quantity"] * body["unit_price_cents"]
            reply = {"message_id": "r-" + mid.removeprefix("q-"), "to": claim["from"],
                     "kind": "quote_response",
                     "body": {"request_id": mid, "sku": body["sku"],
                              "quantity": body["quantity"], "total_cents": total,
                              "attest_receipt": receipt}}
            note.update(applied=True, total_cents=total)
        else:
            reply = {"message_id": "x-" + mid.removeprefix("q-"), "to": claim["from"],
                     "kind": "quote_refusal",
                     "body": {"request_id": mid, "attest_decision": decision,
                              "reason": verdict["reason"], "attest_receipt": receipt}}
            note.update(applied=False)
        if journal is not None:
            journal.record(mid, {"note": note, "reply": reply})
        return "processed", note, [reply]

    return handler


def main() -> None:
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    journal = Journal(os.path.join(env["STATE_DIR"], "journal.db")) if USE_JOURNAL else None
    deadline = time.time() + float(env.get("DEADLINE", "60"))
    run_loop(client, build_handler(journal), until=lambda: time.time() > deadline)
    sys.exit(0)


if __name__ == "__main__":
    main()
