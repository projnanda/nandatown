#!/usr/bin/env python3
"""A Track seller whose one external side effect goes through ActionLedger.

Town's stock seller applies a quote by computing a total. A real seller
usually also writes to a system Town does not own: here it places a stock
hold in a task tracker before it answers. Town's journal and message
identity keep the *messages* idempotent; they cannot see that external
write. This seller routes it through ActionLedger
(https://github.com/Cecilia1220/actionledger), a separately running
service that persists the request, calls the tracker, and verifies an
uncertain outcome before it reports one.

    nandatown run quote-amnesia-restart \
        --agent seller=cmd:"python examples/actionledger/actionledger_seller.py"

    --path actionledger   (default) POST LEDGER_URL/execute
    --path direct         negative control: POST TRACKER_URL/tasks itself

The ledger action identity is derived from Town's own identities,
``town:{RUN_ID}:{message_id}``, so a redelivery, a restart, or a seller
that lost its journal asks the ledger about the same action instead of
creating a new one. The direct path uses the same tag but has nothing to
ask: it writes again.

Only CONFIRMED_SUCCESS and RECOVERED_SUCCESS let the seller answer. Any
other ledger status (AMBIGUOUS above all) is acknowledged ``failed`` with
the ledger's status in the note and no quote is sent: an unverifiable
hold is escalated, never retried blindly. SAFE_TO_RETRY is repeated once
with the identical request, as ActionLedger's contract allows.

Faults are Town's own: FAULT=crash_after_claim and crash_amnesia wrap
this handler with the stock seller's wrappers, so the crash, the lost
journal and the exit code are exactly what Town's scripted seller does.

Environment: TOWN_URL, RUN_ID, NAME, TOKEN, STATE_DIR, DEADLINE, FAULT
(from Town); LEDGER_URL, AGENT_KEY (ActionLedger); TRACKER_URL,
TRACKER_KEY (direct path only). Defaults match ActionLedger's local
demo configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable

import httpx

from nandatown.client import TownClient
from nandatown.participants.base import Journal, run_loop
from nandatown.participants.seller import (ack_accepted, amnesia_wrapper,
                                           build_handler, crash_wrapper)

SUCCESS = ("CONFIRMED_SUCCESS", "RECOVERED_SUCCESS")
TOOL = "tracker.create_task"
# ActionLedger's own tracker timeout is 3 s; with a visibility bound it may
# re-check for longer before it answers, so give it room.
LEDGER_TIMEOUT = 30.0


def action_id(run_id: str, message_id: str) -> str:
    return f"town:{run_id}:{message_id}"


def hold_payload(claim: dict[str, Any]) -> dict[str, Any]:
    """Derived only from the request, so every application is identical."""
    body = claim["body"]
    return {"title": f"Hold stock for quote {claim['message_id']}",
            "sku": body["sku"], "quantity": body["quantity"],
            "total_cents": body["quantity"] * body["unit_price_cents"]}


def log(state_dir: str, **fields: Any) -> None:
    with open(os.path.join(state_dir, "actionledger_seller.log"), "a",
              encoding="utf-8") as f:
        f.write(json.dumps({"at": time.time(), **fields}) + "\n")


def ledger_writer(run_id: str, http: httpx.Client, url: str, key: str
                  ) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def write(claim: dict[str, Any]) -> dict[str, Any]:
        aid = action_id(run_id, claim["message_id"])
        request = {"action_id": aid, "workflow_id": f"town:{run_id}",
                   "tool": TOOL, "payload": hold_payload(claim)}
        note: dict[str, Any] = {"path": "actionledger", "action_id": aid}
        for attempt in (1, 2):
            try:
                r = http.post(f"{url}/execute", json=request,
                              headers={"X-Agent-Key": key},
                              timeout=LEDGER_TIMEOUT)
            except httpx.HTTPError as exc:
                return {**note, "ledger_status": "UNREACHABLE",
                        "error": type(exc).__name__}
            if r.status_code == 409:
                return {**note, "ledger_status": "ID_CONFLICT"}
            if r.status_code != 200:
                return {**note, "ledger_status": "HTTP_ERROR",
                        "http_status": r.status_code}
            result = r.json()
            status = result.get("status")
            if status == "SAFE_TO_RETRY" and attempt == 1:
                continue  # the ledger guarantees the write did not happen
            note.update(ledger_status=status, attempts=attempt)
            if status in SUCCESS:
                note["hold"] = result.get("resource_id")
            else:
                reason = (result.get("evidence") or {}).get("reason")
                if reason:
                    note["ledger_reason"] = reason
            return note
        return note

    return write


def direct_writer(run_id: str, http: httpx.Client, url: str, key: str
                  ) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Negative control: the same write with no ledger in front of it."""
    def write(claim: dict[str, Any]) -> dict[str, Any]:
        aid = action_id(run_id, claim["message_id"])
        note: dict[str, Any] = {"path": "direct", "action_id": aid}
        try:
            r = http.post(f"{url}/tasks",
                          json={"tag": aid, "payload": hold_payload(claim)},
                          headers={"X-Tracker-Key": key}, timeout=3.0)
        except httpx.HTTPError as exc:
            return {**note, "tracker_status": "UNREACHABLE",
                    "error": type(exc).__name__}
        note["tracker_status"] = r.status_code
        if r.status_code == 200:
            note["hold"] = r.json().get("resource_id")
        return note

    return write


def with_hold(journal: Journal, write, inner, state_dir: str):
    """Place the hold before the stock handler applies the quote.

    A writer returns a note for the acknowledgement; it carries ``hold``
    (the tracker resource) only when the write is known to exist. Work this
    seller's journal already holds goes straight to the stock handler,
    which resends the original response. Everything else, including work a
    crashed or amnesiac seller forgot, asks the writer first.
    """
    def handler(claim: dict[str, Any]):
        if (claim["kind"] != "quote_request"
                or journal.seen(claim["message_id"])):
            return inner(claim)
        note = write(claim)
        log(state_dir, message_id=claim["message_id"],
            attempt=claim.get("attempt"), **note)
        if not note.get("hold"):
            # No hold we can vouch for: do not quote, do not retry blindly.
            return "failed", {"applied": False, **note}, []
        status, inner_note, replies = inner(claim)
        return status, {**inner_note, **note}, replies

    return handler


def run(client: TownClient, name: str, token: str, state_dir: str,
        fault: str, deadline_seconds: float, path: str,
        grant_json: str | None = None) -> int:
    client.join_auto(name, token, grant_json)
    env = os.environ
    http = httpx.Client()
    if path == "direct":
        write = direct_writer(client.run_id, http,
                              env.get("TRACKER_URL", "http://127.0.0.1:8001"),
                              env.get("TRACKER_KEY", "local-demo-tracker"))
    else:
        write = ledger_writer(client.run_id, http,
                              env.get("LEDGER_URL", "http://127.0.0.1:8000"),
                              env.get("AGENT_KEY", "local-demo-agent"))
    journal = Journal(os.path.join(state_dir, "journal.db"))
    handler = with_hold(journal, write, build_handler(client, journal),
                        state_dir)
    if fault == "crash_after_claim":
        lease = float(client.run_context.get("lease_seconds", 5.0))
        handler = crash_wrapper(client, journal, state_dir, lease, handler)
    elif fault == "crash_amnesia":
        handler = amnesia_wrapper(client, journal, state_dir, handler)
    deadline = time.time() + deadline_seconds
    run_loop(client, handler, until=lambda: time.time() > deadline,
             on_ack_accepted=ack_accepted(journal))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--path", choices=("actionledger", "direct"),
                        default="actionledger")
    args = parser.parse_args()
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    sys.exit(run(client, env["NAME"], env["TOKEN"], env["STATE_DIR"],
                 env.get("FAULT", "none"),
                 float(env.get("DEADLINE", "60")), args.path,
                 grant_json=env.get("TOWN_GRANT")))


if __name__ == "__main__":
    main()
