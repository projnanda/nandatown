"""A small client for JANG's agent-facing REST API (src/lib/client.ts upstream).

Every call raises ``JangRefusal`` for a JANG error envelope
(``{"error": {"code", "message", "violations"}}``) and ``JangUnreachable``
for transport failures, so a Lab role can turn either into an attributed
event instead of crashing the deterministic engine.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:3939"


class JangUnreachable(Exception):
    pass


class JangRefusal(Exception):
    def __init__(self, status: int, code: str, message: str,
                 violations: list[dict[str, Any]] | None = None):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.violations = violations or []

    def public_detail(self) -> dict[str, Any]:
        """What may be recorded in a Town bundle: no limits, no offered values."""
        return {"code": self.code, "status": self.status,
                "dimensions": sorted({v.get("dimension", "?") for v in self.violations})}


class JangClient:
    def __init__(self, base_url: str = DEFAULT_URL, api_key: str | None = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None, auth: bool = True) -> Any:
        headers = {}
        if auth and self.api_key:
            headers["x-agent-key"] = self.api_key
        try:
            resp = httpx.request(method, self.base_url + path, json=body,
                                 headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise JangUnreachable(type(exc).__name__) from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if resp.status_code >= 400:
            err = (payload or {}).get("error") if isinstance(payload, dict) else None
            if not isinstance(err, dict):
                raise JangUnreachable(f"HTTP {resp.status_code} without a JANG error envelope")
            raise JangRefusal(resp.status_code, err.get("code", "UNKNOWN"),
                              err.get("message", ""), err.get("violations"))
        if not isinstance(payload, dict):
            raise JangUnreachable("non-JSON response")
        return payload

    # -- public routes
    def health(self) -> dict[str, Any]:
        return self._call("GET", "/api/health", auth=False)

    def register_agent(self, agent_id: str, owner_id: str) -> str:
        out = self._call("POST", "/api/agents", {"agentId": agent_id, "ownerId": owner_id},
                         auth=False)
        return out["apiKey"]

    def get_agreement(self, agreement_id: str) -> dict[str, Any]:
        return self._call("GET", f"/api/agreements/{agreement_id}", auth=False)

    # -- agent routes
    def lodge_mandate(self, mandate: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/api/mandates", mandate)

    def open_session(self, counterparty: str, item_spec: dict[str, Any],
                     max_rounds: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"counterpartyAgentId": counterparty, "itemSpec": item_spec}
        if max_rounds is not None:
            body["maxRounds"] = max_rounds
        return self._call("POST", "/api/sessions", body)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._call("GET", f"/api/sessions/{session_id}")

    def offer(self, session_id: str, terms: dict[str, Any], note: str = "",
              alternatives: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"terms": terms}
        if note:
            body["note"] = note[:500]
        if alternatives:
            body["alternatives"] = alternatives
        return self._call("POST", f"/api/sessions/{session_id}/offers", body)

    def accept(self, session_id: str, offer_id: str | None = None) -> dict[str, Any]:
        body = {"offerId": offer_id} if offer_id else {}
        return self._call("POST", f"/api/sessions/{session_id}/accept", body)
