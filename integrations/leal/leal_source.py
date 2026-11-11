# SPDX-License-Identifier: Apache-2.0
"""Where Town gets Leal's answers: the live Leal service, or a copy captured from it.

Two Leal tools are used: ``get_recipe`` (the recipe the agent follows) and
``check_application`` (Leal's check of the exact application at the recipe's self-check
step, with a signed attestation the employer can verify).

- ``live``: set ``NANDATOWN_LEAL_MODE=live`` and ``NANDATOWN_LEAL_ROOT`` to a checkout of
  Leal (https://github.com/Leal-protocol/leal, private). Each request starts Leal's MCP
  server through ``tools/leal_bridge.py`` and calls the tool.
- ``offline`` (the default): answers come from ``fixtures/recipe.json``, captured from the
  live tools by ``tools/capture_fixture.py``. No Leal checkout and no model account are
  needed, so CI and graders can replay every scenario. A packet that was not captured is
  unavailable, never invented.

Example::

    recipe = source_from_env().get_recipe("job-applications")
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_HERE = Path(__file__).resolve().parent
FIXTURE = _HERE / "fixtures" / "recipe.json"
PACKETS = _HERE / "fixtures" / "packets.json"
BRIDGE = _HERE / "tools" / "leal_bridge.py"
PROFILE_ID = "demo-candidate"  # Leal's made-up demo candidate; no real person's data


class LealUnavailable(Exception):
    """Leal could not answer."""


def canonical(data: dict[str, Any]) -> str:
    """The same canonical JSON Leal hashes and signs."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def packet_hash(packet: dict[str, Any]) -> str:
    """The hash Leal puts in its attestation, so the employer can match it to what arrived."""
    return sha256_hex(canonical(packet))


def _check_key(profile_id: str, packet: dict[str, Any]) -> str:
    return sha256_hex(canonical({"profile_id": profile_id, "packet": packet}))


def attestation_problem(
    attestation: dict[str, Any] | None, packet: dict[str, Any], trusted_public_key: str
) -> str | None:
    """What an employer runs before accepting. None if valid, else the reason.

    Mirrors ``verify_attestation`` in Leal's ``core/apply.py``, using only Town's own
    dependencies.

    Example::

        attestation_problem(app["leal_attestation"], packet, leal_card["facts"]["public_key"])
    """
    if not attestation:
        return "no Leal attestation attached"
    if attestation.get("public_key") != trusted_public_key:
        return "signed by a key that is not Leal's"
    if attestation.get("outcome") != "approve":
        return f"Leal's decision was {attestation.get('outcome')}"
    if attestation.get("packet_sha256") != packet_hash(packet):
        return "the application changed after Leal checked it"
    if (attestation.get("employer"), attestation.get("role")) != (packet["employer"], packet["role"]):
        return "the attestation is for another job"
    body = {
        "outcome": attestation["outcome"],
        "packet_sha256": attestation["packet_sha256"],
        "employer": attestation["employer"],
        "role": attestation["role"],
    }
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(trusted_public_key)).verify(
            bytes.fromhex(attestation["signature"]), sha256_hex(canonical(body)).encode()
        )
    except (InvalidSignature, ValueError):
        return "the signature does not verify"
    return None


def build_application(parts_name: str, employer_facts: dict[str, Any]) -> dict[str, Any]:
    """The candidate's application, addressed to the employer the agent found in Town.

    It has exactly the fields of Leal's ``ApplicationPacket``, so the packet Leal checks,
    the packet the employer receives and the hash in Leal's attestation all line up.

    Example::

        build_application("clean", card["facts"])["employer"]
    """
    parts = json.loads(PACKETS.read_text(encoding="utf-8"))[parts_name]
    employer = employer_facts["employer"]
    return {
        "employer": employer,
        "role": employer_facts["role"],
        "posting_text": employer_facts["posting_text"],
        "resume_text": parts["resume_text"],
        "letter_text": parts["letter_text"].replace("{employer}", employer),
        "resume_pages": parts["resume_pages"],
        "letter_pages": parts["letter_pages"],
        "answers": parts["answers"],
        "salary_answer_usd": parts.get("salary_answer_usd"),
    }


class OfflineSource:
    """Leal's answers exactly as its tools returned them in a live capture."""

    mode = "offline"

    def __init__(self, path: Path = FIXTURE) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        self.recipes: dict[str, dict[str, Any]] = data["recipes"]
        self.checks: dict[str, dict[str, Any]] = data.get("checks", {})
        self._public_key: str = data.get("public_key", "")
        self.detail = f"captured {data.get('captured', '')} from Leal {data.get('leal_commit', '')}"

    def get_recipe(self, recipe_id: str) -> dict[str, Any]:
        if recipe_id not in self.recipes:
            raise LealUnavailable(f"no recipe {recipe_id!r} in the capture")
        return self.recipes[recipe_id]

    def check_application(self, profile_id: str, packet: dict[str, Any]) -> dict[str, Any]:
        entry = self.checks.get(_check_key(profile_id, packet))
        if entry is None:
            raise LealUnavailable("this exact application was not in the capture")
        return entry["response"]

    def public_key(self) -> str:
        if not self._public_key:
            raise LealUnavailable("no Leal public key in the capture")
        return self._public_key


class LiveSource:
    """Calls the real Leal MCP server through the bridge."""

    mode = "live"

    def __init__(self, root: Path) -> None:
        uv = shutil.which("uv")
        if uv is None:
            raise LealUnavailable("live mode needs uv to run Leal's environment")
        self.command = [uv, "run", "--directory", str(root), "python", str(BRIDGE)]
        self.detail = f"Leal MCP server at {root}"

    def _run(self, args: list[str], stdin: str = "") -> Any:
        try:
            done = subprocess.run(
                [*self.command, *args], input=stdin, capture_output=True, text=True, timeout=120
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LealUnavailable(str(exc)) from exc
        if done.returncode != 0:
            raise LealUnavailable(done.stderr.strip()[-300:] or "the bridge failed")
        result = json.loads(done.stdout)
        if isinstance(result, dict) and "error" in result:
            raise LealUnavailable(result["error"])
        return result

    def get_recipe(self, recipe_id: str) -> dict[str, Any]:
        return self._run([recipe_id])

    def check_application(self, profile_id: str, packet: dict[str, Any]) -> dict[str, Any]:
        arguments = json.dumps({"profile_id": profile_id, "packet": packet})
        return self._run(["--tool", "check_application"], arguments)

    def public_key(self) -> str:
        return self._run(["--tool", "leal_public_key"], "{}")


def source_from_env() -> OfflineSource | LiveSource:
    """Offline unless ``NANDATOWN_LEAL_MODE=live``.

    Example::

        source_from_env().mode  # 'offline'
    """
    if os.environ.get("NANDATOWN_LEAL_MODE", "offline") == "live":
        root = os.environ.get("NANDATOWN_LEAL_ROOT", "")
        if not root:
            raise LealUnavailable("set NANDATOWN_LEAL_ROOT to a Leal checkout")
        return LiveSource(Path(root))
    return OfflineSource()
