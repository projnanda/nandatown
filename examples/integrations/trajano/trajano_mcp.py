"""Buyer-side MCP calls to a Trajano server, callable from Town's synchronous engine.

Trajano mounts its buyer tools as stateless streamable-HTTP MCP, so every call
can open its own session with the official SDK and close it again. That keeps
the anyio task groups inside one `asyncio.run`, with no long-lived thread to
own them, at the cost of one handshake per call.

A tool that refuses (a Trajano `{code, message, retryable}` error) returns
normally: refusals are what the scenarios observe. Only a call that never
produced an answer raises TrajanoError.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class TrajanoError(Exception):
    """The server could not be reached, timed out, or broke the protocol."""


async def _session(url: str, key: str, timeout: float, work):
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await work(session)


def _run(url: str, key: str, timeout: float, work) -> Any:
    try:
        return asyncio.run(asyncio.wait_for(
            _session(url, key, timeout, work), timeout))
    except TimeoutError:
        raise TrajanoError("deadline exceeded") from None
    except TrajanoError:
        raise
    except Exception as exc:  # the SDK wraps failures in exception groups
        raise TrajanoError(f"{type(exc).__name__}: {exc}") from None


def list_tools(url: str, key: str, timeout: float = 20.0) -> list[str]:
    async def work(session: ClientSession) -> list[str]:
        return sorted(tool.name for tool in (await session.list_tools()).tools)
    return _run(url, key, timeout, work)


def call(url: str, key: str, tool: str, args: dict[str, Any],
         timeout: float = 20.0) -> dict[str, Any]:
    async def work(session: ClientSession) -> dict[str, Any]:
        result = await session.call_tool(tool, args)
        if result.isError:
            text = result.content[0].text if result.content else ""
            raise TrajanoError(f"{tool} rejected the call: {text[:200]}")
        if result.structuredContent is not None:
            return result.structuredContent
        return json.loads(result.content[0].text)
    return _run(url, key, timeout, work)
