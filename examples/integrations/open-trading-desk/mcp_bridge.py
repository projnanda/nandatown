"""One live MCP stdio session, callable from Town's synchronous Lab engine.

The MCP SDK opens task groups, and anyio requires a cancel scope to be
entered and exited by the same task. So one owner task, on its own thread
and event loop, opens both context managers, serves a queue of requests,
and closes them.

A Lab callback waits on each request with a deadline. That wait pauses the
simulation, so a request must never depend on a Town callback, and only the
engine thread may emit observations or schedule messages.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import threading
from typing import Any, Awaitable, Callable

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

Invoke = Callable[..., Awaitable[Any]]

_MARGIN = 2.0  # seconds the caller waits beyond the owner task's own deadline
_OPEN: list["McpBridge"] = []


class BridgeError(Exception):
    """A session could not start, a call failed, or the session is gone."""


class McpBridge:
    def __init__(self, command: str, args: list[str], invoke: Invoke,
                 env: dict[str, str] | None = None,
                 cwd: str | None = None,
                 startup_timeout: float = 30.0,
                 call_timeout: float = 30.0):
        self._params = StdioServerParameters(
            command=command, args=args, env=env, cwd=cwd)
        self._invoke = invoke
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout
        self._ready: concurrent.futures.Future = concurrent.futures.Future()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue | None = None
        self._failure: Exception | None = None

    def start(self) -> list[str]:
        """Open the session and return the server's tool names."""
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._own()), daemon=True,
            name="otd-mcp-bridge")
        self._thread.start()
        _OPEN.append(self)
        try:
            return self._ready.result(self._startup_timeout + _MARGIN)
        except concurrent.futures.TimeoutError:
            raise BridgeError("startup deadline exceeded") from None

    def call(self, tool: str, **arguments: Any) -> Any:
        """Run one tool call. A timeout or tool error raises BridgeError."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._put((tool, arguments, future))
        try:
            return future.result(self._call_timeout + _MARGIN)
        except concurrent.futures.TimeoutError:
            raise BridgeError(f"{tool}: deadline exceeded") from None

    def close(self, timeout: float = 10.0) -> bool:
        """Stop the session. True when it ended cleanly and is gone.
        Never raises, so one bad session cannot stop the others closing."""
        thread = self._thread
        if thread is None:
            return True
        try:
            self._put(None)
            thread.join(timeout)
            if thread.is_alive() and self._loop and self._task:
                # Cancelling unwinds inside the owner task, so the SDK's own
                # cleanup still terminates and reaps the server process.
                self._loop.call_soon_threadsafe(self._task.cancel)
                thread.join(timeout)
        except (BridgeError, RuntimeError):  # the loop is already closing
            thread.join(timeout)
        return not thread.is_alive() and self._failure is None

    def _put(self, item: Any) -> None:
        if self._loop is None or self._queue is None or self._loop.is_closed():
            raise BridgeError("session is not running")
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            raise BridgeError("session is not running") from None

    async def _own(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        queue: asyncio.Queue = asyncio.Queue()
        self._queue = queue
        error: BaseException | None = None
        try:
            async with stdio_client(self._params) as (read, write):
                async with ClientSession(read, write) as session:
                    with anyio.fail_after(self._startup_timeout):
                        await session.initialize()
                        tools = await session.list_tools()
                    self._ready.set_result([t.name for t in tools.tools])
                    await self._serve(session, queue)
        except BaseException as exc:
            error = exc
            if not self._ready.done():
                self._ready.set_exception(BridgeError(
                    f"startup failed: {type(exc).__name__}"))
            elif isinstance(exc, Exception):  # not a cancel from close()
                self._failure = exc
        finally:
            self._fail_pending(error)

    async def _serve(self, session: ClientSession,
                     queue: asyncio.Queue) -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            tool, arguments, future = item
            try:
                with anyio.fail_after(self._call_timeout):
                    result = await self._invoke(session, tool, **arguments)
            except TimeoutError:
                future.set_exception(BridgeError(f"{tool}: timed out"))
            except Exception as exc:
                future.set_exception(BridgeError(f"{tool}: {exc}"))
            except BaseException:  # cancelled by close(); free the caller
                future.set_exception(BridgeError(f"{tool}: session closed"))
                raise
            else:
                future.set_result(result)

    def _fail_pending(self, error: BaseException | None) -> None:
        reason = type(error).__name__ if error else "session closed"
        while self._queue is not None and not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None:
                item[2].set_exception(BridgeError(f"session ended: {reason}"))


def close_all(timeout: float = 10.0) -> bool:
    """Close every session in this process. Town has no agent stop hook."""
    closed = [bridge.close(timeout) for bridge in _OPEN]
    _OPEN.clear()
    return all(closed)


atexit.register(close_all)
