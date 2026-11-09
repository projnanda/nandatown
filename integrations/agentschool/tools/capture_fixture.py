# SPDX-License-Identifier: Apache-2.0
"""Capture a running AgentSchool into the offline fixture Town's tests read.

Run this against an AgentSchool server that has finished a training run, with
each certified agent's own service running so it can sign Town's nonce::

    python integrations/agentschool/tools/capture_fixture.py \\
        --server http://127.0.0.1:8765 \\
        --token "$AGENTSCHOOL_TOKEN" \\
        --roster /path/to/roster.json \\
        --agentschool-commit "$(git -C /path/to/agentschool rev-parse HEAD)" \\
        --out integrations/agentschool/fixtures/directory.json

The roster is a JSON object naming the agents to capture::

    {"atlas":  {"agent_id": "agt_...", "endpoint": "http://127.0.0.1:8101"},
     "brisk":  {"agent_id": "agt_..."},
     "flash":  {"agent_id": "agt_..."}}

For each one the capture records the credential AgentSchool returns, the nonce
AgentSchool minted, the signature that agent's own service produced over it,
and AgentSchool's own verdict on the presentation. It never signs anything
itself, so an agent with no reachable service is recorded without a proof
rather than with one Town invented.

What the capture cannot carry forward is stated in the fixture itself: the
recorded nonce proves nothing about whether the agent is answering later, and
the revocation list is stale within five minutes of being written.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

SCHEMA = "nandatown-agentschool-fixture/1"


def _post(client: httpx.Client, base: str, path: str, token: str,
          body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(f"{base}{path}", json=body,
                           headers={"Authorization": f"Bearer {token}"})
    payload = response.json()
    if response.status_code != 200:
        raise SystemExit(f"POST {path} -> {response.status_code}: {payload}")
    return payload


def _town_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def capture(server: str, token: str, roster: dict[str, Any], *, skill: str,
            min_level: int, audience: str,
            agentschool_commit: str | None) -> dict[str, Any]:
    base = server.rstrip("/") + "/api"
    client = httpx.Client(timeout=30)

    jwks = client.get(f"{base}/.well-known/jwks.json").raise_for_status().json()
    revocations = client.get(f"{base}/revocations").raise_for_status().json()
    status = client.get(f"{base}/status").raise_for_status().json()
    revocation_captured_at = time.time()

    certified = _post(client, base, "/search_certified_agents", token,
                      {"skill": skill, "min_level": min_level})
    certified_ids = {e["agent_id"] for e in certified
                     if isinstance(e, dict) and isinstance(
                         e.get("agent_id"), str)}

    agents: list[dict[str, Any]] = []
    for name in sorted(roster):
        entry = roster[name]
        entry = entry if isinstance(entry, dict) else {"agent_id": entry}
        agent_id = entry.get("agent_id")
        if not isinstance(agent_id, str):
            raise SystemExit(f"roster entry {name!r} has no agent_id")
        record: dict[str, Any] = {"name": name, "agent_id": agent_id,
                                  "skill": skill}

        offered = _post(client, base, "/verify_credential", token,
                        {"agent_id": agent_id, "skill": skill,
                         "min_level": 1, "audience": audience})
        token_jwt = offered.get("credential_jwt")
        claims = offered.get("claims") or {}
        record["credential_jwt"] = token_jwt
        record["level"] = claims.get("level")
        record["score"] = claims.get("score")
        record["exam_version"] = claims.get("exam_version")
        record["exam_session"] = claims.get("exam_session")
        record["credential_jti"] = claims.get("jti")
        record["operator"] = claims.get("operator")
        record["agentschool_valid_at_capture"] = offered.get("valid")
        record["agentschool_reasons_at_capture"] = offered.get("reasons")
        record["credential_status"] = (
            "valid" if offered.get("valid") is True
            else (offered.get("reasons") or ["unknown"])[0])
        if not token_jwt:
            record["absent_reason"] = (offered.get("reasons")
                                       or ["no_credential"])[0]
            record["in_certified_search"] = agent_id in certified_ids
            agents.append(record)
            print(f"{name}: no credential"
                  f" ({record['absent_reason']})")
            continue

        nonce = offered.get("nonce")
        endpoint = entry.get("endpoint")
        signature = None
        signer = "unreachable"
        if isinstance(endpoint, str) and isinstance(nonce, str):
            try:
                challenge = client.post(
                    endpoint.rstrip("/") + "/challenge",
                    json={"nonce": nonce, "audience": audience})
                if challenge.status_code == 200:
                    signature = challenge.json().get("signature")
                    signer = "agent_service"
                else:
                    signer = f"challenge_http_{challenge.status_code}"
            except Exception as exc:  # noqa: BLE001
                signer = f"challenge_error_{type(exc).__name__}"
                print(f"  {name}: /challenge failed: {exc}", file=sys.stderr)

        if isinstance(signature, str):
            record["recorded_proof"] = {"nonce": nonce, "audience": audience,
                                        "signature": signature,
                                        "signer": signer,
                                        "captured_at": time.time()}
            record["agentschool_verdict"] = _post(
                client, base, "/check_identity_proof", token,
                {"credential_jwt": token_jwt, "nonce": nonce,
                 "signature": signature, "audience": audience})
        else:
            record["recorded_proof"] = None
            record["proof_absent_reason"] = signer
        record["in_certified_search"] = agent_id in certified_ids
        verdict = record.get("agentschool_verdict") or {}
        print(f"{name}: level {record['level']} score {record['score']}"
              f" status {record['credential_status']}"
              f" proof {signer} verified {verdict.get('verified')}")
        agents.append(record)

    return {
        "schema": SCHEMA,
        "captured_at": time.time(),
        "captured_by": "integrations/agentschool/tools/capture_fixture.py",
        "nandatown_commit": _town_commit(),
        "agentschool": {
            "commit": agentschool_commit,
            "server": server,
            "issuer_kid": status.get("issuer_kid"),
            "skill": skill,
            "skill_version": next(
                (a.get("skill_version") for a in agents
                 if a.get("skill_version")), "2.0.0"),
            "course": status.get("course"),
        },
        "jwks": jwks,
        "revocations_jwt": revocations["jwt"],
        "revocation_captured_at": revocation_captured_at,
        "revocation_max_age_seconds": revocations.get("max_age_seconds"),
        "certified_search_result": certified,
        "agents": agents,
        "limitations": [
            "The recorded nonce was minted at capture time, so replaying this"
            " proof shows the holder key signed that nonce once and cannot"
            " show the agent is answering now.",
            "The revocation list is stale beyond its own max_age_seconds, so"
            " an offline run reads it with the freshness limit lifted and"
            " reports freshness as untested.",
            "Credential scores are AgentSchool's own exam results. Nothing"
            " here establishes that the exam predicts performance on"
            " delegated work.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--token", required=True,
                        help="bearer token of an agent registered with"
                             " AgentSchool, used to call its tools")
    parser.add_argument("--roster", required=True,
                        help="JSON file of name -> {agent_id, endpoint}")
    parser.add_argument("--out", required=True)
    parser.add_argument("--skill", default="data-driven-slides")
    parser.add_argument("--min-level", type=int, default=1)
    parser.add_argument("--audience", default="nandatown")
    parser.add_argument("--agentschool-commit", default=None)
    args = parser.parse_args(argv)

    roster = json.loads(Path(args.roster).read_text())
    if not isinstance(roster, dict):
        raise SystemExit("the roster must be a JSON object")
    fixture = capture(args.server, args.token, roster, skill=args.skill,
                      min_level=args.min_level, audience=args.audience,
                      agentschool_commit=args.agentschool_commit)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
