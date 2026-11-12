#!/usr/bin/env python3
"""A seller that consults an external safety service before it acts.

Proof that a town participant can depend on a third service and still
hold the protocol: this seller calls the Agentic Graveyard, an MCP
server of recorded agent failures, before it applies a quote request,
and records the guard it was given in its acknowledgement note.

The point is not that the quote changes. It does not. The point is what
happens to custody when a participant has an outside dependency:

  * the check runs once per message_id, not once per delivery, so a
    duplicated delivery replays the stored response without paying for
    a second lookup inside the same lease;
  * when the service is unreachable, the policy decides. Fail closed
    acknowledges `retryable` and sends no response, so the town sees a
    refusal rather than an unguarded answer. Fail open answers and
    marks the note degraded.

Run it through the town:

    nandatown test-agent --role seller --cmd "python examples/graveyard_seller.py"

Environment:

    GRAVEYARD_URL      MCP endpoint (default: the public deployment)
    GRAVEYARD_POLICY   fail-closed (default) or fail-open
    GRAVEYARD_TIMEOUT  seconds for one lookup (default: 3.0)

Point GRAVEYARD_URL at an address that refuses connections to exercise
the unreachable path without touching the network.

Standard library only, like examples/byoa_seller.py, so it runs with no
model credentials and no third-party package. Token runs only.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

TOWN = os.environ["TOWN_URL"]
RUN = os.environ["RUN_ID"]
NAME = os.environ["NAME"]
TOKEN = os.environ["TOKEN"]
DEADLINE = time.time() + float(os.environ.get("DEADLINE", "45"))

GRAVEYARD_URL = os.environ.get(
    "GRAVEYARD_URL", "https://agentic-graveyard.vercel.app/api/mcp")
GRAVEYARD_POLICY = os.environ.get("GRAVEYARD_POLICY", "fail-closed")
GRAVEYARD_TIMEOUT = float(os.environ.get("GRAVEYARD_TIMEOUT", "3.0"))

MCP_PROTOCOL_VERSION = "2025-06-18"

session = None
processed = {}
checked = {}


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


def lost_lease(method, path, body=None):
    """Post a reply or acknowledgement that the town may already refuse.

    A lease can expire while an outside dependency is still being waited
    on. The town then rejects the stale fence, which is correct: custody
    moved on. Returns False so the caller drops the attempt and waits for
    redelivery instead of exiting with an unhandled error, which the town
    can only read as a crashed participant.
    """
    try:
        call(method, path, body)
        return True
    except urllib.error.HTTPError:
        return False


def mcp(payload, timeout):
    """One JSON-RPC call to an MCP endpoint over HTTP.

    Accepts a single JSON object or an SSE framing of one, which is what
    a streamable-HTTP MCP server returns.
    """
    req = urllib.request.Request(
        GRAVEYARD_URL, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            decoded = json.loads(line)
            if "result" in decoded or "error" in decoded:
                return decoded
    raise ValueError("no JSON-RPC object in MCP response")


def handshake():
    """Initialize once, outside any lease, so a lookup is one round trip."""
    try:
        reply = mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": MCP_PROTOCOL_VERSION,
                                "capabilities": {},
                                "clientInfo": {
                                    "name": "nandatown-graveyard-seller",
                                    "version": "1"}}},
                    GRAVEYARD_TIMEOUT)
        server = (reply.get("result") or {}).get("serverInfo") or {}
        return {"reachable": True, "server": server.get("name"),
                "server_version": server.get("version")}
    except Exception as exc:
        return {"reachable": False, "error": type(exc).__name__}


def premortem(task):
    """Ask the graveyard about this task. Never raises.

    Returns the matched tags, the first grave's id and guard, and the
    measured latency, or a status saying why there is none.
    """
    started = time.monotonic()
    try:
        reply = mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "graveyard_search",
                                "arguments": {"task": task}}},
                    GRAVEYARD_TIMEOUT)
    except Exception as exc:
        return {"status": "unreachable", "error": type(exc).__name__,
                "graves": None, "abstained": None,
                "latency_ms": round((time.monotonic() - started) * 1000)}
    latency_ms = round((time.monotonic() - started) * 1000)
    result = reply.get("result") or {}
    text = "\n".join(block.get("text", "")
                     for block in result.get("content") or [])
    if reply.get("error") or result.get("isError"):
        return {"status": "error", "graves": None, "abstained": None,
                "latency_ms": latency_ms}
    tags = ""
    if "match (" in text:
        tags = text.split("match (", 1)[1].split(")", 1)[0]
    # The service either abstains outright or returns a ranked list. Both
    # are useful answers; record which one, so "consulted" is never
    # mistaken for "warned".
    graves = 0
    if "past agent failures match" in text:
        head = text.split("past agent failures match", 1)[0].split()
        graves = int(head[-1]) if head and head[-1].isdigit() else -1
    grave_id, guard = "", ""
    for line in text.splitlines():
        stripped = line.strip()
        if not grave_id and "(g_" in stripped:
            grave_id = "g_" + stripped.rsplit("(g_", 1)[1].rstrip(")")
        if not guard and stripped.startswith("guard ("):
            guard = stripped
    return {"status": "consulted", "latency_ms": latency_ms,
            "graves": graves, "abstained": graves == 0,
            "matched_tags": "" if tags == "-" else tags,
            "grave_id": grave_id, "guard": guard[:240]}


def describe(body):
    return (f"respond to a quote request for {body.get('quantity')} x "
            f"{body.get('sku')} at {body.get('unit_price_cents')} cents "
            f"as an autonomous selling agent in a marketplace")


def main():
    global session
    joined = call("POST", "/join", {"name": NAME, "token": TOKEN})
    session = joined["session"]
    health = handshake()

    while time.time() < DEADLINE:
        call("GET", "/inbox/notify?wait=0.4")
        claim = call("POST", "/inbox/claim")
        if claim is None:
            continue
        mid = claim["message_id"]
        if claim["kind"] != "quote_request":
            lost_lease("POST", "/inbox/ack",
                       {"message_id": mid, "fence": claim["fence"],
                        "status": "rejected",
                        "note": {"reason": "unknown kind"}})
            continue
        if mid in processed:
            # A duplicate delivery replays the stored response. The
            # pre-mortem is not repeated: the decision belongs to the
            # message, not to the delivery.
            lost_lease("POST", "/messages", processed[mid])
            lost_lease("POST", "/inbox/ack",
                       {"message_id": mid, "fence": claim["fence"],
                        "status": "processed",
                        "note": {"duplicate": True,
                                 "graveyard": checked.get(mid, {})}})
            continue
        body = claim["body"]
        check = premortem(describe(body))
        checked[mid] = check
        if check["status"] != "consulted" and GRAVEYARD_POLICY == "fail-closed":
            # The guard service is the reason this seller is allowed to
            # act. Without it, refuse and let the town redeliver.
            if not lost_lease("POST", "/inbox/ack",
                              {"message_id": mid, "fence": claim["fence"],
                               "status": "retryable",
                               "note": {"applied": False,
                                        "reason": "pre-mortem unavailable,"
                                                  " failing closed",
                                        "policy": GRAVEYARD_POLICY,
                                        "graveyard": check}}):
                # The lookup outlasted the lease, so even the refusal is
                # stale. Keep the loop alive for the redelivery.
                pass
            continue
        total = body["quantity"] * body["unit_price_cents"]
        reply = {"message_id": "r-" + mid.removeprefix("q-"),
                 "to": claim["from"], "kind": "quote_response",
                 "body": {"request_id": mid, "total_cents": total}}
        if not lost_lease("POST", "/messages", reply):
            continue
        processed[mid] = reply
        note = {"applied": True, "total_cents": total,
                "runtime": "graveyard-mcp-stdlib",
                "policy": GRAVEYARD_POLICY,
                "graveyard": check, "graveyard_health": health}
        if check["status"] != "consulted":
            note["degraded"] = True
        lost_lease("POST", "/inbox/ack",
                   {"message_id": mid, "fence": claim["fence"],
                    "status": "processed", "note": note})
    return 0


if __name__ == "__main__":
    sys.exit(main())
