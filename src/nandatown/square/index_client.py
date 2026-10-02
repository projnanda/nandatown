"""NANDA Index client: agents register by email and find each other.

Speaks the nanda-index-v2 API (github.com/projnanda/nanda-index-v2) the
way a person registering an agent would: an account for the agent's
email, then a personal index record pointing at its agent card. The
index emails a verification link; following it activates a personal
record, and only active records are returned by search. A registration
that is still pending is reported as not yet discoverable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

LIVE_INDEX = "https://api.nandaindex.org"
ORG_ID = re.compile(r"[a-z0-9][a-z0-9-]*[a-z0-9]")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
A2A_CARD = "application/a2a-agent-card+json"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class NandaIndexError(Exception):
    pass


@dataclass(frozen=True)
class Registration:
    org_id: str
    status: str
    email_verified: bool
    record: dict[str, Any]

    @property
    def discoverable(self) -> bool:
        return self.status == "active"

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Registration:
        return cls(record.get("org_id", ""), record.get("status", "unknown"),
                   bool(record.get("email_verified")), record)


def _https(url: str, label: str) -> str:
    parts = urlsplit(url)
    local = parts.scheme == "http" and parts.hostname in LOCAL_HOSTS
    if not parts.hostname or (parts.scheme != "https" and not local):
        raise NandaIndexError(
            f"{label} must be an https URL (plain http only for localhost)")
    return url.rstrip("/")


class NandaIndexClient:
    def __init__(self, base_url: str = LIVE_INDEX,
                 http: httpx.Client | None = None, timeout: float = 10.0):
        self.base_url = _https(base_url, "the index URL")
        self.http = http or httpx.Client(base_url=self.base_url,
                                         timeout=timeout,
                                         follow_redirects=False)

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise NandaIndexError(
                f"{method} {path} failed: {type(exc).__name__}") from exc

    @staticmethod
    def _json(response: httpx.Response, what: str) -> dict[str, Any]:
        """The decoded body of a success, or an error naming only the
        status and the index's own error code, never the request."""
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not response.is_success:
            code = body.get("error", "") if isinstance(body, dict) else ""
            raise NandaIndexError(
                f"{what} failed: HTTP {response.status_code} {code}".rstrip())
        if not isinstance(body, dict):
            raise NandaIndexError(f"{what} returned a non-object body")
        return body

    # -- account and registration -------------------------------------

    def account_token(self, email: str, password: str) -> str:
        """A session token for the agent's email: register, or log in if
        the account already exists."""
        if not EMAIL.fullmatch(email or ""):
            raise NandaIndexError("the agent needs a valid email address")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise NandaIndexError("the password must be 8 to 128 characters")
        credentials = {"email": email, "password": password}
        response = self._call("POST", "/auth/register", json=credentials)
        if response.status_code == 409:
            response = self._call("POST", "/auth/login", json=credentials)
        token = self._json(response, "signing in").get("token")
        if not isinstance(token, str) or not token:
            raise NandaIndexError("the index returned no session token")
        return token

    def register_agent(self, token: str, *, org_id: str, display_name: str,
                       contact_email: str, card_url: str,
                       description: str = "",
                       tags: list[str] | None = None) -> Registration:
        if not ORG_ID.fullmatch(org_id or "") or not 2 <= len(org_id) <= 64:
            raise NandaIndexError(
                "org_id must be 2-64 lowercase letters, digits and dashes")
        if not EMAIL.fullmatch(contact_email or ""):
            raise NandaIndexError("contact_email must be an email address")
        record = {
            "org_id": org_id, "display_name": display_name,
            "hosting_path": "personal", "contact_email": contact_email,
            "registry_url": _https(card_url, "the agent card URL"),
            "media_type": A2A_CARD, "description": description,
            "tags": list(tags or []),
        }
        response = self._call("POST", "/api/v1/orgs", json=record,
                              headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 409:
            raise NandaIndexError(f"org_id {org_id!r} is already taken")
        return Registration.from_record(
            self._json(response, f"registering {org_id}"))

    def verify_email(self, verification_token: str) -> Registration:
        """Follow the index's emailed link; activates a personal record."""
        response = self._call("GET", "/api/v1/verify-email",
                              params={"token": verification_token})
        return Registration.from_record(
            self._json(response, "verifying the email"))

    # -- discovery --------------------------------------------------------

    def search(self, query: str) -> list[dict[str, Any]]:
        response = self._call("GET", "/api/v1/search", params={"q": query})
        results = self._json(response, "searching").get("results", [])
        return [r for r in results
                if isinstance(r, dict) and r.get("status") == "active"]

    def find_peers(self, query: str, me: str) -> list[dict[str, Any]]:
        """Active agents matching query, other than me, by org_id."""
        return sorted((r for r in self.search(query)
                       if r.get("org_id") != me),
                      key=lambda r: r.get("org_id", ""))
