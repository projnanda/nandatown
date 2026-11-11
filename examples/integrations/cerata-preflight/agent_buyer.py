#!/usr/bin/env python3
"""An agent that asks Cerata before it acts, and checks the answer itself.

It plays the buyer role in a Track run. CERATA_CASE names one case in
cases.json: an action a sales, marketing, leasing or recruiting agent is about
to take, with the answer Cerata must give. The
buyer finds the seller through the town, sends the action as a quote_request
and judges the quote_response against that expectation:

- exact cases: the decision Cerata must give;
- never-clear cases: block, flag or hold, and never a clearance token;
- every signed receipt is verified directly with Cerata's public
  POST /v1/verify, a channel that goes through neither the seller nor Town;
- a clearance token, when the case expects one, is consumed once and must
  then be refused on a second presentation;
- a repeated case must get the same decision digest as the case it repeats.

CERATA_EXPECT=unavailable is the failure case: Cerata is unreachable and the
only correct answer is no decision and no token.

The verdict is the buyer's own assertion, recorded on its acknowledgement as
`correct`; Town records it and does not compute it. CERATA_OBS_DIR, when set,
also gets one JSON observation per case for check.sh.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from nandatown.client import TownClient
from nandatown.participants.base import run_loop
from nandatown.participants.buyer import find_seller

EXIT_CORRECT, EXIT_INCORRECT, EXIT_NO_RESPONSE = 0, 4, 5
HERE = os.path.dirname(os.path.abspath(__file__))

Verify = Callable[[dict[str, Any], bool], dict[str, Any]]


def load_case(case_id: str, path: str | None = None) -> dict[str, Any]:
    with open(path or os.path.join(HERE, "cases.json")) as f:
        cases = json.load(f)["cases"]
    for case in cases:
        if case["id"] == case_id:
            return case
    raise SystemExit(f"unknown case {case_id!r}")


def http_verify(url: str, timeout: float = 15.0) -> Verify:
    """POST /v1/verify, no API key: anyone can check a receipt or token."""
    def call(body: dict[str, Any], consume: bool) -> dict[str, Any]:
        path = "/v1/verify" + ("?consume=true" if consume else "")
        req = urllib.request.Request(
            url.rstrip("/") + path, data=json.dumps(body).encode(),
            method="POST", headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return {"valid": False, "http_status": exc.code}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"valid": False, "error": type(exc).__name__}
    return call


def judge(body: dict[str, Any], case: dict[str, Any], verify: Verify,
          expect_unavailable: bool = False,
          prior: dict[str, Any] | None = None) -> dict[str, Any]:
    """The buyer's note for one quote_response body."""
    if expect_unavailable:
        checks = {"no_decision": body.get("status") == "unavailable" and "decision" not in body,
                  "no_token": not body.get("token")}
        return {"correct": all(checks.values()), "case": case["id"], "expect": "unavailable",
                "status": body.get("status"), "checks": checks}
    if body.get("status") != "decided":
        return {"correct": False, "case": case["id"], "status": body.get("status"),
                "reason": "no decision from Cerata", "cerata_status": body.get("cerata_status")}

    expect = case["expect"]
    decision = body.get("decision")
    checks: dict[str, bool] = {}
    if expect.get("never_clear"):
        checks["not_clear"] = decision in ("block", "hold", "flag")
        checks["no_clearance_token"] = not body.get("token")
    else:
        checks["decision"] = decision == expect["decision"]
    # The receipt, checked by Cerata's public verifier, not by the seller.
    receipt_check: dict[str, Any] = {}
    if body.get("receipt"):
        v = verify({"receipt": body["receipt"]}, False)
        checks["receipt_verifies"] = v.get("valid") is True
        receipt_check = {"kind": v.get("kind"), "kid": v.get("kid"), "reason": v.get("reason")}
    elif decision != "hold":
        checks["receipt_present"] = False
    else:
        checks["hold_reported"] = isinstance(body.get("hold"), dict) or bool(body.get("state"))

    # A clearance token is good for one action: first use consumed, second refused.
    token_check: dict[str, Any] = {}
    if expect.get("token"):
        checks["token_present"] = bool(body.get("token"))
    if body.get("token") and not expect.get("never_clear"):
        # The audience the agent asked the token for; the claims are a fallback.
        aud = (case["request"].get("token") or {}).get("aud")
        if not aud:
            claims = verify({"token": body["token"]}, False).get("claims") or {}
            aud = claims.get("aud") if isinstance(claims, dict) else None
        if expect.get("token"):
            # Never let the single-use check pass by not running.
            checks["token_audience_known"] = bool(aud)
        if aud:
            first = verify({"token": body["token"], "aud": aud}, True)
            second = verify({"token": body["token"], "aud": aud}, True)
            checks["token_consumed_once"] = first.get("valid") is True
            checks["token_reuse_refused"] = second.get("valid") is False
            token_check = {"aud": aud, "first": first.get("reason") or "consumed",
                           "second": second.get("reason")}

    if expect.get("same_digest_as"):
        checks["same_decision_digest"] = bool(prior) and prior.get("decision_digest") == body.get("decision_digest")

    return {"correct": all(checks.values()), "case": case["id"], "decision": decision,
            "decision_digest": body.get("decision_digest"),
            "receipt_id": body.get("receipt_id"), "checks": checks,
            "receipt_check": receipt_check, "token_check": token_check}


def main() -> int:
    env = os.environ
    case = load_case(env["CERATA_CASE"], env.get("CERATA_CASES"))
    expect_unavailable = env.get("CERATA_EXPECT") == "unavailable"
    verify = http_verify(env.get("CERATA_VERIFY_URL") or env.get("CERATA_URL") or
                         "https://clearance-kohl.vercel.app")
    obs_dir = env.get("CERATA_OBS_DIR")
    prior = None
    same_as = case.get("expect", {}).get("same_digest_as")
    if obs_dir and same_as:
        try:
            with open(os.path.join(obs_dir, same_as + ".json")) as f:
                prior = json.load(f)
        except OSError:
            prior = None

    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    deadline = time.time() + float(env.get("DEADLINE", "40"))
    seller = None
    while seller is None and time.time() < deadline:
        seller = find_seller(client)
        if seller is None:
            time.sleep(0.3)
    if seller is None:
        return EXIT_NO_RESPONSE
    client.send(message_id="q-1", to=seller, kind="quote_request",
                body={"sku": "cerata-preflight", "quantity": 1, "case": case["id"],
                      "agent": case["agent"], "preflight": case["request"]})

    outcome: dict[str, Any] = {}
    seen: set[str] = set()

    def handler(claim: dict[str, Any]):
        if claim["kind"] != "quote_response":
            return "rejected", {"reason": "unknown kind"}, []
        if claim["message_id"] in seen:
            return "processed", {"duplicate": True}, []
        note = judge(claim["body"], case, verify, expect_unavailable, prior)
        seen.add(claim["message_id"])
        outcome.update(note)
        print(json.dumps(note, indent=1), file=sys.stderr)
        if obs_dir:
            os.makedirs(obs_dir, exist_ok=True)
            with open(os.path.join(obs_dir, case["id"] + ".json"), "w") as f:
                json.dump(note, f, indent=1)
        return "processed", note, []

    run_loop(client, handler, until=lambda: "correct" in outcome or time.time() > deadline)
    if "correct" not in outcome:
        return EXIT_NO_RESPONSE
    return EXIT_CORRECT if outcome["correct"] else EXIT_INCORRECT


if __name__ == "__main__":
    sys.exit(main())
