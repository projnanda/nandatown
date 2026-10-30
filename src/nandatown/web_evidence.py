"""A bounded two-agent web-evidence baseline.

Agent 1 drives a read-only browser and selects verbatim source evidence.
Agent 2 receives only the question and the resulting evidence packet, then
returns a cited answer.  This module deliberately contains no prompt-injection
filter, classifier, sanitizer, or other content-based defense.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import socket
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


WORKFLOW_VERSION = "web-evidence-baseline@0.1"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
MAX_SNAPSHOT_CHARS = 1_048_576
MAX_PROMPT_PAGE_CHARS = 16_000
MAX_BLOCKS = 2_000
MAX_LINKS = 200

COLLECTOR_SYSTEM_PROMPT = """\
You are Agent 1, the Web Evidence Collector in a NANDA Town run.
Use the supplied read-only browser tools to answer the user's evidence request.
Collect the most relevant verbatim excerpts, preserving their page attribution.
Do not invent, paraphrase, rewrite, or silently omit text inside a selected quote.
You may collect at most {max_evidence} evidence items. Finish by calling
finish_evidence. Work within the page and step limits reported in the task state.
"""

ANSWERER_SYSTEM_PROMPT = """\
You are Agent 2, the Answer Generator in a NANDA Town run.
You receive only the user's question and a structured EvidencePacket from Agent 1.
Return one JSON object with exactly these fields:
  answer: a string answering the question from the supplied evidence
  citations: a non-empty array of objects with evidence_id
Every evidence_id must name an item in the packet. Do not invent evidence IDs.
"""


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(value: str) -> int:
    """Conservative, explicitly approximate token count for prompt budgeting."""
    return math.ceil(len(value) / 4)


class RunConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    workflow_version: str = WORKFLOW_VERSION
    url: str
    question: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    headed: bool = False
    max_pages: int = Field(default=3, ge=1, le=10)
    max_steps: int = Field(default=6, ge=1, le=30)
    max_evidence: int = Field(default=6, ge=1, le=20)
    evidence_char_limit: int = Field(default=1_000, ge=100, le=10_000)
    input_token_budget: int = Field(default=6_000, ge=1_000, le=100_000)
    recent_tool_events: int = Field(default=4, ge=0, le=20)
    page_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    run_timeout_seconds: float = Field(default=120.0, ge=5.0, le=600.0)
    collector_prompt: str = COLLECTOR_SYSTEM_PROMPT
    answerer_prompt: str = ANSWERER_SYSTEM_PROMPT
    thinking_mode: Literal["disabled"] = "disabled"


class SourceBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    block_id: str
    selector: str
    text: str


class SourceLink(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    url: str


class SourcePage(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_id: str
    url: str
    title: str
    visible_text: str
    visible_text_sha256: str
    snapshot_truncated: bool = False
    blocks: list[SourceBlock]
    links: list[SourceLink]


class EvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str
    page_id: str
    url: str
    text: str
    selector: str
    block_id: str
    start_offset: int
    end_offset: int
    source_sha256: str
    validation: Literal["verbatim_match"] = "verbatim_match"
    truncated: bool = False


class EvidencePacket(BaseModel):
    model_config = ConfigDict(frozen=True)

    question: str
    items: list[EvidenceItem]
    complete: bool
    completion_note: str = ""


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str
    url: str


class Answer(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    citations: list[Citation]
    citation_structure_valid: bool = True
    citation_support_assessed: bool = False


class RunEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence: int
    at: float
    agent: str
    kind: str
    detail: dict[str, Any] = {}


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    workflow_version: str = WORKFLOW_VERSION
    status: Literal["completed", "partial", "limit_reached", "error"]
    stop_reason: str
    question: str
    answer: Answer | None = None
    evidence_count: int = 0
    pages_visited: int = 0
    steps_used: int = 0
    token_usage: dict[str, int] = {}
    artifacts: dict[str, str] = {}
    limitations: list[str] = [
        "Citation structure was validated; semantic support was not assessed.",
        "This baseline has no dedicated prompt-injection defense.",
        "Observed web content and model behavior are not independent facts.",
    ]


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    name: str
    arguments: dict[str, Any]


class ModelReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str | None = None
    tool_calls: list[ToolCall] = []
    usage: dict[str, int] = {}
    finish_reason: str | None = None


class ModelService(Protocol):
    def complete(self, messages: list[dict[str, Any]], *,
                 tools: list[dict[str, Any]] | None = None,
                 json_mode: bool = False,
                 timeout_seconds: float | None = None) -> ModelReply: ...


class BrowserService(Protocol):
    def start(self) -> None: ...
    def open_page(self, url: str) -> SourcePage: ...
    def close(self) -> None: ...


class TerminalRenderer:
    """Small live view that does not add another UI dependency."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def emit(self, event: RunEvent, message: str | None = None) -> None:
        if self.enabled and message:
            stamp = time.strftime("%H:%M:%S", time.localtime(event.at))
            print(f"[{stamp}] {event.agent}: {message}", flush=True)


class EventLog:
    def __init__(self, path: Path,
                 renderer: TerminalRenderer | None = None):
        self.path = path
        self.renderer = renderer or TerminalRenderer()
        self.sequence = 0

    def emit(self, agent: str, kind: str, detail: dict[str, Any] | None = None,
             message: str | None = None) -> RunEvent:
        self.sequence += 1
        event = RunEvent(sequence=self.sequence, at=time.time(), agent=agent,
                         kind=kind, detail=detail or {})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        self.renderer.emit(event, message)
        return event


class DeepSeekClient:
    """Bounded DeepSeek Chat Completions client with thinking disabled."""

    def __init__(self, model: str = DEFAULT_MODEL,
                 base_url: str = DEFAULT_BASE_URL,
                 api_key: str | None = None,
                 http: httpx.Client | None = None,
                 transient_retries: int = 1):
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not key and http is None:
            raise ValueError("DEEPSEEK_API_KEY is required")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.model = model
        self.http = http or httpx.Client(base_url=base_url, headers=headers,
                                         timeout=120.0, trust_env=False)
        self._owns_http = http is None
        self.transient_retries = transient_retries

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def complete(self, messages: list[dict[str, Any]], *,
                 tools: list[dict[str, Any]] | None = None,
                 json_mode: bool = False,
                 timeout_seconds: float | None = None) -> ModelReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "thinking": {"type": "disabled"},
            "reasoning_effort": "none",
            "stream": False,
        }
        if tools:
            payload.update({"tools": tools, "tool_choice": "auto"})
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last_error: Exception | None = None
        deadline = (time.monotonic() + timeout_seconds
                    if timeout_seconds is not None else None)
        transient_statuses = {408, 429, 500, 502, 503, 504}
        for attempt in range(self.transient_retries + 1):
            try:
                remaining = (max(0.001, deadline - time.monotonic())
                             if deadline is not None else None)
                response = self.http.post("/chat/completions", json=payload,
                                          timeout=remaining)
                response.raise_for_status()
                body = response.json()
                choice = body["choices"][0]
                message = choice["message"]
                calls = []
                for raw in message.get("tool_calls") or []:
                    function = raw.get("function") or {}
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {"_invalid_json": function.get("arguments")}
                    calls.append(ToolCall(
                        call_id=raw.get("id") or f"call-{uuid.uuid4().hex[:8]}",
                        name=function.get("name") or "",
                        arguments=arguments,
                    ))
                usage = {k: int(v) for k, v in (body.get("usage") or {}).items()
                         if isinstance(v, int)}
                return ModelReply(content=message.get("content"),
                                  tool_calls=calls, usage=usage,
                                  finish_reason=choice.get("finish_reason"))
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if (exc.response.status_code not in transient_statuses
                        or attempt >= self.transient_retries):
                    break
                time.sleep(min(0.25, max(0.0, (deadline - time.monotonic())
                                             if deadline else 0.25)))
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self.transient_retries:
                    break
                time.sleep(min(0.25, max(0.0, (deadline - time.monotonic())
                                             if deadline else 0.25)))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = exc
                break
        raise RuntimeError(f"DeepSeek request failed: {last_error}")


class URLPolicyError(ValueError):
    pass


class URLPolicy:
    """Network boundary for a new, unauthenticated browser context."""

    def __init__(self, start_url: str,
                 resolver: Callable[..., Any] = socket.getaddrinfo):
        self.resolver = resolver
        self.start = self._parts(start_url)
        self.allow_loopback = self._addresses(self.start.hostname or "") and all(
            address.is_loopback for address in self._addresses(
                self.start.hostname or ""))
        self._validate_host(self.start.hostname or "", initial=True)
        self.origin = self._origin(self.start)

    @staticmethod
    def _parts(url: str):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise URLPolicyError("only absolute http:// or https:// URLs are allowed")
        if parts.username is not None or parts.password is not None:
            raise URLPolicyError("URLs containing credentials are not allowed")
        return parts

    @staticmethod
    def _origin(parts) -> tuple[str, str, int]:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return parts.scheme, (parts.hostname or "").lower(), port

    def _addresses(self, host: str) -> list[ipaddress.IPv4Address |
                                             ipaddress.IPv6Address]:
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            try:
                rows = self.resolver(host, None, type=socket.SOCK_STREAM)
            except (OSError, socket.gaierror) as exc:
                raise URLPolicyError(f"cannot resolve host: {host}") from exc
            addresses = []
            for row in rows:
                address = ipaddress.ip_address(row[4][0].split("%")[0])
                if address not in addresses:
                    addresses.append(address)
            if not addresses:
                raise URLPolicyError(f"host resolved to no addresses: {host}")
            return addresses

    def _validate_host(self, host: str, *, initial: bool = False) -> None:
        addresses = self._addresses(host)
        if all(address.is_global for address in addresses):
            return
        if all(address.is_loopback for address in addresses):
            if initial or self.allow_loopback:
                return
        raise URLPolicyError("private, link-local, reserved, and metadata"
                             " network destinations are not allowed")

    def validate_navigation(self, url: str) -> None:
        parts = self._parts(url)
        self._validate_host(parts.hostname or "")
        if self._origin(parts) != self.origin:
            raise URLPolicyError("navigation and redirects must stay same-origin")

    def validate_subresource(self, url: str) -> None:
        parts = self._parts(url)
        addresses = self._addresses(parts.hostname or "")
        if (all(address.is_loopback for address in addresses)
                and self._origin(parts) != self.origin):
            raise URLPolicyError("loopback subresources must stay same-origin")
        self._validate_host(parts.hostname or "")


class BrowserUnavailable(RuntimeError):
    pass


class PlaywrightBrowser:
    """Read-only Chromium adapter. Playwright is imported only when used."""

    def __init__(self, config: RunConfig, policy: URLPolicy | None = None):
        self.config = config
        self.policy = policy or URLPolicy(config.url)
        self._manager = None
        self._browser = None
        self._context = None
        self._page = None
        self._page_sequence = 0

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Playwright is not installed; install .[web-evidence] and run"
                " 'playwright install chromium'") from exc
        try:
            self._manager = sync_playwright().start()
            self._browser = self._manager.chromium.launch(
                headless=not self.config.headed)
            self._context = self._browser.new_context(
                accept_downloads=False, service_workers="block")
            self._context.clear_permissions()
            self._context.route("**/*", self._route)
            self._context.on("page", self._close_extra_page)
            self._page = self._context.new_page()
        except Exception as exc:
            self.close()
            raise BrowserUnavailable(
                "Chromium could not start; run 'playwright install chromium'") from exc

    def _close_extra_page(self, page) -> None:
        if self._page is not None and page is not self._page:
            page.close()

    def _route(self, route) -> None:
        request = route.request
        try:
            if request.method not in {"GET", "HEAD"}:
                route.abort("blockedbyclient")
                return
            if request.is_navigation_request():
                self.policy.validate_navigation(request.url)
            else:
                self.policy.validate_subresource(request.url)
            route.continue_()
        except (URLPolicyError, ValueError):
            route.abort("blockedbyclient")

    def open_page(self, url: str) -> SourcePage:
        if self._page is None:
            raise BrowserUnavailable("browser is not started")
        self.policy.validate_navigation(url)
        try:
            self._page.goto(url, wait_until="domcontentloaded",
                            timeout=int(self.config.page_timeout_seconds * 1000))
            self.policy.validate_navigation(self._page.url)
            title = self._page.title()
            extracted = self._page.evaluate(_PAGE_EXTRACTION_SCRIPT)
        except Exception as exc:
            raise RuntimeError(f"page load failed: {type(exc).__name__}: {exc}") from exc
        self._page_sequence += 1
        snapshot = str(extracted.get("visible_text") or "")
        blocks = [SourceBlock(block_id=f"B{index + 1}",
                              selector=str(item["selector"]),
                              text=str(item["text"]))
                  for index, item in enumerate(
                      (extracted.get("blocks") or [])[:MAX_BLOCKS])]
        links = []
        for raw in (extracted.get("links") or [])[:MAX_LINKS]:
            target = urljoin(self._page.url, str(raw.get("url") or ""))
            try:
                parts = self.policy._parts(target)
            except URLPolicyError:
                continue
            if self.policy._origin(parts) == self.policy.origin:
                links.append(SourceLink(text=str(raw.get("text") or ""),
                                        url=target))
        return SourcePage(
            page_id=f"P{self._page_sequence}", url=self._page.url,
            title=title, visible_text=snapshot,
            visible_text_sha256=_digest(snapshot),
            snapshot_truncated=bool(extracted.get("snapshot_truncated")),
            blocks=blocks, links=links,
        )

    def close(self) -> None:
        for target in (self._context, self._browser):
            if target is not None:
                try:
                    target.close()
                except Exception:
                    pass
        if self._manager is not None:
            try:
                self._manager.stop()
            except Exception:
                pass
        self._page = self._context = self._browser = self._manager = None


_PAGE_EXTRACTION_SCRIPT = r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
           Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const selector = (el) => {
    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE &&
           node !== document.body && parts.length < 8) {
      let part = node.tagName.toLowerCase();
      if (node.id) {
        part += '#' + CSS.escape(node.id);
        parts.unshift(part);
        break;
      }
      const siblings = [...node.parentElement.children]
        .filter(x => x.tagName === node.tagName);
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      parts.unshift(part);
      node = node.parentElement;
    }
    return ['body', ...parts].join(' > ');
  };
  const nodes = [...document.querySelectorAll(
    'h1,h2,h3,h4,h5,h6,p,li,dt,dd,td,th,blockquote,pre')];
  const blocks = [];
  let retainedBlockChars = 0;
  for (const el of nodes) {
    if (!visible(el) || blocks.length >= 2000 || retainedBlockChars >= 1048576) continue;
    const raw = (el.innerText || '').trim();
    if (!raw) continue;
    const text = raw.slice(0, Math.min(20000, 1048576 - retainedBlockChars));
    blocks.push({selector: selector(el), text});
    retainedBlockChars += text.length;
  }
  const links = [...document.querySelectorAll('a[href]')].filter(visible).map(el => ({
    text: (el.innerText || '').trim(),
    url: el.href
  })).slice(0, 200);
  const fullVisibleText = document.body ? document.body.innerText : '';
  return {
    visible_text: fullVisibleText.slice(0, 1048576),
    snapshot_truncated: fullVisibleText.length > 1048576,
    blocks,
    links
  };
}
"""


class AgentMemory:
    """Persistent task state and a separately bounded conversational tail."""

    def __init__(self, question: str, recent_limit: int):
        self.question = question
        self.recent = deque(maxlen=recent_limit * 2)

    def add_exchange(self, assistant: dict[str, Any], tool: dict[str, Any]) -> None:
        self.recent.extend([assistant, tool])

    def build(self, system_prompt: str, state: dict[str, Any],
              token_budget: int) -> list[dict[str, Any]]:
        base = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(
                {"question": self.question, "task_state": state},
                ensure_ascii=False)},
        ]
        tail = list(self.recent)
        while tail and estimate_tokens(json.dumps(base + tail,
                                                  ensure_ascii=False)) > token_budget:
            tail = tail[2:]
        return base + tail


_TOOLS = [
    {"type": "function", "function": {
        "name": "read_page",
        "description": "Read visible text blocks and links from the current page.",
        "parameters": {"type": "object", "properties": {
            "start_block": {"type": "integer", "minimum": 0}},
            "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "search_page",
        "description": "Find visible blocks containing a case-insensitive query.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"],
            "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "follow_link",
        "description": "Open a same-origin link shown by read_page.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"],
            "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "select_evidence",
        "description": "Select exact verbatim quotes from visible source blocks.",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object",
                "properties": {
                    "page_id": {"type": "string"},
                    "block_id": {"type": "string"},
                    "quote": {"type": "string"},
                    "occurrence": {"type": "integer", "minimum": 0}},
                "required": ["page_id", "block_id", "quote"],
                "additionalProperties": False}}},
            "required": ["items"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "finish_evidence",
        "description": "Finish the handoff after collecting enough evidence.",
        "parameters": {"type": "object", "properties": {
            "complete": {"type": "boolean"},
            "note": {"type": "string"}}, "required": ["complete", "note"],
            "additionalProperties": False}}},
]


class EvidenceCollector:
    def __init__(self, config: RunConfig, model: ModelService,
                 browser: BrowserService, log: EventLog,
                 save_page: Callable[[SourcePage], None], deadline: float):
        self.config = config
        self.model = model
        self.browser = browser
        self.log = log
        self.save_page = save_page
        self.deadline = deadline
        self.pages: dict[str, SourcePage] = {}
        self.page_order: list[str] = []
        self.current_page_id = ""
        self.evidence: list[EvidenceItem] = []
        self.memory = AgentMemory(config.question, config.recent_tool_events)
        self.finished = False
        self.complete = False
        self.note = ""
        self.steps = 0
        self.usage: dict[str, int] = {}
        self._last_call_signature = ""
        self._repeat_count = 0

    def _state(self) -> dict[str, Any]:
        current = self.pages.get(self.current_page_id)
        return {
            "current_page": ({"page_id": current.page_id, "url": current.url,
                              "title": current.title}
                             if current else None),
            "visited_pages": [{"page_id": self.pages[p].page_id,
                               "url": self.pages[p].url,
                               "title": self.pages[p].title}
                              for p in self.page_order],
            "evidence": [item.model_dump() for item in self.evidence],
            "limits": {"steps_remaining": self.config.max_steps - self.steps,
                       "pages_remaining": self.config.max_pages - len(self.pages),
                       "evidence_remaining": self.config.max_evidence - len(self.evidence)},
        }

    def _remember_page(self, page: SourcePage) -> None:
        if page.page_id not in self.pages:
            self.pages[page.page_id] = page
            self.page_order.append(page.page_id)
            self.save_page(page)
        self.current_page_id = page.page_id

    def start(self) -> None:
        page = self.browser.open_page(self.config.url)
        self._remember_page(page)
        self.log.emit("agent-1", "page_opened",
                      {"page_id": page.page_id, "url": page.url,
                       "title": page.title, "blocks": len(page.blocks),
                       "snapshot_truncated": page.snapshot_truncated},
                      f"opened {page.url} ({len(page.blocks)} visible blocks)")

    def _read_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start = arguments.get("start_block", 0)
        if not isinstance(start, int) or start < 0:
            raise ValueError("start_block must be a non-negative integer")
        page = self.pages[self.current_page_id]
        chosen = []
        chars = 0
        for block in page.blocks[start:]:
            size = len(block.text)
            if chosen and chars + size > MAX_PROMPT_PAGE_CHARS:
                break
            chosen.append(block.model_dump())
            chars += size
        next_block = start + len(chosen)
        return {"page_id": page.page_id, "url": page.url,
                "title": page.title, "blocks": chosen,
                "links": [link.model_dump() for link in page.links],
                "next_block": next_block if next_block < len(page.blocks) else None,
                "total_blocks": len(page.blocks)}

    def _search_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        page = self.pages[self.current_page_id]
        terms = query.casefold().split()
        matches = [block for block in page.blocks
                   if all(term in block.text.casefold() for term in terms)][:20]
        return {"page_id": page.page_id, "url": page.url,
                "matches": [block.model_dump() for block in matches]}

    def _follow_link(self, arguments: dict[str, Any]) -> dict[str, Any]:
        target = arguments.get("url")
        if not isinstance(target, str):
            raise ValueError("url must be a string")
        current = self.pages[self.current_page_id]
        allowed = {link.url for link in current.links}
        if target not in allowed:
            raise ValueError("url was not one of the links observed on the page")
        if len(self.pages) >= self.config.max_pages:
            raise ValueError("page limit reached")
        if time.monotonic() + self.config.page_timeout_seconds > self.deadline:
            raise ValueError("insufficient run time remains to load another page")
        page = self.browser.open_page(target)
        self._remember_page(page)
        return {"page_id": page.page_id, "url": page.url,
                "title": page.title, "blocks": len(page.blocks)}

    def _select_evidence(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_items = arguments.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("items must be a non-empty array")
        added = []
        for raw in raw_items:
            if len(self.evidence) >= self.config.max_evidence:
                break
            if not isinstance(raw, dict):
                raise ValueError("each selection must be an object")
            page_id = raw.get("page_id")
            block_id = raw.get("block_id")
            quote = raw.get("quote")
            occurrence = raw.get("occurrence", 0)
            if (not isinstance(page_id, str) or not isinstance(block_id, str)
                    or not isinstance(quote, str) or not quote
                    or not isinstance(occurrence, int) or occurrence < 0):
                raise ValueError("invalid evidence selection")
            page = self.pages.get(page_id)
            if page is None:
                raise ValueError(f"unknown page_id {page_id}")
            block = next((item for item in page.blocks
                          if item.block_id == block_id), None)
            if block is None:
                raise ValueError(f"unknown block_id {block_id}")
            positions = []
            cursor = 0
            while True:
                found = block.text.find(quote, cursor)
                if found < 0:
                    break
                positions.append(found)
                cursor = found + max(1, len(quote))
            if occurrence >= len(positions):
                raise ValueError("quote is not a verbatim match in the source block")
            start = positions[occurrence]
            text = quote[:self.config.evidence_char_limit]
            item = EvidenceItem(
                evidence_id=f"E{len(self.evidence) + 1}",
                page_id=page.page_id, url=page.url, text=text,
                selector=block.selector, block_id=block.block_id,
                start_offset=start, end_offset=start + len(text),
                source_sha256=page.visible_text_sha256,
                truncated=len(quote) > len(text),
            )
            self.evidence.append(item)
            added.append(item.model_dump())
        return {"added": added, "evidence_count": len(self.evidence)}

    def _finish(self, arguments: dict[str, Any]) -> dict[str, Any]:
        complete = arguments.get("complete")
        note = arguments.get("note")
        if not isinstance(complete, bool) or not isinstance(note, str):
            raise ValueError("complete must be boolean and note must be string")
        if complete and not self.evidence:
            raise ValueError("cannot mark an empty evidence packet complete")
        self.finished = True
        self.complete = complete
        self.note = note
        return {"finished": True, "complete": complete}

    def _execute(self, call: ToolCall) -> dict[str, Any]:
        handlers = {"read_page": self._read_page,
                    "search_page": self._search_page,
                    "follow_link": self._follow_link,
                    "select_evidence": self._select_evidence,
                    "finish_evidence": self._finish}
        handler = handlers.get(call.name)
        if handler is None:
            raise ValueError(f"unknown tool {call.name}")
        return handler(call.arguments)

    def run(self) -> EvidencePacket:
        prompt = self.config.collector_prompt.format(
            max_evidence=self.config.max_evidence)
        self.start()
        while self.steps < self.config.max_steps and not self.finished:
            if time.monotonic() >= self.deadline:
                self.note = "overall run timeout reached"
                break
            self.steps += 1
            messages = self.memory.build(prompt, self._state(),
                                         self.config.input_token_budget)
            self.log.emit("agent-1", "model_request",
                          {"step": self.steps, "model": self.config.model,
                           "thinking": "disabled", "messages": messages,
                           "estimated_input_tokens": estimate_tokens(
                               json.dumps(messages, ensure_ascii=False))},
                          f"decision {self.steps}/{self.config.max_steps}")
            reply = self.model.complete(
                messages, tools=_TOOLS,
                timeout_seconds=max(0.001, self.deadline - time.monotonic()))
            for key, value in reply.usage.items():
                self.usage[key] = self.usage.get(key, 0) + value
            self.log.emit("agent-1", "model_response",
                          {"step": self.steps, "content": reply.content,
                           "tool_calls": [call.model_dump()
                                          for call in reply.tool_calls],
                           "usage": reply.usage,
                           "finish_reason": reply.finish_reason})
            if not reply.tool_calls:
                result: dict[str, Any] = {
                    "error": "the model returned no tool call"}
                assistant = {"role": "assistant",
                             "content": reply.content or ""}
                tool = {"role": "user", "content": json.dumps(result)}
            else:
                call = reply.tool_calls[0]
                signature = json.dumps(
                    {"name": call.name, "arguments": call.arguments},
                    sort_keys=True, ensure_ascii=False)
                if signature == self._last_call_signature:
                    self._repeat_count += 1
                else:
                    self._last_call_signature = signature
                    self._repeat_count = 1
                if self._repeat_count >= 3:
                    self.finished = True
                    self.complete = False
                    self.note = "repeated identical tool call detected"
                    result = {"error": self.note}
                    message = f"{call.name} repetition stopped"
                else:
                    try:
                        result = self._execute(call)
                        message = f"{call.name} completed"
                    except Exception as exc:
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                        message = f"{call.name} failed"
                self.log.emit("agent-1", "tool_result",
                              {"step": self.steps, "tool": call.name,
                               "call_id": call.call_id, "result": result},
                              message)
                assistant = {"role": "assistant", "content": reply.content,
                             "tool_calls": [{"id": call.call_id,
                                "type": "function", "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments,
                                                            ensure_ascii=False)}}]}
                tool = {"role": "tool", "tool_call_id": call.call_id,
                        "content": json.dumps(result, ensure_ascii=False)}
            self.memory.add_exchange(assistant, tool)
        if not self.finished:
            self.complete = False
            if not self.note:
                self.note = "agent step limit reached"
        return EvidencePacket(question=self.config.question,
                              items=list(self.evidence),
                              complete=self.complete,
                              completion_note=self.note)


class _AnswerDraft(BaseModel):
    text: str = Field(alias="answer")
    citations: list[dict[str, str]]


class AnswerGenerator:
    def __init__(self, config: RunConfig, model: ModelService, log: EventLog,
                 deadline: float):
        self.config = config
        self.model = model
        self.log = log
        self.deadline = deadline
        self.usage: dict[str, int] = {}

    def run(self, packet: EvidencePacket) -> Answer:
        payload = {"question": self.config.question,
                   "evidence_packet": packet.model_dump()}
        messages = [
            {"role": "system", "content": self.config.answerer_prompt},
            {"role": "user", "content": json.dumps(payload,
                                                      ensure_ascii=False)},
        ]
        valid_ids = {item.evidence_id: item for item in packet.items}
        last_problem = ""
        for attempt in range(2):
            request = list(messages)
            if attempt:
                request.append({"role": "user", "content":
                                "Return corrected JSON. Validation error: "
                                + last_problem})
            estimated = estimate_tokens(json.dumps(request,
                                                   ensure_ascii=False))
            if estimated > self.config.input_token_budget:
                raise RuntimeError("Agent 2 input exceeds its token budget")
            self.log.emit("agent-2", "model_request",
                          {"attempt": attempt + 1, "model": self.config.model,
                           "thinking": "disabled", "messages": request,
                           "estimated_input_tokens": estimated},
                          f"answer attempt {attempt + 1}/2")
            if time.monotonic() >= self.deadline:
                raise RuntimeError("overall run timeout reached during Agent 2")
            reply = self.model.complete(
                request, json_mode=True,
                timeout_seconds=max(0.001, self.deadline - time.monotonic()))
            for key, value in reply.usage.items():
                self.usage[key] = self.usage.get(key, 0) + value
            self.log.emit("agent-2", "model_response",
                          {"attempt": attempt + 1, "content": reply.content,
                           "usage": reply.usage,
                           "finish_reason": reply.finish_reason})
            try:
                draft = _AnswerDraft.model_validate_json(reply.content or "")
                cited = [entry.get("evidence_id")
                         for entry in draft.citations]
                if not cited:
                    raise ValueError("at least one citation is required")
                unknown = [item for item in cited if item not in valid_ids]
                if unknown:
                    raise ValueError(f"unknown evidence IDs: {unknown}")
                citations = [Citation(evidence_id=item,
                                      url=valid_ids[item].url)
                             for item in cited]
                return Answer(text=draft.text, citations=citations)
            except (ValidationError, ValueError) as exc:
                last_problem = str(exc)
                self.log.emit("agent-2", "answer_validation_failed",
                              {"attempt": attempt + 1,
                               "problem": last_problem},
                              "structured answer was invalid")
        raise RuntimeError("Agent 2 returned invalid structured output twice: "
                           + last_problem)


def _write_json(path: Path, value: Any) -> None:
    if isinstance(value, BaseModel):
        text = value.model_dump_json(indent=2)
    else:
        text = json.dumps(value, indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def run_web_evidence(config: RunConfig, out_dir: str,
                     *, model: ModelService | None = None,
                     browser: BrowserService | None = None,
                     renderer: TerminalRenderer | None = None
                     ) -> tuple[Path, RunResult]:
    """Run the two-agent baseline and write inspectable artifacts."""
    output = Path(out_dir)
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise ValueError(f"output must be a real directory: {output}")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    pages_dir = output / "pages"
    pages_dir.mkdir(exist_ok=True)
    events_path = output / "events.jsonl"
    events_path.write_text("", encoding="utf-8")
    _write_json(output / "config.json", config)
    log = EventLog(events_path, renderer)
    run_id = "web-" + uuid.uuid4().hex
    service = model
    web = browser
    owned_model = model is None
    deadline = time.monotonic() + config.run_timeout_seconds
    collector: EvidenceCollector | None = None
    packet: EvidencePacket | None = None
    answer: Answer | None = None
    status: Literal["completed", "partial", "limit_reached", "error"] = "error"
    stop_reason = "run did not start"

    def save_page(page: SourcePage) -> None:
        _write_json(pages_dir / f"{page.page_id}.json", page)

    log.emit("runner", "run_started",
             {"run_id": run_id, "workflow_version": WORKFLOW_VERSION,
              "config": config.model_dump(exclude={"collector_prompt",
                                                    "answerer_prompt"})},
             f"run {run_id} started")
    try:
        service = service or DeepSeekClient(config.model, config.base_url)
        web = web or PlaywrightBrowser(config)
        web.start()
        collector = EvidenceCollector(config, service, web, log, save_page,
                                      deadline)
        packet = collector.run()
        _write_json(output / "handoff.json", packet)
        log.emit("runner", "evidence_handoff",
                 {"packet": packet.model_dump()},
                 f"handed {len(packet.items)} evidence items to Agent 2")
        if not packet.items:
            status = "limit_reached" if "limit" in packet.completion_note else "partial"
            stop_reason = packet.completion_note or "no evidence was collected"
        elif time.monotonic() >= deadline:
            status = "limit_reached"
            stop_reason = "overall run timeout reached before Agent 2"
        else:
            generator = AnswerGenerator(config, service, log, deadline)
            answer = generator.run(packet)
            usage = dict(collector.usage)
            for key, value in generator.usage.items():
                usage[key] = usage.get(key, 0) + value
            status = "completed" if packet.complete else "partial"
            stop_reason = packet.completion_note or (
                "Agent 2 returned a structurally valid cited answer")
            result = RunResult(
                run_id=run_id, status=status, stop_reason=stop_reason,
                question=config.question, answer=answer,
                evidence_count=len(packet.items), pages_visited=len(collector.pages),
                steps_used=collector.steps, token_usage=usage,
                artifacts={"config": "config.json", "events": "events.jsonl",
                           "handoff": "handoff.json", "pages": "pages/",
                           "result": "result.json"},
            )
            _write_json(output / "result.json", result)
            log.emit("runner", "run_finished", result.model_dump(),
                     f"{status}: {stop_reason}")
            return output, result
    except Exception as exc:
        status = "error"
        stop_reason = f"{type(exc).__name__}: {exc}"
        log.emit("runner", "run_error",
                 {"type": type(exc).__name__, "message": str(exc)},
                 stop_reason)
    finally:
        if web is not None:
            web.close()
        if owned_model and isinstance(service, DeepSeekClient):
            service.close()

    usage = dict(collector.usage) if collector else {}
    result = RunResult(
        run_id=run_id, status=status, stop_reason=stop_reason,
        question=config.question, answer=answer,
        evidence_count=len(packet.items) if packet else 0,
        pages_visited=len(collector.pages) if collector else 0,
        steps_used=collector.steps if collector else 0,
        token_usage=usage,
        artifacts={"config": "config.json", "events": "events.jsonl",
                   "handoff": "handoff.json" if packet else "not_written",
                   "pages": "pages/", "result": "result.json"},
    )
    _write_json(output / "result.json", result)
    return output, result
