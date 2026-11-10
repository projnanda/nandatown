# SPDX-License-Identifier: Apache-2.0
"""Where Town gets Leal's recipes: the live Leal service, or a copy captured from it.

- ``live``: set ``NANDATOWN_LEAL_MODE=live`` and ``NANDATOWN_LEAL_ROOT`` to a checkout of
  Leal (https://github.com/Leal-protocol/leal, private). Each request starts Leal's MCP
  server through ``tools/leal_bridge.py`` and calls its ``get_recipe`` tool.
- ``offline`` (the default): the recipe comes from ``fixtures/recipe.json``, captured from
  the live tool by ``tools/capture_fixture.py``. No Leal checkout and no model account are
  needed, so CI and graders can replay every scenario.

Example::

    recipe = source_from_env().get_recipe("job-applications")
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
FIXTURE = _HERE / "fixtures" / "recipe.json"
PACKETS = _HERE / "fixtures" / "packets.json"
BRIDGE = _HERE / "tools" / "leal_bridge.py"


class LealUnavailable(Exception):
    """Leal could not hand over the recipe."""


def build_application(parts_name: str, employer_facts: dict[str, Any]) -> dict[str, Any]:
    """The candidate's documents, addressed to the employer the agent found in Town.

    Example::

        build_application("clean", card["facts"])["employer"]
    """
    parts = json.loads(PACKETS.read_text(encoding="utf-8"))[parts_name]
    employer = employer_facts["employer"]
    return {
        "employer": employer,
        "role": employer_facts["role"],
        "resume_text": parts["resume_text"],
        "letter_text": parts["letter_text"].replace("{employer}", employer),
        "resume_pages": parts["resume_pages"],
        "letter_pages": parts["letter_pages"],
        "answers": parts["answers"],
    }


class OfflineSource:
    """The recipe exactly as Leal's get_recipe tool returned it in a live capture."""

    mode = "offline"

    def __init__(self, path: Path = FIXTURE) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        self.recipes: dict[str, dict[str, Any]] = data["recipes"]
        self.detail = f"captured {data.get('captured', '')} from Leal {data.get('leal_commit', '')}"

    def get_recipe(self, recipe_id: str) -> dict[str, Any]:
        if recipe_id not in self.recipes:
            raise LealUnavailable(f"no recipe {recipe_id!r} in the capture")
        return self.recipes[recipe_id]


class LiveSource:
    """Calls the real Leal MCP server through the bridge."""

    mode = "live"

    def __init__(self, root: Path) -> None:
        uv = shutil.which("uv")
        if uv is None:
            raise LealUnavailable("live mode needs uv to run Leal's environment")
        self.command = [uv, "run", "--directory", str(root), "python", str(BRIDGE)]
        self.detail = f"Leal MCP server at {root}"

    def get_recipe(self, recipe_id: str) -> dict[str, Any]:
        try:
            done = subprocess.run(
                [*self.command, recipe_id], capture_output=True, text=True, timeout=120
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LealUnavailable(str(exc)) from exc
        if done.returncode != 0:
            raise LealUnavailable(done.stderr.strip()[-300:] or "the bridge failed")
        recipe = json.loads(done.stdout)
        if "error" in recipe:
            raise LealUnavailable(recipe["error"])
        return recipe


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
