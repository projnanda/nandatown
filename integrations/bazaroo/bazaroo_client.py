"""Minimal client for Bazaroo's external-agent API (standard library only).

Bazaroo contract (verified live against https://bazaaro.ryan-teo.com):
  POST /api/public/agents/register   Bearer key, {name, capabilities[], description}
       201 {"agent_id": ...}   new name, or same key re-registering its own name
       409 {"error":"name_taken"}   name owned by a different key (or reserved)
       401 missing/unknown key, 400 bad body
  GET  /api/public/agents/search?q=  Bearer key
       200 {"agents": [{id,name,description,capabilities,source}]}

Design rule for the Nanda Town integration: Bazaroo makes the accept/reject
decision. This client never decides ownership locally; it only reports what
Bazaroo answered. A refusal is raised as NameTaken, never swallowed.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DEFAULT_BASE_URL = "https://bazaaro.ryan-teo.com"


class BazarooError(Exception):
    """Base class for all Bazaroo client errors."""


class Unauthorized(BazarooError):
    """401: missing or unknown API key."""


class NameTaken(BazarooError):
    """409: the name belongs to a different key, or is a reserved built-in."""


class BadRequest(BazarooError):
    """400: Bazaroo rejected the request body or query."""


class Unavailable(BazarooError):
    """Network failure, timeout, or a 5xx. Bazaroo gave no usable answer.

    Callers must treat this as "unknown", not as accept and not as reject.
    """


@dataclass(frozen=True)
class AgentRecord:
    id: str
    name: str
    description: str
    capabilities: list[str] = field(default_factory=list)
    source: str = "external"


class BazarooClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 10.0):
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls, key_var: str, base_url_var: str = "BAZAROO_BASE_URL", timeout: float = 10.0):
        """Read the key from an environment variable so no secret is ever in code."""
        key = os.environ.get(key_var, "")
        if not key:
            raise ValueError(f"environment variable {key_var} is not set")
        return cls(key, os.environ.get(base_url_var, DEFAULT_BASE_URL), timeout)

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, self._parse(resp.read())
        except urllib.error.HTTPError as e:  # non-2xx responses carry a JSON body
            with e:  # close the response so Python 3.14 raises no ResourceWarning
                return e.code, self._parse(e.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Unavailable(f"Bazaroo unreachable: {e}") from e

    @staticmethod
    def _parse(raw: bytes) -> dict:
        try:
            parsed = json.loads(raw.decode() or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def register(self, name: str, capabilities: list[str], description: str) -> str:
        """Register an agent. Returns agent_id. Raises NameTaken on 409."""
        status, body = self._request(
            "POST",
            "/api/public/agents/register",
            {"name": name, "capabilities": capabilities, "description": description},
        )
        if status == 201:
            return str(body["agent_id"])
        self._raise(status, body)

    def search(self, query: str) -> list[AgentRecord]:
        """Search by name/description/capability. Returns matching agents."""
        status, body = self._request(
            "GET", "/api/public/agents/search?" + urllib.parse.urlencode({"q": query})
        )
        if status == 200:
            return [
                AgentRecord(
                    id=a["id"],
                    name=a["name"],
                    description=a.get("description", ""),
                    capabilities=list(a.get("capabilities", [])),
                    source=a.get("source", "external"),
                )
                for a in body.get("agents", [])
            ]
        self._raise(status, body)

    @staticmethod
    def _raise(status: int, body: dict):
        err = body.get("error", "")
        if status == 401:
            raise Unauthorized(err or "unauthorized")
        if status == 409:
            raise NameTaken(err or "name_taken")
        if status == 400:
            raise BadRequest(err or "bad request")
        raise Unavailable(f"unexpected HTTP {status}: {err}")
