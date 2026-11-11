#!/usr/bin/env python3
"""Cerata as a Track seller: one Town quote_request in, one preflight decision out.

The seller role here is a thin adapter in front of Cerata, a hosted legal
preflight service for agent actions. It claims a quote_request whose body
carries a Cerata PreflightRequest (an action an agent is about to take: a
sales call, a text, an email, a rental listing or a job post), sends it to
Cerata's POST /v1/preflight, and answers with Cerata's decision (clear, flag,
hold or block), the signed receipt and, for a clear, the single-use token.

The adapter keeps no journal. Cerata's Idempotency-Key does that job: the key
is derived from the Town run and message identity, so a redelivery, a lost
acknowledgement or a restart that forgot everything asks Cerata again under
the same key and gets the stored response replayed, byte for byte. The reply
body therefore never carries anything that differs between the first answer
and a replay (the replay flag goes in the acknowledgement note instead).

CERATA_ADAPTER_NAIVE=1 is the negative control: no Idempotency-Key, so each
application is a new Cerata check with a new check id and receipt.

If Cerata cannot be reached or answers with an error, the reply says
"unavailable" and carries no decision. The adapter never turns a failure into
a clear.

Environment: CERATA_URL (default the production API), CERATA_API_KEY (a test
key; read from the environment only, never from the command line), plus the
Town participant variables TOWN_URL, RUN_ID, NAME, TOKEN, STATE_DIR, DEADLINE
and FAULT.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from nandatown.client import StaleFenceError, TownClient
from nandatown.participants.base import run_loop

CRASH_EXIT_CODE = 3
DEFAULT_URL = "https://clearance-kohl.vercel.app"
# The reply carries only what the asking agent acts on: the decision, the
# receipt to verify, the single-use token and the hold reference. Nothing
# else Cerata returns goes into the run record. All of
# it is stored by Cerata under the idempotency key, so a replay reproduces it.
KEEP = ("check_id", "decision", "receipt_id", "receipt", "token", "decision_digest")

Post = Callable[[str, dict[str, Any], dict[str, str]], tuple[int, dict[str, str], Any]]


def idempotency_key(run_id: str, message_id: str) -> str:
    """Stable across redelivery and restarts; unique per run and request."""
    digest = hashlib.sha256(f"{run_id}\n{message_id}".encode()).hexdigest()
    return "town-" + digest[:40]


def http_post(base_url: str, timeout: float = 35.0) -> Post:
    def post(path: str, body: dict[str, Any], headers: dict[str, str]):
        req = urllib.request.Request(
            base_url.rstrip("/") + path, data=json.dumps(body).encode(),
            method="POST", headers={"content-type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                payload = {}
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return 0, {}, {"error": type(exc).__name__}
    return post


def preflight(post: Post, api_key: str, request: dict[str, Any],
              key: str | None, attempts: int = 4) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (reply fields, acknowledgement note)."""
    headers = {"authorization": f"Bearer {api_key}"}
    if key is not None:
        headers["idempotency-key"] = key
    status, resp_headers, payload = 0, {}, {}
    for _ in range(attempts):
        status, resp_headers, payload = post("/v1/preflight", request, headers)
        # 409: the same key is still in flight (an earlier attempt of ours).
        if status == 409:
            time.sleep(min(float(resp_headers.get("retry-after", "1") or 1), 3.0))
            continue
        break
    note = {"cerata_status": status,
            "idempotency_key_sent": key is not None,
            "idempotent_replayed": resp_headers.get("idempotent-replayed") == "true"}
    # 200 is a decided check; 202 is a hold (a person or panel decides later; Location names it).
    if status not in (200, 202) or not isinstance(payload, dict) or "decision" not in payload:
        code = (payload.get("error") or {}).get("code") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else None
        return ({"status": "unavailable", "cerata_status": status,
                 "error": code or (payload.get("error") if isinstance(payload, dict) else None)}, note)
    reply = {"status": "decided"}
    reply.update({k: payload[k] for k in KEEP if k in payload})
    if isinstance(payload.get("hold"), dict):
        reply["hold"] = {k: payload["hold"][k] for k in ("hold_id", "state") if k in payload["hold"]}
    note.update({"decision": payload["decision"], "check_id": payload.get("check_id"),
                 "receipt_id": payload.get("receipt_id")})
    return reply, note


def build_handler(post: Post, api_key: str, run_id: str, naive: bool):
    def handler(claim: dict[str, Any]):
        if claim["kind"] != "quote_request":
            return "rejected", {"reason": "unknown kind"}, []
        body = claim.get("body") or {}
        request = body.get("preflight")
        if not isinstance(request, dict):
            # Town's stock buyer asks for widgets; this seller only answers
            # preflight requests and says so rather than inventing a price.
            return "rejected", {"reason": "not a Cerata preflight request"}, []
        mid = claim["message_id"]
        key = None if naive else idempotency_key(run_id, mid)
        fields, note = preflight(post, api_key, request, key)
        reply = {"message_id": "r-" + mid.removeprefix("q-"), "to": claim["from"],
                 "kind": "quote_response",
                 "body": {"request_id": mid, "case": body.get("case"), **fields}}
        if claim.get("duplicate"):
            note["duplicate"] = True
        # Exactly one Cerata check per request, reported exactly once, with no
        # journal here. Cerata's Idempotent-Replayed says whether this call
        # ran a new check; the town's `duplicate` flag says whether it already
        # holds a completed record. A replay the town has no record of is an
        # earlier check whose report was lost (a crash before the ack), so it
        # is reported now; a replay of recorded work is not applied again.
        # "unavailable" is still a processed request: the answer the buyer
        # gets is "no decision, do not act", which is the fail-closed reply.
        replayed = note["idempotent_replayed"]
        applied = (not replayed) or (not claim.get("duplicate"))
        if replayed and applied:
            note["applied_by_earlier_attempt"] = True
        return "processed", {"applied": applied, **note}, [reply]
    return handler


def crash_wrapper(client: TownClient, state_dir: str, lease: float, inner):
    """FAULT=crash_after_claim: stall past the lease on the first claim, prove
    the stale fence is refused, exit; the runner restarts the seller."""
    marker = os.path.join(state_dir, "crashed-once")

    def handler(claim: dict[str, Any]):
        if not os.path.exists(marker):
            with open(marker, "w") as f:
                f.write(claim["fence"])
            time.sleep(lease + 0.5)
            try:
                client.ack(claim["message_id"], claim["fence"], "processed",
                           {"applied": True, "after_crash_stall": True})
            except StaleFenceError:
                sys.exit(CRASH_EXIT_CODE)
            sys.exit(9)
        return inner(claim)
    return handler


def amnesia_wrapper(client: TownClient, state_dir: str, inner):
    """FAULT=crash_amnesia: answer, then die before acknowledging. The restart
    has no memory (this adapter never had a journal) and asks Cerata again."""
    marker = os.path.join(state_dir, "amnesia-once")

    def handler(claim: dict[str, Any]):
        if os.path.exists(marker):
            return inner(claim)
        with open(marker, "w") as f:
            f.write(claim["fence"])
        _, _, replies = inner(claim)
        for reply in replies:
            client.send(**reply)
        sys.exit(CRASH_EXIT_CODE)
    return handler


def main() -> int:
    env = os.environ
    api_key = env.get("CERATA_API_KEY", "")
    if not api_key:
        print("set CERATA_API_KEY (a Cerata test key)", file=sys.stderr)
        return 2
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    post = http_post(env.get("CERATA_URL", DEFAULT_URL))
    handler = build_handler(post, api_key, env["RUN_ID"], env.get("CERATA_ADAPTER_NAIVE") == "1")
    fault = env.get("FAULT", "none")
    if fault == "crash_after_claim":
        lease = float(client.run_context.get("lease_seconds", 5.0))
        handler = crash_wrapper(client, env["STATE_DIR"], lease, handler)
    elif fault in ("crash_amnesia", "crash_amnesia_fresh_ids"):
        handler = amnesia_wrapper(client, env["STATE_DIR"], handler)
    deadline = time.time() + float(env.get("DEADLINE", "45"))
    run_loop(client, handler, until=lambda: time.time() > deadline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
