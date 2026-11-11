"""Minimal standard-library client for the Locard service API.

Locard runs as its own service (https://github.com/zaccytoymas/locard-mvp,
`locard/`), not inside Town. This module is the only place the integration
talks to it:

  obtain_cover()  POST /v1/submissions -> POST /v1/quotes -> POST /v1/quotes/{id}/accept
  report_event()  POST /v1/policies/{id}/events   (live monitoring; a violation suspends)
  verify()        POST /v1/verify                 (what a counterparty calls at the door)

Any network failure raises LocardUnavailable, so callers can fail closed.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEFAULT_URL = "http://127.0.0.1:8700"


class LocardUnavailable(Exception):
    """Locard could not be reached or did not answer in time."""


class LocardRefused(Exception):
    """Locard answered with an error code (e.g. 409 agent_has_suspended_policy)."""

    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(f"{status} {body.get('error')}: {body.get('detail')}")
        self.status, self.body = status, body


def _call(base_url: str, method: str, path: str, body: Any = None,
          headers: dict[str, str] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {"error": "http_error", "detail": str(exc)}
        raise LocardRefused(exc.code, payload) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise LocardUnavailable(str(exc)) from None


def fingerprint(manifest: dict[str, Any], git_commit: str | None = None) -> str:
    """Locard's version fingerprint (locard-mvp INTERFACE_SPEC 2.3)."""
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload = canonical + str(manifest.get("model", "")) + (git_commit or "none")
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_packet(agent_id: str | None = None, manifest_path: Path | None = None,
                traces_path: Path | None = None) -> dict[str, Any]:
    """The underwriting packet: manifest, fingerprint and trace history.

    Defaults to the bundled fixture (the Locard MVP's procurement bot, 150 of its
    synthetic trace events). `agent_id` renames the agent so repeated runs against
    one Locard database don't collide with an earlier suspension.
    """
    manifest = json.loads((manifest_path or FIXTURES / "procurement_bot_manifest.json").read_text())
    if agent_id:
        manifest["agent_id"] = agent_id
    lines = (traces_path or FIXTURES / "procurement_bot_traces.jsonl").read_text().splitlines()
    traces = [json.loads(line) for line in lines if line.strip()]
    return {"manifest": manifest, "fingerprint": fingerprint(manifest), "traces": traces}


def obtain_cover(base_url: str, packet: dict[str, Any]) -> dict[str, Any]:
    """Underwrite, quote and bind. Returns the policy plus what Locard decided on the way."""
    sub = _call(base_url, "POST", "/v1/submissions", packet)
    quote = _call(base_url, "POST", "/v1/quotes", {"submission_id": sub["submission_id"]})
    bound = _call(base_url, "POST", f"/v1/quotes/{quote['quote_id']}/accept", {
        "payment": {"method": "fake_usd", "amount_cents": quote["premium_annual_cents"]},
        "accept_monitoring": True,
    })
    return {
        "agent_id": packet["manifest"]["agent_id"],
        "submission_id": sub["submission_id"],
        "risk_mean_score": sub["risk"]["mean_score"],
        "quote_id": quote["quote_id"],
        "premium_annual_cents": quote["premium_annual_cents"],
        "spend_cap_per_tx_cents": quote["coverage"].get("spend_cap_per_tx_cents"),
        "policy_id": bound["policy_id"],
        "policy_status": bound["status"],
        "policy_token": bound["policy_token"],
        "monitoring": bound["monitoring"],
    }


def report_event(base_url: str, cover: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Stream one trace event to Locard's live monitoring."""
    return _call(base_url, "POST", f"/v1/policies/{cover['policy_id']}/events", event,
                 headers={"Authorization": f"Bearer {cover['monitoring']['api_key']}"})


def verify(base_url: str, policy_token: str, agent_id: str,
           amount_cents: int) -> dict[str, Any]:
    """Ask Locard whether this policy covers this transaction, right now."""
    return _call(base_url, "POST", "/v1/verify", {
        "policy_token": policy_token, "agent_id": agent_id,
        "transaction_amount_cents": amount_cents,
    })
