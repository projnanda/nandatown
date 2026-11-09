#!/usr/bin/env python3
"""Exercise a cited synthetic claim handoff over Town and the live record's MCP interface.

Two Town participants join separate mailbox sessions. They use distinct scoped
record bearers and relay the claim and verification receipt through Town.
Only the case verdict and an allowlisted summary of the Town events leave the run.
"""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
from threading import Thread
import time

import httpx
import uvicorn

from nandatown.client import TownClient
from nandatown.coordinator import build_app
from nandatown.profiles import PROFILES
from nandatown.records import fingerprint


TOWN_BASE = "6f9a7c064486c4a0ae9242f1ff341b8a4aad0244"
EXPECTED = {
    "faithful": {"result": "matches_record", "problems": []},
    "altered": {"result": "differs_from_record", "problems": ["value_mismatch"]},
}
SENDER = "brief-sender"
VERIFIER = "claim-verifier"
CLAIM = "synthetic-claim"
RECEIPT = "verification-receipt"
TIMEOUT = 10


class ScenarioFailure(ValueError):
    """A public diagnostic with no record content or request settings."""


def require(condition, message):
    """Fail the run without including response bodies or credentials."""
    if not condition:
        raise ScenarioFailure(message)


@contextmanager
def coordinator(db_path):
    """Run Town's unmodified HTTP coordinator on a disposable loopback socket."""
    admin = secrets.token_urlsafe(24)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(
            build_app(str(db_path), admin), log_level="critical", access_log=False,
        ))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + TIMEOUT
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            require(server.started, "Town coordinator did not start")
            with httpx.Client(base_url=url, timeout=TIMEOUT, trust_env=False) as http:
                yield http, {"X-Town-Admin": admin}
        finally:
            server.should_exit = True
            thread.join(timeout=TIMEOUT)


def record_tool(http, url, bearer, name, arguments):
    """Call an MCP tool and accept only a correlated, successful JSON-RPC result."""
    request_id = secrets.token_hex(16)
    response = http.post(url, headers={
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }, json={
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    require(response.status_code == 200, f"{name}: record HTTP request failed")
    rpc = response.json()
    require(rpc.get("jsonrpc") == "2.0" and rpc.get("id") == request_id,
            f"{name}: record RPC response did not correlate")
    tool_result = rpc.get("result", {})
    content = tool_result.get("content", [])
    require(not tool_result.get("isError") and len(content) == 1
            and content[0].get("type") == "text", f"{name}: record tool failed")
    body = json.loads(content[0]["text"])
    require(body.get("ok") is True and isinstance(body.get("result"), dict),
            f"{name}: record did not return a usable result")
    return body["result"]


def claim_handoff(http, admin, record, url, tokens, person, case):
    """Publish the real brief claim, verify what arrived, and return a Town receipt."""
    # The coordinator currently requires quote task metadata. Only its mailbox
    # is used here, so the quote task and its evaluator do not run.
    profile = PROFILES["quote-clean"].model_dump()
    profile.update(name="synapse-handoff", roles={SENDER: SENDER, VERIFIER: VERIFIER},
                   capabilities={SENDER: ["brief.read"], VERIFIER: ["claim.check"]},
                   lease_seconds=30)
    created = http.post("/runs", headers=admin, json={"profile": profile})
    created.raise_for_status()
    run = created.json()
    run_id = run["run_id"]
    sender = TownClient(str(http.base_url), run_id, http)
    verifier = TownClient(str(http.base_url), run_id, http)
    sender.join(SENDER, run["join_tokens"][SENDER])
    verifier.join(VERIFIER, run["join_tokens"][VERIFIER])
    require(sender.session != verifier.session, "Town did not issue distinct sessions")
    require(any(peer["name"] == VERIFIER and "claim.check" in peer["capabilities"]
                for peer in sender.participants()), "Town did not list the verifier capability")

    scope = {"person_id": person, "purpose": "shift_handoff"}
    brief = record_tool(record, url, tokens[0], "request_brief", {**scope, "item_kinds": ["mobility"]})
    claims = brief.get("claims", [])
    require(len(claims) == 1 and claims[0].get("item_kind") == "mobility"
            and claims[0].get("entries"), "scoped brief lacked a cited mobility claim")

    claim = deepcopy(claims[0])
    if case == "altered":
        claim["entries"][0]["untrusted"]["value"] += " [synthetic alteration]"
    submitted = {"claim": claim}
    sender.send(CLAIM, VERIFIER, "record.claim", submitted)
    received = verifier.claim()
    require(received and received["message_id"] == CLAIM and received["from"] == SENDER
            and received["kind"] == "record.claim" and received["body"] == submitted,
            "verifier did not receive the claim sent through Town")

    checked = record_tool(record, url, tokens[1], "check_claims",
                          {**scope, "claims": [received["body"]["claim"]]})
    checks = checked.get("checks", [])
    require(len(checks) == 1 and checks[0].get("item_kind") == "mobility",
            "record did not verify one mobility claim")
    observed = {"result": checks[0].get("result"), "problems": checks[0].get("problems")}
    require(observed == EXPECTED[case], f"{case}: unexpected verification verdict")

    verifier.ack(CLAIM, received["fence"], "processed")
    receipt = {"request_id": CLAIM, "claim_fingerprint": fingerprint(received["body"]), **observed}
    verifier.send(RECEIPT, SENDER, "record.verification", receipt)
    reply = sender.claim()
    require(reply and reply["message_id"] == RECEIPT and reply["from"] == VERIFIER
            and reply["kind"] == "record.verification" and reply["body"] == receipt,
            "sender did not receive the verification receipt")
    sender.ack(RECEIPT, reply["fence"], "processed")

    http.post(f"/runs/{run_id}/finish", headers=admin).raise_for_status()
    exported = http.get(f"/runs/{run_id}/events", headers=admin)
    exported.raise_for_status()
    events = exported.json()["events"]
    evidence = {
        "case": case,
        "run_id": run_id,
        "original_fingerprint": fingerprint({"claim": claims[0]}),
        "receipt": receipt,
        "mailbox": mailbox_trace(events),
    }
    verify_case(evidence)
    return evidence


def mailbox_trace(events):
    """Keep protocol evidence without claim payloads, leases, tokens, or identities."""
    keep = {
        "message_accepted": ("sender", "to", "kind", "content_fingerprint"),
        "message_claimed": ("claimant",),
        "ack_recorded": ("status",),
    }
    return [
        {"kind": event["kind"], "subject": event["subject"], "observer": event["observer"],
         "detail": {key: event["detail"][key] for key in keep[event["kind"]]}}
        for event in events
        if event["kind"] in keep and event["subject"] in {CLAIM, RECEIPT}
    ]


def verify_case(case):
    """Check the published trace's sequencing, fingerprints, and expected verdict."""
    name = case["case"]
    receipt = case["receipt"]
    actual_fingerprint = receipt["claim_fingerprint"]
    require(receipt == {"request_id": CLAIM, "claim_fingerprint": actual_fingerprint, **EXPECTED[name]},
            "verification verdict or correlation does not match the case")
    original_fingerprint = case["original_fingerprint"]
    require(all(re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                for digest in (original_fingerprint, actual_fingerprint)), "invalid claim fingerprint")
    require((original_fingerprint == actual_fingerprint) == (name == "faithful"),
            "claim fingerprint does not match the case")
    expected_events = []
    for subject, sender, receiver, kind, digest in [
        (CLAIM, SENDER, VERIFIER, "record.claim", actual_fingerprint),
        (RECEIPT, VERIFIER, SENDER, "record.verification", fingerprint(receipt)),
    ]:
        expected_events.extend([
            {"kind": "message_accepted", "subject": subject, "observer": "town",
             "detail": {"sender": sender, "to": receiver, "kind": kind, "content_fingerprint": digest}},
            {"kind": "message_claimed", "subject": subject, "observer": "town",
             "detail": {"claimant": receiver}},
            {"kind": "ack_recorded", "subject": subject, "observer": receiver,
             "detail": {"status": "processed"}},
        ])
    require(case["mailbox"] == expected_events, "Town mailbox trace does not match the receipt")


def configuration():
    """Load separate scoped actor tokens and restrict the record target to a plain MCP URL."""
    url = httpx.URL(os.environ["SYNAPSE_HW8_MCP_URL"])
    loopback = url.host in {"127.0.0.1", "localhost", "::1"}
    require(url.path == "/mcp" and url.host and not (url.userinfo or url.query or url.fragment)
            and (url.scheme == "https" or (url.scheme == "http" and loopback)),
            "configure an HTTPS MCP URL or a loopback HTTP MCP URL")
    tokens = (os.environ["SYNAPSE_HW8_SENDER_TOKEN"], os.environ["SYNAPSE_HW8_VERIFIER_TOKEN"])
    require(all(tokens) and tokens[0] != tokens[1], "configure distinct, nonempty actor bearers")
    person = os.environ["SYNAPSE_HW8_PERSON_ID"]
    require(bool(person), "configure the designated synthetic person")
    return url, tokens, person, "local-synthetic" if loopback else "hosted-synthetic"


def main():
    """Run both scenarios or verify the archived trace offline."""
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--out", type=Path, help="write the allowlisted evidence JSON here")
    action.add_argument("--verify", type=Path, help="check an existing evidence JSON offline")
    parser.add_argument("--project-version", help="record version for a live run")
    args = parser.parse_args()
    if args.out and not args.project_version:
        parser.error("--out requires --project-version")
    try:
        if args.verify:
            evidence = json.loads(args.verify.read_text(encoding="utf-8"))
            require(evidence["format"] == "synapse-handoff-v1" and evidence["town_base"] == TOWN_BASE,
                    "unexpected evidence format or Town base")
            require([case["case"] for case in evidence["cases"]] == list(EXPECTED),
                    "expected both scenarios in evidence")
            for case in evidence["cases"]:
                verify_case(case)
                print(f"Verified {case['case']}: {case['receipt']['result']}")
            return 0

        require(re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", args.project_version), "invalid project version label")
        url, tokens, person, mode = configuration()
        with tempfile.TemporaryDirectory(prefix="synapse-town-") as scratch:
            with coordinator(Path(scratch) / "town.db") as (http, admin):
                with httpx.Client(timeout=TIMEOUT, trust_env=False) as record:
                    cases = [claim_handoff(http, admin, record, url, tokens, person, case) for case in EXPECTED]
        evidence = {
            "format": "synapse-handoff-v1",
            "town_base": TOWN_BASE,
            "project_version": args.project_version,
            "mode": mode,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cases": cases,
        }
        args.out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        for case in cases:
            print(f"PASS {case['case']}: {case['receipt']['result']} {case['receipt']['problems']}")
        return 0
    except ScenarioFailure as error:
        print(f"Integration failed: {error}", file=sys.stderr)
    except KeyError:
        print("Integration failed: missing configuration or required response field", file=sys.stderr)
    except (OSError, httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError, AttributeError):
        print("Integration failed: configuration, transport, or scenario validation error", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
