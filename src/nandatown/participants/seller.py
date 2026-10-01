"""The stock seller: claims quote requests, applies each once, responds.

Scripted crash fault (crash_after_claim): after its first claim the
seller writes a durable crash marker, stalls past its lease, tries to
acknowledge with the now-stale fence to prove the fence holds, and exits
with code 3. The runner restarts it; the journal and the town's
redelivery finish the job on attempt two.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any

from ..client import StaleFenceError, TownClient
from .base import Journal, run_loop

CRASH_EXIT_CODE = 3


def response_id(request_id: str) -> str:
    return "r-" + request_id.removeprefix("q-")


def build_handler(client: TownClient, journal: Journal,
                  fresh_ids: bool = False):
    """fresh_ids is the negative control for crash_amnesia: each
    application mints a new response identity instead of deriving it
    from the request, so a forgotten application answers twice."""
    def handler(claim: dict[str, Any]):
        if claim["kind"] != "quote_request":
            return "rejected", {"reason": "unknown kind"}, []
        message_id = claim["message_id"]
        if journal.seen(message_id):
            # Already applied: resend the original response (idempotent by
            # message identity, the town returns the original acceptance)
            # and say so, without applying again.
            done = journal.get(message_id)
            note = {"duplicate": True}
            if journal.unreported(message_id) and claim.get("duplicate"):
                # The town only re-offers work it has already completed
                # (Db.reoffer requires status 'done'), so its record holds
                # this application and the mark is merely stale. Settle
                # that here rather than leave the question open for a
                # later delivery to answer differently.
                journal.mark_reported(message_id)
            if journal.unreported(message_id):
                # The application happened, and this seller never saw an
                # acknowledgement of it accepted: the fence died first, or
                # this seller did. Report the work actually done rather
                # than let the evidence blame the seller for the timing.
                #
                # Not seeing one accepted is not the same as the town
                # holding no record, though, and the town says which this
                # is: the delivery above already settled the case where
                # it does. What is left is work the town is still waiting
                # to hear about.
                note["applied"] = True
                note["total_cents"] = done["total_cents"]
            return "processed", note, [done["reply"]]
        body = claim["body"]
        total_cents = body["quantity"] * body["unit_price_cents"]
        reply = {
            "message_id": ("r-" + uuid.uuid4().hex[:12] if fresh_ids
                           else response_id(message_id)),
            "to": claim["from"],
            "kind": "quote_response",
            "body": {"request_id": message_id, "sku": body["sku"],
                     "quantity": body["quantity"],
                     "total_cents": total_cents},
        }
        # The application and the mark saying the town has not recorded it
        # commit together, before the acknowledgement is even attempted.
        journal.record(message_id, {"reply": reply,
                                    "total_cents": total_cents},
                       unreported=True)
        return "processed", {"applied": True, "total_cents": total_cents}, [reply]

    return handler


def crash_wrapper(client: TownClient, journal: Journal, state_dir: str,
                  lease_seconds: float, inner):
    marker = os.path.join(state_dir, "crashed-once")

    def handler(claim: dict[str, Any]):
        if not os.path.exists(marker):
            with open(marker, "w") as f:
                f.write(claim["fence"])
            time.sleep(lease_seconds + 0.5)
            try:
                client.ack(claim["message_id"], claim["fence"], "processed",
                           {"applied": True, "after_crash_stall": True})
            except StaleFenceError:
                sys.exit(CRASH_EXIT_CODE)
            # The stale fence was wrongly accepted; fail loudly.
            sys.exit(9)
        return inner(claim)

    return handler


def amnesia_wrapper(client: TownClient, journal: Journal, state_dir: str,
                    inner):
    """crash_amnesia: answer the first claim, wipe the journal, exit
    before the ack. The restart has no memory of the work and applies it
    again, so only the town's message identity stops a second response.
    The marker only stops the fault firing twice."""
    marker = os.path.join(state_dir, "amnesia-once")

    def handler(claim: dict[str, Any]):
        if os.path.exists(marker):
            return inner(claim)
        with open(marker, "w") as f:
            f.write(claim["fence"])
        _, _, replies = inner(claim)
        for reply in replies:
            client.send(**reply)
        # Empty the journal rather than delete its file: the effect is the
        # same lost memory, and Windows refuses to delete an open file.
        with journal._conn() as conn:
            conn.execute("DELETE FROM processed")
        sys.exit(CRASH_EXIT_CODE)

    return handler


def ack_accepted(journal: Journal):
    """Clear the mark once the record carries the application.

    An accepted acknowledgement is the only proof that an assertion of
    this seller's own reached the evidence. Until one arrives the
    application stays marked, and a redelivery of work the town has no
    completed record of carries it again.
    """
    def on_ack_accepted(claim: dict[str, Any],
                        note: dict[str, Any]) -> None:
        if note.get("applied"):
            journal.mark_reported(claim["message_id"])

    return on_ack_accepted


def run(client: TownClient, name: str, token: str, state_dir: str,
        fault: str, deadline_seconds: float = 60.0,
        grant_json: str | None = None) -> int:
    client.join_auto(name, token, grant_json)
    journal = Journal(os.path.join(state_dir, "journal.db"))
    handler = build_handler(client, journal,
                            fresh_ids=(fault == "crash_amnesia_fresh_ids"))
    if fault == "crash_after_claim":
        lease = float(client.run_context.get("lease_seconds", 5.0))
        handler = crash_wrapper(client, journal, state_dir, lease, handler)
    elif fault in ("crash_amnesia", "crash_amnesia_fresh_ids"):
        handler = amnesia_wrapper(client, journal, state_dir, handler)
    deadline = time.time() + deadline_seconds
    run_loop(client, handler, until=lambda: time.time() > deadline,
             on_ack_accepted=ack_accepted(journal))
    return 0


def main() -> None:
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    code = run(client, env["NAME"], env["TOKEN"], env["STATE_DIR"],
               env.get("FAULT", "none"),
               deadline_seconds=float(env.get("DEADLINE", "60")),
               grant_json=env.get("TOWN_GRANT"))
    sys.exit(code)


if __name__ == "__main__":
    main()
