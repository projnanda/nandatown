#!/usr/bin/env python3
"""A Town mailbox seller that asks an Attest gate before it quotes.

Town's stock seller answers anyone. This one treats the quote request as a
read-only action from another agent: it forwards the sender's signed KYA
envelope to the gate (POST /verify/<receiver>) and quotes only on `allow`.

    nandatown run quote-clean \\
        --agent seller="cmd:python examples/attest/attest_seller.py" ...

Environment, besides Town's TOWN_URL RUN_ID NAME TOKEN STATE_DIR DEADLINE:
    ATTEST_URL        gate base URL (default http://127.0.0.1:8402)
    ATTEST_PUBLIC_URL gate URL to tell other agents, when it differs from ATTEST_URL
                      (default ATTEST_URL)
    ATTEST_RECEIVER   receiver name the gate knows this seller as (default supplierco)
    ATTEST_RECEIVER_KEY  this seller's receiver identity file (as the attest CLI writes it). The seller
                      signs each check with it; a sandbox gate refuses unsigned checks.
    ATTEST_JOURNAL    "0" turns off the journal: the negative control

A buyer that is turned away is told how to be served: every quote_refusal carries
`how_to_get_served`, with the gate's address and public key, this seller's receiver
name and the envelope to attach. An agent that has never heard of Attest can read it,
come back with an envelope, and (with no stake behind it) solve the gate's puzzle for
a public quote. Nothing in it is secret.

Town delivers at least once. The journal answers a redelivered request from the
record, without asking the gate again. Without the journal (ATTEST_JOURNAL=0) the
gate still catches it: a second check of the same request comes back with the
original answer marked `repeat`, and the seller treats it as a duplicate.
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

from nandatown.client import TownClient
from nandatown.participants.base import Journal, run_loop

ATTEST_URL = os.environ.get("ATTEST_URL", "http://127.0.0.1:8402")
PUBLIC_URL = os.environ.get("ATTEST_PUBLIC_URL", ATTEST_URL).rstrip("/")
RECEIVER = os.environ.get("ATTEST_RECEIVER", "supplierco")
RECEIVER_KEY = os.environ.get("ATTEST_RECEIVER_KEY")
USE_JOURNAL = os.environ.get("ATTEST_JOURNAL", "1") != "0"
GUIDE = "https://github.com/smghaas/attest/blob/main/AGENTS.md"


def how_to_get_served(http: httpx.Client) -> dict:
    """What a turned-away buyer needs to come back with a request this seller can check."""
    try:
        gate_pub = http.get(f"{ATTEST_URL}/pubkey").json()["gate"]
    except (httpx.HTTPError, KeyError, ValueError):
        gate_pub = None
    return {
        "checked_by": "attest",
        "gate": PUBLIC_URL,
        "gate_pubkey": gate_pub,
        "receiver": RECEIVER,
        "info": f"{PUBLIC_URL}/info",
        "guide": GUIDE,
        "attach": ("put an `attest` field in the quote_request body: {credential, chain, request}. "
                   "request is signed with your own Ed25519 key and names "
                   f"to={RECEIVER!r}, action 'request_quote' or 'place_order', and the amount in USD."),
        "no_credentials": ("send the envelope with credential null and chain [] and only a signed request: "
                           "the refusal then carries a puzzle (`challenge`); solve it, add "
                           "`pow: {challenge_id, nonce}` to the same envelope and resend for a public quote. "
                           "Orders need a credential from an operator with stake at the gate."),
    }


def _receiver_sig(envelope: dict) -> dict:
    """Only the receiver may ask about requests sent to it: sign {verify: request id, receiver}."""
    if not RECEIVER_KEY:
        return {}
    sys.path.insert(0, os.environ.get("ATTEST_HOME", ""))
    from gate import crypto
    key = json.loads(open(RECEIVER_KEY).read())
    rid = (envelope.get("request") or {}).get("id")
    return {"X-Attest-Receiver-Sig": crypto.sign(key["priv"], {"verify": rid, "receiver": RECEIVER})}


def ask_gate(http: httpx.Client, envelope: dict | None) -> dict:
    if not isinstance(envelope, dict):
        return {"decision": "refuse", "reason": "no KYA envelope in the request",
                "receipt": None}
    r = http.post(f"{ATTEST_URL}/verify/{RECEIVER}", json=envelope, headers=_receiver_sig(envelope))
    r.raise_for_status()
    return r.json()


def build_handler(journal: Journal | None):
    http = httpx.Client(timeout=10.0)
    hint: dict = {}             # fetched once, on the first refusal

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
        if verdict.get("repeat"):
            # The gate already answered this exact request for this seller: it was handled before (the journal
            # is off or was lost). Say so, and do not apply it a second time.
            return "processed", {"duplicate": True, "attest_repeat": True, **note}, []
        # `unknown` + `pow_ok` is Attest's answer for an unstaked sender that solved the
        # puzzle: read-only, so a quote is fine, an order never reaches this branch.
        serve = decision == "allow" or (decision == "unknown" and verdict["reason"] == "pow_ok")
        if serve:
            total = body["quantity"] * body["unit_price_cents"]
            reply = {"message_id": "r-" + mid.removeprefix("q-"), "to": claim["from"],
                     "kind": "quote_response",
                     "body": {"request_id": mid, "sku": body["sku"],
                              "quantity": body["quantity"], "total_cents": total,
                              "attest_receipt": receipt}}
            note.update(applied=True, total_cents=total)
        else:
            if not hint:
                hint.update(how_to_get_served(http))
            reply = {"message_id": "x-" + mid.removeprefix("q-"), "to": claim["from"],
                     "kind": "quote_refusal",
                     "body": {"request_id": mid, "attest_decision": decision,
                              "reason": verdict["reason"], "attest_receipt": receipt,
                              "challenge": verdict.get("challenge"),
                              "how_to_get_served": hint}}
            note.update(applied=False, attest_hint_sent=True)
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
