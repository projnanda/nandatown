# SPDX-License-Identifier: Apache-2.0
"""Ask the real Leal MCP server for a recipe and print it as JSON.

This script runs inside Leal's own environment, not Town's, because the MCP client library
is Leal's dependency and Town does not need it. ``leal_source.LiveSource`` starts it as:

    uv run --directory <LEAL_ROOT> python <this file> <recipe_id>

It starts Leal's MCP server over stdio, exactly as an agent host would, calls the
``get_recipe`` tool and prints the result.

Example::

    uv run --directory ../leal python leal_bridge.py job-applications
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _payload(result: Any) -> Any:
    data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if data:
        return data["result"] if set(data) == {"result"} else data
    return json.loads(result.content[0].text)


async def _get(recipe_id: str) -> Any:
    root = Path.cwd()
    db = Path(tempfile.mkdtemp()) / "leal.db"
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(root / "server" / "mcp_server.py")],
        env={**os.environ, "PYTHONPATH": str(root), "LEAL_DATABASE_URL": f"sqlite:///{db}"},
        cwd=str(root),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return _payload(await session.call_tool("get_recipe", {"recipe_id": recipe_id}))


def main() -> None:
    print(json.dumps(asyncio.run(_get(sys.argv[1]))))


if __name__ == "__main__":
    main()
