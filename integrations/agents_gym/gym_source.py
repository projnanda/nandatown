"""Where the scorecards come from: a captured fixture, or the live gym.

Agents Gym runs as its own service. Its public evaluation endpoint runs the
scripted candidates on a built-in workflow against the real gym server and
returns one signed scorecard per candidate, plus the gym's own four-level
verification of each:

    POST https://agents-gym.vercel.app/api/public/evaluate
    {"workflow": "sme_loan", "perturbation": "missing_bank_statements",
     "candidates": ["honest", "fabricator", "shortcut", "lazy", "flaky"]}

It is rate limited (8 runs per IP per 10 minutes, 400 per hour overall).

- ``fixture`` mode (the default) reads cards captured from that endpoint, so
  a run needs no network and no account.
- ``live`` mode calls the endpoint once per run, before the simulation
  starts, when ``NANDATOWN_AGENTSGYM_URL`` is set or a scenario names a
  ``gym_url``. If the gym cannot be reached, the source holds no cards and
  says why; nothing falls back to the fixture.

Example::

    source = GymSource.from_config({})
    source.card_for("honest")
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "sme_loan_cards.json"
ENV_URL = "NANDATOWN_AGENTSGYM_URL"
DEFAULT_LIVE_URL = "https://agents-gym.vercel.app/api/public/evaluate"
# The gym signs with one stable key (agk_02d9388016c5e166). It is pinned here
# rather than taken from the response, so a card signed by any other key fails
# key_trusted even when its signature verifies.
PINNED_KEY = ("ed25519:"
              "44fc008abc61ff51a33e760c2a90b288c270bb71c6ecfa82af2d3c630a6d2c35")
WORKFLOW = "sme_loan"
PERTURBATION = "missing_bank_statements"
CANDIDATES = ["honest", "fabricator", "shortcut", "lazy", "flaky"]
TIMEOUT_SECONDS = 20.0


@dataclass
class GymSource:
    mode: str
    endpoint: str | None
    captured_at: str | None
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    gym_verdicts: dict[str, dict[str, str]] = field(default_factory=dict)
    error: str | None = None
    pinned_key: str = PINNED_KEY

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "GymSource":
        url = config.get("gym_url") or os.environ.get(ENV_URL)
        if url:
            return cls.live(url)
        return cls.fixture(Path(config.get("gym_fixture") or FIXTURE))

    @classmethod
    def fixture(cls, path: Path = FIXTURE) -> "GymSource":
        data = json.loads(path.read_text())
        return cls(mode="fixture", endpoint=data["endpoint"],
                   captured_at=data["captured_at"],
                   cards={n: c["signed_card"]
                          for n, c in data["candidates"].items()},
                   gym_verdicts={n: c["gym_verify"]
                                 for n, c in data["candidates"].items()},
                   pinned_key=data.get("pinned_key", PINNED_KEY))

    @classmethod
    def live(cls, url: str, client: httpx.Client | None = None) -> "GymSource":
        """Ask the gym for fresh cards. Any failure leaves the source empty."""
        if url == "default":
            url = DEFAULT_LIVE_URL
        body = {"workflow": WORKFLOW, "perturbation": PERTURBATION,
                "candidates": CANDIDATES}
        try:
            owned = client is None
            client = client or httpx.Client(timeout=TIMEOUT_SECONDS,
                                            follow_redirects=False,
                                            trust_env=False)
            try:
                response = client.post(url, json=body)
            finally:
                if owned:
                    client.close()
        except httpx.TimeoutException:
            return cls(mode="live", endpoint=url, captured_at=None,
                       error="gym_timeout")
        except httpx.HTTPError as exc:
            return cls(mode="live", endpoint=url, captured_at=None,
                       error=f"gym_unreachable:{type(exc).__name__}")
        if response.status_code != 200:
            return cls(mode="live", endpoint=url, captured_at=None,
                       error=f"gym_http_{response.status_code}")
        try:
            return cls.from_response(url, response.json())
        except (ValueError, KeyError, TypeError):
            return cls(mode="live", endpoint=url, captured_at=None,
                       error="gym_response_unreadable")

    @classmethod
    def from_response(cls, url: str, data: dict[str, Any]) -> "GymSource":
        cards, verdicts = {}, {}
        for run in data["runs"]:
            cards[run["candidate"]] = run["card"]
            verdicts[run["candidate"]] = dict(run["verify"])
        # The gym stamps generatedAt with milliseconds; cards use seconds.
        captured = str(data["generatedAt"])[:19] + "Z"
        return cls(mode="live", endpoint=url, captured_at=captured,
                   cards=cards, gym_verdicts=verdicts)

    def card_for(self, gym_name: str) -> dict[str, Any] | None:
        return self.cards.get(gym_name)

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "endpoint": self.endpoint,
                "captured_at": self.captured_at, "error": self.error,
                "pinned_key": self.pinned_key,
                "workflow": WORKFLOW, "perturbation": PERTURBATION,
                "candidates": sorted(self.cards),
                "gym_verdicts": {n: self.gym_verdicts[n]
                                 for n in sorted(self.gym_verdicts)}}
