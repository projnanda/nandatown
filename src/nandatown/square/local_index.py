"""An in-process stand-in for NANDA Index, for demos and tests.

It answers the nanda-index-v2 endpoints Town Square uses, with the same
rules: a personal record starts pending, following the emailed
verification link activates it, and search returns active records whose
org_id, domain or display_name contains the query. Mount it with
httpx.MockTransport; nothing leaves the process and nothing is written
to the live index.
"""

from __future__ import annotations

import hashlib
import json

import httpx

from .index_client import NandaIndexClient

BASE_URL = "https://index.local"


def _digest(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


class LocalNandaIndex:
    def __init__(self):
        self.users: dict[str, str] = {}
        self.orgs: dict[str, dict] = {}
        self.outbox: dict[str, str] = {}  # contact email -> verify token
        self._tokens: dict[str, str] = {}  # verify token -> org_id
        self.calls: list[tuple[str, str]] = []
        self.fail_with: int | None = None

    def client(self) -> NandaIndexClient:
        return NandaIndexClient(BASE_URL, http=httpx.Client(
            base_url=BASE_URL, transport=httpx.MockTransport(self)))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"error": "BOOM"})
        route = (request.method, request.url.path)
        body = json.loads(request.content) if request.content else {}
        handler = {
            ("POST", "/auth/register"): self._register,
            ("POST", "/auth/login"): self._login,
            ("POST", "/api/v1/orgs"): self._create_org,
            ("GET", "/api/v1/verify-email"): self._verify,
            ("GET", "/api/v1/search"): self._search,
        }.get(route)
        if handler is None:
            return httpx.Response(404, json={"error": "NOT_FOUND"})
        return handler(request, body)

    def _register(self, _request, body) -> httpx.Response:
        if body["email"] in self.users:
            return httpx.Response(409, json={"error": "CONFLICT"})
        self.users = {**self.users, body["email"]: _digest(body["password"])}
        return httpx.Response(201, json={"token": f"jwt:{body['email']}"})

    def _login(self, _request, body) -> httpx.Response:
        if self.users.get(body["email"]) != _digest(body["password"]):
            return httpx.Response(401, json={"error": "UNAUTHORIZED"})
        return httpx.Response(200, json={"token": f"jwt:{body['email']}"})

    def _create_org(self, request, body) -> httpx.Response:
        if not request.headers.get("authorization", "").startswith(
                "Bearer jwt:"):
            return httpx.Response(401, json={"error": "UNAUTHORIZED"})
        if body["org_id"] in self.orgs:
            return httpx.Response(409, json={"error": "CONFLICT"})
        record = {**body, "status": "pending", "email_verified": False}
        token = f"verify-{body['org_id']}"
        self.orgs = {**self.orgs, body["org_id"]: record}
        self._tokens = {**self._tokens, token: body["org_id"]}
        self.outbox = {**self.outbox, body["contact_email"]: token}
        return httpx.Response(201, json=record)

    def _verify(self, request, _body) -> httpx.Response:
        token = request.url.params.get("token", "")
        org_id = self._tokens.get(token)
        if org_id is None:
            return httpx.Response(404, json={"error": "NOT_FOUND"})
        self._tokens = {k: v for k, v in self._tokens.items() if k != token}
        record = {**self.orgs[org_id], "email_verified": True,
                  "status": "active"}
        self.orgs = {**self.orgs, org_id: record}
        return httpx.Response(200, json=record)

    def _search(self, request, _body) -> httpx.Response:
        q = request.url.params.get("q", "").lower()
        hits = [o for o in self.orgs.values() if o["status"] == "active"
                and any(q in str(o.get(k) or "").lower()
                        for k in ("org_id", "domain", "display_name"))]
        return httpx.Response(200, json={"query": q, "count": len(hits),
                                         "results": hits})
