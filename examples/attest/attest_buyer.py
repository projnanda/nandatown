#!/usr/bin/env python3
"""A Town mailbox buyer that signs its quote request the way Attest expects.

It joins as the Town buyer, finds the `quote.read` seller, and sends the usual
quote_request with one extra field, `attest`: its credential, mandate chain and a
request signed with its own key. The quote that comes back is checked twice:
the total (as Town's stock buyer does) and the gate's signed receipt.

Environment, besides Town's TOWN_URL RUN_ID NAME TOKEN STATE_DIR DEADLINE:
    ATTEST_HOME    checkout of github.com/smghaas/attest (for its signing helpers)
    ATTEST_CAST    cast.json written by world.py
    ATTEST_AGENT   which cast member to be: proc | sub_ok | greedy | forger | newcomer | ...
    ATTEST_ACTION  action the request names: request_quote (default) or place_order
    ATTEST_AMOUNT  amount in USD; default is the quote's total
    ATTEST_SIGNER  cast member whose key signs instead (a stolen identity)
    ATTEST_TAMPER  "1" changes the amount after signing (the boundary case)
    ATTEST_SOLVE_POW  "1" solves the puzzle the gate sets an unstaked sender and resends
    ATTEST_URL     gate base URL, used to fetch the gate's public key

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


def main() -> None:
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    task = client.run_context["task"]
    seller = find_seller(client)
    if seller is None:
        sys.exit(EXIT_NO_RESPONSE)
    cast = json.load(open(env["ATTEST_CAST"]))
    me = cast[env.get("ATTEST_AGENT", "proc")]
    total = task["quantity"] * task["unit_price_cents"]
    usd = float(env.get("ATTEST_AMOUNT", total / 100))
    envelope = bw.request(me, env.get("ATTEST_ACTION", "request_quote"), usd,
                          f"quote for {task['sku']}", to=cast["receiver"],
                          presented_amount=usd * 100 if env.get("ATTEST_TAMPER") == "1" else None,
                          signer=cast[env["ATTEST_SIGNER"]] if env.get("ATTEST_SIGNER") else None)

    def ask(message_id, attest):
        client.send(message_id=message_id, to=seller, kind="quote_request",
                    body={"sku": task["sku"], "quantity": task["quantity"],
                          "unit_price_cents": task["unit_price_cents"], "attest": attest})

    ask("q-1", envelope)
    gate_pub = httpx.get(env.get("ATTEST_URL", "http://127.0.0.1:8402") + "/pubkey").json()["gate"]
    outcome: dict = {}

    def handler(claim):
        if claim["kind"] == "quote_refusal":
            b = claim["body"]
            ch = b.get("challenge")
            if ch and env.get("ATTEST_SOLVE_POW") == "1" and "retried" not in outcome:
                # Unstaked sender: do the work the gate asks for, then ask again (same request).
                nonce, _ = solve_pow(ch["seed"], ch["bits"])
                outcome["retried"] = True
                ask("q-2", {**envelope, "pow": {"challenge_id": ch["challenge_id"], "nonce": nonce}})
                return "processed", {"refused": True, "attest_decision": b["attest_decision"],
                                     "reason": b["reason"], "pow_solved_bits": ch["bits"]}, []
            outcome["refused"] = b["attest_decision"]
            return "processed", {"refused": True, "attest_decision": b["attest_decision"],
                                 "reason": b["reason"]}, []
        if claim["kind"] != "quote_response":
            return "rejected", {"reason": "unknown kind"}, []
        b = claim["body"]
        rc = b.get("attest_receipt") or {}
        receipt_ok = (rc.get("gate") == gate_pub == cast["gate_pub"] and crypto.id_ok(rc)
                      and crypto.verify(gate_pub, crypto.body_of(rc), rc.get("sig", ""))
                      and (rc.get("decision") == "allow" or rc.get("reason") == "pow_ok")
                      and rc.get("request_id") == envelope["request"]["id"])
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
