#!/usr/bin/env python3
"""A Town mailbox buyer that signs its quote request the way Attest expects.

It joins as the Town buyer, finds the `quote.read` seller, and sends the usual
quote_request with one extra field, `attest`: its credential, mandate chain and a
request signed with its own key. The quote that comes back is checked twice:
the total (as Town's stock buyer does) and the gate's signed receipt.

Who it is, one of:
    ATTEST_CAST + ATTEST_AGENT   a member of the cast world.py wrote (test keys)
    ATTEST_IDENTITY              an agent file made with Attest's own CLI
                                 (`attest agent create`, kept in ~/.attest/<name>.json)
    neither, with ATTEST_DISCOVER=1   a fresh key with no credential: a stranger

Environment, besides Town's TOWN_URL RUN_ID NAME TOKEN STATE_DIR DEADLINE:
    ATTEST_HOME     checkout of github.com/smghaas/attest (for its signing helpers)
    ATTEST_ACTION   action the request names: request_quote (default) or place_order
    ATTEST_AMOUNT   amount in USD; default is the quote's total
    ATTEST_SIGNER   cast member whose key signs instead (a stolen identity)
    ATTEST_TAMPER   "1" changes the amount after signing (the boundary case)
    ATTEST_SOLVE_POW  "1" solves the puzzle the gate sets an unstaked sender and resends
    ATTEST_URL      gate base URL, used to fetch the gate's public key
    ATTEST_RECEIVER receiver name of the seller at the gate (default: the cast's, else supplierco)
    ATTEST_DISCOVER "1" first asks like a stock Town buyer, with no envelope, and learns
                    the gate, its key and the receiver name from the seller's refusal
                    (`how_to_get_served`); then asks again with an envelope. ATTEST_URL and
                    ATTEST_RECEIVER are ignored in this mode: only what the seller said counts

Exit codes follow the stock buyer: 0 correct, 4 incorrect, 5 no response, plus
6 when the gate's receipt is missing or does not verify.
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.environ["ATTEST_HOME"])
from bench import world as bw  # noqa: E402
from gate import crypto  # noqa: E402
from gate.core import solve_pow  # noqa: E402

from nandatown.client import TownClient  # noqa: E402
from nandatown.participants.base import run_loop  # noqa: E402
from nandatown.participants.buyer import (EXIT_CORRECT, EXIT_INCORRECT,  # noqa: E402
                                          EXIT_NO_RESPONSE, find_seller)

EXIT_BAD_RECEIPT = 6


def whoami(env) -> tuple[dict, dict | None]:
    """(this agent, the cast or None)."""
    if env.get("ATTEST_CAST"):
        cast = json.load(open(env["ATTEST_CAST"]))
        return cast[env.get("ATTEST_AGENT", "proc")], cast
    if env.get("ATTEST_IDENTITY"):
        return json.load(open(env["ATTEST_IDENTITY"])), None
    return bw.bare("stranger"), None


def main() -> None:
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    task = client.run_context["task"]
    seller = find_seller(client)
    if seller is None:
        sys.exit(EXIT_NO_RESPONSE)
    me, cast = whoami(env)
    total = task["quantity"] * task["unit_price_cents"]
    usd = float(env.get("ATTEST_AMOUNT", total / 100))
    discover = env.get("ATTEST_DISCOVER") == "1"
    if discover:                    # knows nothing about the gate until the seller says
        gate = {"url": None, "receiver": None, "pub": None}
    else:
        gate = {"url": env.get("ATTEST_URL", "http://127.0.0.1:8402").rstrip("/"),
                "receiver": env.get("ATTEST_RECEIVER") or (cast or {}).get("receiver", "supplierco"),
                "pub": (cast or {}).get("gate_pub")}
    outcome: dict = {}
    sent: dict = {}

    def ask(message_id, attest):
        body = {"sku": task["sku"], "quantity": task["quantity"],
                "unit_price_cents": task["unit_price_cents"]}
        if attest is not None:
            body["attest"] = attest
        client.send(message_id=message_id, to=seller, kind="quote_request", body=body)

    def ask_with_envelope(message_id):
        sent["envelope"] = bw.request(
            me, env.get("ATTEST_ACTION", "request_quote"), usd, f"quote for {task['sku']}",
            to=gate["receiver"],
            presented_amount=usd * 100 if env.get("ATTEST_TAMPER") == "1" else None,
            signer=cast[env["ATTEST_SIGNER"]] if env.get("ATTEST_SIGNER") and cast else None)
        ask(message_id, sent["envelope"])

    if discover:
        ask("q-0", None)            # like any Town buyer: no envelope, no idea a gate exists
    else:
        ask_with_envelope("q-1")

    def handler(claim):
        b = claim["body"]
        if claim["kind"] == "quote_refusal":
            how = b.get("how_to_get_served") or {}
            if discover and "envelope" not in sent and how.get("checked_by") == "attest":
                # Learn where the seller's gate is and what it knows the seller as.
                gate.update(url=how["gate"].rstrip("/"), receiver=how["receiver"],
                            pub=how.get("gate_pubkey"))
                ask_with_envelope("q-1")
                return "processed", {"refused": True, "attest_decision": b["attest_decision"],
                                     "reason": b["reason"], "learned_gate": gate["url"],
                                     "learned_receiver": gate["receiver"]}, []
            ch = b.get("challenge")
            if ch and env.get("ATTEST_SOLVE_POW") == "1" and "retried" not in outcome:
                # Unstaked sender: do the work the gate asks for, then ask again (same request).
                nonce, _ = solve_pow(ch["seed"], ch["bits"])
                outcome["retried"] = True
                ask("q-2", {**sent["envelope"], "pow": {"challenge_id": ch["challenge_id"], "nonce": nonce}})
                return "processed", {"refused": True, "attest_decision": b["attest_decision"],
                                     "reason": b["reason"], "pow_solved_bits": ch["bits"]}, []
            outcome["refused"] = b["attest_decision"]
            return "processed", {"refused": True, "attest_decision": b["attest_decision"],
                                 "reason": b["reason"]}, []
        if claim["kind"] != "quote_response":
            return "rejected", {"reason": "unknown kind"}, []
        rc = b.get("attest_receipt") or {}
        gate_pub = httpx.get(gate["url"] + "/pubkey").json()["gate"]
        receipt_ok = (rc.get("gate") == gate_pub and gate["pub"] in (None, gate_pub)
                      and crypto.id_ok(rc)
                      and crypto.verify(gate_pub, crypto.body_of(rc), rc.get("sig", ""))
                      and (rc.get("decision") == "allow" or rc.get("reason") == "pow_ok")
                      and rc.get("request_id") == sent.get("envelope", {}).get("request", {}).get("id"))
        correct = b["total_cents"] == task["expected_total_cents"]
        outcome.update(correct=correct, receipt_ok=receipt_ok)
        return "processed", {"correct": correct, "total_cents": b["total_cents"],
                             "expected_total_cents": task["expected_total_cents"],
                             "attest_receipt_verified": receipt_ok}, []

    deadline = time.time() + float(env.get("DEADLINE", "25"))
    run_loop(client, handler,
             until=lambda: bool({k: v for k, v in outcome.items() if k != "retried"})
             or time.time() > deadline)
    if "correct" in outcome:
        if not outcome["receipt_ok"]:
            sys.exit(EXIT_BAD_RECEIPT)
        sys.exit(EXIT_CORRECT if outcome["correct"] else EXIT_INCORRECT)
    sys.exit(EXIT_NO_RESPONSE)


if __name__ == "__main__":
    main()
