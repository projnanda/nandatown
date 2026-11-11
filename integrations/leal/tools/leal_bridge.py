# SPDX-License-Identifier: Apache-2.0
"""Call a tool on the real Leal MCP server and print the result as JSON.

This script runs inside Leal's own environment, not Town's, because the MCP client library
is Leal's dependency and Town does not need it. ``leal_source.LiveSource`` starts it as:

    uv run --directory <LEAL_ROOT> python <this file> <recipe_id>
    uv run --directory <LEAL_ROOT> python <this file> --tool <tool name>   # arguments on stdin

It starts Leal's MCP server over stdio, exactly as an agent host would, calls the tool
(``get_recipe`` by default, or ``check_application`` and ``leal_public_key``) and prints
the result.

Example::

    uv run --directory ../leal python leal_bridge.py job-applications
    echo '{}' | uv run --directory ../leal python leal_bridge.py --tool leal_public_key
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


async def _call(tool: str, arguments: dict[str, Any]) -> Any:
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
            return _payload(await session.call_tool(tool, arguments))


def main() -> None:
    if sys.argv[1] == "--tool":
        tool, arguments = sys.argv[2], json.loads(sys.stdin.read() or "{}")
    else:
        tool, arguments = "get_recipe", {"recipe_id": sys.argv[1]}
    print(json.dumps(asyncio.run(_call(tool, arguments))))


if __name__ == "__main__":
    main()
