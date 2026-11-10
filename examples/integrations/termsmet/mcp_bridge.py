"""Synchronous, identity-scoped bridge to two real TermsMet MCP processes."""

import asyncio
import copy
import json
import os
import queue
import threading
import time
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError
from mcp.types import REQUEST_TIMEOUT


class BridgeError(RuntimeError):
    """A safe-to-log bridge failure category, without server diagnostics."""

    def __init__(self, category):
        self.category = category
        super().__init__(category)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _business_result(response):
    if response.is_error:
        raise ValueError('MCP tool error')
    texts = [item.text for item in response.content if item.type == 'text']
    if len(texts) > 1:
        raise ValueError('multiple text results')
    parsed = json.loads(texts[0]) if texts else None
    result = response.structured_content if response.structured_content is not None else parsed
    if not isinstance(result, dict):
        raise ValueError('invalid MCP response')
    if texts and response.structured_content is not None and json.dumps(parsed, sort_keys=True, allow_nan=False) != json.dumps(result, sort_keys=True, allow_nan=False):
        raise ValueError('inconsistent MCP response')
    return result


class TermsMetBridge:
    def __init__(self, project_dir, project_python, db_path, review_seconds=2,
                 startup_timeout=20, call_timeout=10):
        self.project_dir = Path(project_dir).resolve()
        self.project_python = os.path.abspath(project_python)
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            raise ValueError('db_path must be absolute')
        if min(review_seconds, startup_timeout, call_timeout) <= 0:
            raise ValueError('timeouts and review period must be positive')
        self.review_seconds = review_seconds
        self.startup_timeout = startup_timeout
        self.call_timeout = call_timeout
        self._queue = queue.Queue(maxsize=32)
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._thread = None
        self._state = 'new'
        self._startup_error = None
        self._cleanup_error = False
        self._inventory = None
        self._records = []
        self._next_id = 0

    @property
    def records(self):
        with self._lock:
            return copy.deepcopy(self._records)

    def start(self):
        with self._lock:
            if self._state != 'new':
                raise BridgeError('invalid_state')
            self._state = 'starting'
            self._thread = threading.Thread(target=self._run, name='termsmet-mcp-owner', daemon=True)
            self._thread.start()
        # Each session has its own startup deadline; allow both plus bounded cleanup.
        if not self._ready.wait(2 * self.startup_timeout + 1):
            self.close()
            raise BridgeError('startup_timeout')
        with self._lock:
            if self._startup_error:
                raise BridgeError(self._startup_error)
            if self._state != 'ready':
                raise BridgeError('invalid_state')
            return copy.deepcopy(self._inventory)

    def call(self, identity, tool, arguments, request_id):
        deadline = time.monotonic() + self.call_timeout
        if identity not in ('buyer', 'provider') or not isinstance(tool, str) or not isinstance(arguments, dict) or not isinstance(request_id, str):
            raise BridgeError('invalid_request')
        args = copy.deepcopy(arguments)
        with self._lock:
            if self._state != 'ready':
                raise BridgeError('invalid_state')
            self._next_id += 1
            record = {'call_id': self._next_id, 'request_id': request_id, 'identity': identity,
                      'tool': tool, 'arguments': copy.deepcopy(args), 'requested_at': _now()}
            self._records.append(record)
            request = {'identity': identity, 'tool': tool, 'arguments': args, 'deadline': deadline,
                       'record': record, 'event': threading.Event(), 'state': 'queued'}
            try:
                self._queue.put_nowait(request)
            except queue.Full:
                self._finish(request, error='queue_full')
                raise BridgeError('queue_full') from None
        if not request['event'].wait(max(0, deadline - time.monotonic())):
            with self._lock:
                if not request['event'].is_set():
                    if request['state'] == 'queued':
                        self._finish(request, error='queue_timeout')
                    else:
                        self._state = 'failed'
                        self._finish(request, error='call_timeout_unknown')
        if request.get('error') in ('call_timeout_unknown', 'transport_error'):
            self.close()
        if request.get('error'):
            raise BridgeError(request['error'])
        return copy.deepcopy(request['result'])

    def _finish(self, request, result=None, error=None):
        # Caller holds _lock. A late server response must never overwrite a timeout.
        if request['event'].is_set():
            return
        request['state'] = 'done'
        request['record']['returned_at'] = _now()
        if error:
            request['error'] = error
            request['record']['transport_error'] = error
        else:
            request['result'] = copy.deepcopy(result)
            request['record']['result'] = copy.deepcopy(result)
        request['event'].set()

    def close(self):
        with self._lock:
            if self._state == 'new':
                self._state = 'closed'
                return
            if self._state == 'closed':
                if self._cleanup_error:
                    raise BridgeError('cleanup_failed')
                return
            self._state = 'closing'
            while True:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    break
                if pending is not None:
                    self._finish(pending, error='bridge_closed')
            self._queue.put_nowait(None)
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(self.call_timeout + self.startup_timeout + 15)
            if thread.is_alive():
                raise BridgeError('cleanup_failed')
        with self._lock:
            if self._state != 'closed' or self._cleanup_error:
                raise BridgeError('cleanup_failed')

    def _run(self):
        try:
            asyncio.run(self._owner())
        except BaseException:
            with self._lock:
                self._cleanup_error = True
                if not self._ready.is_set():
                    self._startup_error = 'startup_failed'
                self._state = 'failed'
        finally:
            with self._lock:
                while True:
                    try:
                        pending = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if pending is not None:
                        self._finish(pending, error='bridge_closed')
                self._state = 'closed'
                self._ready.set()

    async def _owner(self):
        import tempfile
        async with AsyncExitStack() as stack:
            diagnostics = stack.enter_context(tempfile.TemporaryFile(mode='w+'))
            sessions = {}
            inventory = {}
            for identity in ('buyer', 'provider'):
                params = StdioServerParameters(
                    command=self.project_python, args=['-m', 'server.mcp_server'], cwd=self.project_dir,
                    env={'TERMSMET_IDENTITY': identity, 'TERMSMET_DB_PATH': str(self.db_path),
                         'TERMSMET_REVIEW_SECONDS': str(self.review_seconds), 'TERMSMET_FIRST_SUBMIT_MIN': '5'})
                try:
                    async with asyncio.timeout(self.startup_timeout):
                        read, write = await stack.enter_async_context(stdio_client(params, errlog=diagnostics))
                        session = await stack.enter_async_context(ClientSession(read, write))
                        await session.initialize()
                        names = [tool.name for tool in (await session.list_tools()).tools]
                        sessions[identity] = session
                        inventory[identity] = names
                except TimeoutError:
                    with self._lock:
                        self._startup_error = 'startup_timeout'
                    return
                except Exception:
                    with self._lock:
                        self._startup_error = 'startup_failed'
                    return
            with self._lock:
                if self._state != 'starting':
                    return
                self._inventory = inventory
                self._state = 'ready'
                self._ready.set()
            while True:
                request = await asyncio.to_thread(self._queue.get)
                if request is None:
                    return
                with self._lock:
                    if request['event'].is_set():
                        continue
                    if self._state != 'ready':
                        self._finish(request, error='bridge_closed')
                        continue
                    remaining = request['deadline'] - time.monotonic()
                    if remaining <= 0:
                        self._finish(request, error='queue_timeout')
                        continue
                    request['state'] = 'running'
                try:
                    async with asyncio.timeout(remaining):
                        response = await sessions[request['identity']].call_tool(request['tool'], request['arguments'])
                    result = _business_result(response)
                    with self._lock:
                        self._finish(request, result=result)
                except TimeoutError:
                    with self._lock:
                        self._state = 'failed'
                        self._finish(request, error='call_timeout_unknown')
                    return
                except Exception as exc:
                    category = 'call_timeout_unknown' if isinstance(exc, MCPError) and exc.code == REQUEST_TIMEOUT else 'transport_error'
                    with self._lock:
                        self._state = 'failed'
                        self._finish(request, error=category)
                    return
