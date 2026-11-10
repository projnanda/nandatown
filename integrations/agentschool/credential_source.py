# SPDX-License-Identifier: Apache-2.0
"""Where Town gets AgentSchool credentials from, offline or live.

Two sources answer the same three questions a relying party asks: which agents
are certified in a skill, what credential does one of them present, and how did
the job it was given turn out.

``FixtureSource`` reads a file captured from a real AgentSchool run, so the
compatibility check reproduces with no AgentSchool, no model account and no
network. ``LiveSource`` calls a running AgentSchool server and that agent's own
service, so the check exercises the project itself.

The difference is recorded rather than hidden. A fixture holds a proof signed
over a nonce captured earlier, which says nothing about whether the agent is
answering now, and a captured revocation list goes stale within five minutes of
being written. Both sources report what they can and cannot establish through
``describe()``, and the scenario validators turn that into ``not_tested``
stages instead of passes.

Example::

    source = source_from_env()
    for entry in source.certified("data-driven-slides", 1):
        presentation = source.present(entry["agent_id"], audience="nandatown")
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURE_SCHEMA = "nandatown-agentschool-fixture/1"

#: Default audience string Town uses when it asks an agent to prove key custody.
AUDIENCE = "nandatown"

#: How old a captured revocation list may be before a fixture run is refused
#: outright. Inside this window the run proceeds with freshness reported as
#: untested; beyond it the fixture is treated as unusable rather than
#: quietly accepted. One year, because a fixture is an archive, not a feed.
FIXTURE_REVOCATION_MAX_AGE_S = 365 * 86400


class SourceUnavailable(Exception):
    """The configured source could not be reached or read."""


@dataclass
class Presentation:
    """What an agent offered when Town asked it to prove its certification.

    ``absent_reason`` is set when there is nothing to verify, for example an
    agent with no credential at all, which is a refusal rather than an error.

    Example::

        presentation = Presentation(agent_id="agt_1", absent_reason="no_credential")
    """

    agent_id: str
    credential_jwt: str | None = None
    nonce: str | None = None
    signature: str | None = None
    audience: str = AUDIENCE
    absent_reason: str | None = None
    #: AgentSchool's own verdict on the same presentation, live mode only.
    issuer_verdict: dict[str, Any] | None = None
    #: Who minted the nonce: "agentschool" live, or "recorded" from a fixture.
    nonce_origin: str = "recorded"
    #: Who produced the holder signature, for the record.
    signer: str = "recorded"

    def as_detail(self) -> dict[str, Any]:
        """A JSON-safe summary for a Town event, without the credential bytes."""
        return {
            "agent_id": self.agent_id,
            "presented": self.credential_jwt is not None,
            "absent_reason": self.absent_reason,
            "nonce_origin": self.nonce_origin,
            "signer": self.signer,
            "issuer_verdict": self.issuer_verdict,
        }


@dataclass
class SourceDescription:
    """Provenance Town writes into the trace before it verifies anything.

    Example::

        description = SourceDescription(mode="fixture", issuer_kid="as-abc")
    """

    mode: str
    issuer_kid: str | None = None
    skill: str | None = None
    skill_version: str | None = None
    agentschool_commit: str | None = None
    server: str | None = None
    captured_at: float | None = None
    revocation_age_s: float | None = None
    #: Checks this source cannot decide, named so a validator can report them.
    cannot_establish: list[str] = field(default_factory=list)

    def as_detail(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "issuer_kid": self.issuer_kid,
            "skill": self.skill,
            "skill_version": self.skill_version,
            "agentschool_commit": self.agentschool_commit,
            "server": self.server,
            "captured_at": self.captured_at,
            "revocation_age_s": self.revocation_age_s,
            "cannot_establish": list(self.cannot_establish),
        }


# -- the captured fixture ----------------------------------------------


class FixtureSource:
    """AgentSchool credentials captured earlier, replayed offline.

    Example::

        source = FixtureSource("integrations/agentschool/fixtures/directory.json")
    """

    mode = "fixture"

    def __init__(self, path: str | Path, *, now: float | None = None) -> None:
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise SourceUnavailable(f"cannot read fixture {self.path}: {exc}") from exc
        if data.get("schema") != FIXTURE_SCHEMA:
            raise SourceUnavailable(
                f"fixture schema is {data.get('schema')!r},"
                f" expected {FIXTURE_SCHEMA!r}")
        self.data = data
        self.jwks: dict[str, Any] = data["jwks"]
        self.revocations_jwt: str = data["revocations_jwt"]
        self._agents = {a["agent_id"]: a for a in data.get("agents", [])}
        self._now = now

    # The captured list is old by construction, so Town reads it with the
    # freshness limit lifted and says so, rather than reading a stale list as
    # an absence of revocations.
    revocation_max_age = FIXTURE_REVOCATION_MAX_AGE_S

    def describe(self) -> SourceDescription:
        meta = self.data.get("agentschool", {})
        captured = self.data.get("revocation_captured_at")
        now = time.time() if self._now is None else self._now
        return SourceDescription(
            mode=self.mode,
            issuer_kid=meta.get("issuer_kid"),
            skill=meta.get("skill"),
            skill_version=meta.get("skill_version"),
            agentschool_commit=meta.get("commit"),
            server=None,
            captured_at=self.data.get("captured_at"),
            revocation_age_s=(None if not isinstance(captured, (int, float))
                              else now - float(captured)),
            cannot_establish=[
                "nonce_freshness",
                "revocation_list_freshness",
                "agent_is_answering_now",
            ],
        )

    def certified(self, skill: str, min_level: int) -> list[dict[str, Any]]:
        """Entries the capture recorded as certified at or above ``min_level``.

        This mirrors AgentSchool's ``search_certified_agents``, which returns
        only agents holding a valid, unexpired, unrevoked credential, so a
        revoked or uncertified agent is absent here by design. Town verifies
        what the capture claims rather than taking it on trust.
        """
        out = []
        for entry in self._agents.values():
            if entry.get("skill", skill) != skill:
                continue
            level = entry.get("level")
            if (isinstance(level, int) and level >= min_level
                    and entry.get("credential_status") == "valid"):
                out.append({k: v for k, v in entry.items()
                            if k != "credential_jwt"})
        return sorted(out, key=lambda e: e["agent_id"])

    def roster(self) -> dict[str, str]:
        """Every agent the capture knows, by name, certified or not.

        A buyer finds sellers through Town's registry, not through
        AgentSchool's search, so it needs to resolve an agent it has already
        met to the identity AgentSchool knows it by. A real candidate agent
        publishes that identity on its own agent card as
        ``agentschool_agent_id``; here the capture supplies it.
        """
        return {e["name"]: e["agent_id"] for e in self._agents.values()
                if isinstance(e.get("name"), str)}

    def present(self, agent_id: str, *, audience: str = AUDIENCE
                ) -> Presentation:
        entry = self._agents.get(agent_id)
        if entry is None:
            return Presentation(agent_id=agent_id, absent_reason="unknown_agent")
        token = entry.get("credential_jwt")
        if not token:
            return Presentation(agent_id=agent_id,
                                absent_reason=entry.get("absent_reason")
                                or "no_credential")
        proof = entry.get("recorded_proof") or {}
        return Presentation(
            agent_id=agent_id, credential_jwt=token,
            nonce=proof.get("nonce"), signature=proof.get("signature"),
            audience=proof.get("audience", audience),
            issuer_verdict=entry.get("agentschool_verdict"),
            nonce_origin="recorded", signer="recorded")

    def report_outcome(self, agent_id: str, task_id: str, success: bool, *,
                       quality: float | None = None,
                       credential_jti: str | None = None,
                       notes: str | None = None) -> dict[str, Any] | None:
        """A fixture has nobody to report to, so nothing is sent."""
        return None


# -- the live server ---------------------------------------------------


class LiveSource:
    """A running AgentSchool server, plus each agent's own service.

    Town calls ``search_certified_agents`` to discover, ``verify_credential``
    to get the credential and a fresh single-use nonce, the agent's own
    ``/challenge`` endpoint to have that nonce signed, ``check_identity_proof``
    for AgentSchool's own verdict, and ``report_task_outcome`` afterwards.

    Example::

        source = LiveSource("http://localhost:8765", token="ast_...")
    """

    mode = "live"
    revocation_max_age = None  # the live list declares its own limit

    def __init__(self, base_url: str, token: str, *, timeout: float = 20.0,
                 skill: str = "data-driven-slides",
                 roster: dict[str, str] | None = None,
                 endpoints: dict[str, str] | None = None) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a Town dep
            raise SourceUnavailable("live mode needs httpx") from exc
        self.base_url = base_url.rstrip("/")
        self.skill = skill
        self._client = httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {token}"}
        # Endpoints from the roster matter for an agent AgentSchool's search
        # will not return, such as one whose credential was revoked. Without
        # one, Town cannot challenge it and records that it could not, which
        # muddles the reason it was refused.
        self._endpoints: dict[str, str] = dict(endpoints or {})
        self._roster = dict(roster or {})
        self.jwks = self._get("/api/.well-known/jwks.json")
        self.revocations_jwt = self._get("/api/revocations")["jwt"]
        self._status = self._get("/api/status")

    def _get(self, path: str) -> Any:
        response = self._client.get(self.base_url + path)
        if response.status_code != 200:
            raise SourceUnavailable(
                f"GET {path} returned {response.status_code}")
        return response.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        response = self._client.post(self.base_url + path, json=body,
                                     headers=self._headers)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceUnavailable(
                f"POST {path} returned non-JSON") from exc
        if response.status_code != 200:
            raise SourceUnavailable(
                f"POST {path} returned {response.status_code}:"
                f" {payload.get('error')} {payload.get('message')}")
        return payload

    def describe(self) -> SourceDescription:
        return SourceDescription(
            mode=self.mode,
            issuer_kid=self._status.get("issuer_kid"),
            skill=self.skill,
            skill_version=None,
            agentschool_commit=os.environ.get(
                "NANDATOWN_AGENTSCHOOL_COMMIT"),
            server=self.base_url,
            captured_at=time.time(),
            revocation_age_s=0.0,
            cannot_establish=["exam_validity", "a2a_conformance"],
        )

    def certified(self, skill: str, min_level: int) -> list[dict[str, Any]]:
        found = self._post("/api/search_certified_agents",
                           {"skill": skill, "min_level": min_level})
        out = []
        for entry in found if isinstance(found, list) else []:
            agent_id = entry.get("agent_id")
            if isinstance(agent_id, str):
                out.append({"agent_id": agent_id, "name": entry.get("name"),
                            "level": entry.get("level"),
                            "score": entry.get("score"), "skill": skill,
                            "endpoint": entry.get("endpoint")})
                if isinstance(entry.get("endpoint"), str):
                    self._endpoints[agent_id] = entry["endpoint"]
                if isinstance(entry.get("name"), str):
                    self._roster.setdefault(entry["name"], agent_id)
        return sorted(out, key=lambda e: e["agent_id"])

    def roster(self) -> dict[str, str]:
        """Names Town can resolve to an AgentSchool identity.

        ``search_certified_agents`` returns only currently valid credentials,
        so an agent whose credential was revoked, or that never earned one,
        cannot be resolved from the search alone. Those identities come from
        ``NANDATOWN_AGENTSCHOOL_ROSTER``, a JSON object mapping name to
        agent id, standing in for the identity each agent publishes on its own
        agent card.
        """
        return dict(self._roster)

    def present(self, agent_id: str, *, audience: str = AUDIENCE,
                min_level: int = 1,
                town_nonce: str | None = None) -> Presentation:
        """Collect a credential and two separate proofs of key custody.

        AgentSchool mints a nonce of its own with the credential, and only
        AgentSchool can decide whether that nonce was fresh and unspent,
        because it issued it and holds the used-nonce store. Its verdict is
        recorded as its own claim.

        For a check Town can make on its own evidence, Town mints a second
        nonce and asks the agent's service to sign that one too. The signature
        that comes back is verified against the holder key named in the
        credential, and Town refuses a replay of its own nonce, so freshness
        rests on a nonce Town chose rather than on AgentSchool's word.
        """
        offered = self._post("/api/verify_credential",
                             {"agent_id": agent_id, "skill": self.skill,
                              "min_level": min_level, "audience": audience})
        token = offered.get("credential_jwt")
        if not token:
            reasons = offered.get("reasons") or ["no_credential"]
            return Presentation(agent_id=agent_id, absent_reason=reasons[0],
                                nonce_origin="town", signer="none")

        issuer_nonce = offered.get("nonce")
        issuer_signature, issuer_signer = self._sign(agent_id, issuer_nonce,
                                                     audience)
        issuer_verdict = None
        if issuer_signature is not None:
            issuer_verdict = self._post(
                "/api/check_identity_proof",
                {"credential_jwt": token, "nonce": issuer_nonce,
                 "signature": issuer_signature, "audience": audience})

        if town_nonce is None:
            # No Town nonce asked for, so the only proof is over AgentSchool's,
            # whose freshness Town cannot judge. Say which nonce this is.
            return Presentation(
                agent_id=agent_id, credential_jwt=token, nonce=issuer_nonce,
                signature=issuer_signature, audience=audience,
                issuer_verdict=issuer_verdict, nonce_origin="agentschool",
                signer=issuer_signer)

        town_signature, town_signer = self._sign(agent_id, town_nonce,
                                                 audience)
        return Presentation(
            agent_id=agent_id, credential_jwt=token, nonce=town_nonce,
            signature=town_signature, audience=audience,
            issuer_verdict=issuer_verdict, nonce_origin="town",
            signer=town_signer)

    def _sign(self, agent_id: str, nonce: Any,
              audience: str) -> tuple[str | None, str]:
        """Ask the agent's own service to sign Town's nonce.

        The signature has to come from the agent, not from Town, or the proof
        establishes nothing. When the agent publishes no reachable service,
        Town records that it could not be challenged instead of substituting a
        signature of its own.
        """
        endpoint = self._endpoints.get(agent_id)
        if not isinstance(endpoint, str) or not isinstance(nonce, str):
            return None, "unreachable"
        try:
            response = self._client.post(
                endpoint.rstrip("/") + "/challenge",
                json={"nonce": nonce, "audience": audience})
        except Exception:  # noqa: BLE001 - any transport failure is one case
            return None, "unreachable"
        if response.status_code != 200:
            return None, f"challenge_http_{response.status_code}"
        try:
            signature = response.json().get("signature")
        except ValueError:
            return None, "challenge_non_json"
        if not isinstance(signature, str):
            return None, "challenge_no_signature"
        return signature, "agent_service"

    def report_outcome(self, agent_id: str, task_id: str, success: bool, *,
                       quality: float | None = None,
                       credential_jti: str | None = None,
                       notes: str | None = None) -> dict[str, Any] | None:
        body: dict[str, Any] = {"agent_id": agent_id, "task_id": task_id,
                                "success": bool(success)}
        if quality is not None:
            body["quality"] = quality
        if credential_jti is not None:
            body["credential_jti"] = credential_jti
        if notes is not None:
            body["notes"] = notes
        return self._post("/api/report_task_outcome", body)


# -- selection ---------------------------------------------------------

DEFAULT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "directory.json"


def read_roster(path: str | None
                ) -> tuple[dict[str, str], dict[str, str]]:
    """Read the roster file into (name -> agent id, agent id -> endpoint).

    Each value is either the agent id on its own, or an object carrying the id
    and the agent's service URL, which is the same shape
    ``tools/capture_fixture.py`` reads::

        {"atlas": {"agent_id": "agt_1", "endpoint": "http://127.0.0.1:8101"},
         "flash": "agt_3"}

    Example::

        roster, endpoints = read_roster("roster.json")
    """
    if not path:
        return {}, {}
    try:
        loaded = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise SourceUnavailable(f"cannot read roster {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SourceUnavailable(
            "the roster must be a JSON object of name -> agent id, or"
            " name -> {agent_id, endpoint}")
    roster: dict[str, str] = {}
    endpoints: dict[str, str] = {}
    for name, value in loaded.items():
        if isinstance(value, str):
            roster[name] = value
        elif isinstance(value, dict) and isinstance(value.get("agent_id"), str):
            roster[name] = value["agent_id"]
            if isinstance(value.get("endpoint"), str):
                endpoints[value["agent_id"]] = value["endpoint"]
    if not roster:
        raise SourceUnavailable(
            f"the roster {path} named no agent ids")
    return roster, endpoints


def source_from_env(env: dict[str, str] | None = None):
    """Build the source the operator selected, defaulting to the fixture.

    ``NANDATOWN_AGENTSCHOOL_MODE`` picks ``fixture`` or ``live``. Live mode
    needs ``NANDATOWN_AGENTSCHOOL_URL`` and ``NANDATOWN_AGENTSCHOOL_TOKEN``;
    fixture mode takes an optional ``NANDATOWN_AGENTSCHOOL_FIXTURE`` path.

    Example::

        source = source_from_env({"NANDATOWN_AGENTSCHOOL_MODE": "fixture"})
    """
    env = dict(os.environ if env is None else env)
    mode = env.get("NANDATOWN_AGENTSCHOOL_MODE", "fixture").strip().lower()
    if mode == "live":
        url = env.get("NANDATOWN_AGENTSCHOOL_URL",
                      "http://127.0.0.1:8765")
        token = env.get("NANDATOWN_AGENTSCHOOL_TOKEN")
        if not token:
            raise SourceUnavailable(
                "live mode needs NANDATOWN_AGENTSCHOOL_TOKEN, the bearer token"
                " of an agent registered with AgentSchool")
        roster, endpoints = read_roster(
            env.get("NANDATOWN_AGENTSCHOOL_ROSTER"))
        return LiveSource(url, token, roster=roster, endpoints=endpoints,
                          skill=env.get("NANDATOWN_AGENTSCHOOL_SKILL",
                                        "data-driven-slides"))
    if mode != "fixture":
        raise SourceUnavailable(
            f"unknown mode {mode!r}; use 'fixture' or 'live'")
    return FixtureSource(env.get("NANDATOWN_AGENTSCHOOL_FIXTURE")
                         or DEFAULT_FIXTURE)
