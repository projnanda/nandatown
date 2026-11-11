#!/usr/bin/env python3
"""Drive Cerata's MCP endpoint the way an MCP host (Claude, any agent) would.

Cerata serves MCP as stateless Streamable HTTP at POST {CERATA_URL}/mcp with
JSON responses. This script initializes, lists the tools, calls `preflight`
for one case through MCP (with an idempotency key, twice, to show the replay),
and verifies the returned receipt through MCP's `verify` tool. It compares
each step with the expected result and prints one JSON line per step plus a
summary; the exit status is 0 only if every step matched.

    CERATA_URL=... CERATA_API_KEY=... python mcp_check.py [CASE_ID] [OUT.json]

`nandatown mcp test` probes stdio MCP servers; to point it at the hosted
endpoint, bridge it, for example with `npx -y mcp-remote "$CERATA_URL/mcp"
--header "Authorization:${CERATA_AUTH_HEADER}"` (keep the key in the
environment, not in the command text).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
EXPECTED_TOOLS = {"preflight", "approve_campaign", "ask_coverage", "explain_rule", "get_hold_status", "verify"}


def rpc(url: str, key: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    msg = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:8], "method": method, "params": params or {}}
    req = urllib.request.Request(
        url.rstrip("/") + "/mcp", data=json.dumps(msg).encode(), method="POST",
        headers={"content-type": "application/json", "accept": "application/json, text/event-stream",
                 "authorization": f"Bearer {key}", "mcp-protocol-version": "2025-06-18"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        return {"http_status": exc.code}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"error": type(exc).__name__}
    if raw.lstrip().startswith("event:") or raw.lstrip().startswith("data:"):
        raw = next(l[5:] for l in raw.splitlines() if l.startswith("data:"))
    return json.loads(raw)


def tool_json(resp: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(resp["result"]["content"][0]["text"])
    except (KeyError, IndexError, ValueError, TypeError):
        return {}


def main() -> int:
    url = os.environ.get("CERATA_URL", "https://clearance-kohl.vercel.app")
    key = os.environ.get("CERATA_API_KEY", "")
    if not key:
        print("set CERATA_API_KEY", file=sys.stderr)
        return 2
    case_id = sys.argv[1] if len(sys.argv) > 1 else "call-national-dnc"
    out_path = sys.argv[2] if len(sys.argv) > 2 else None
    with open(os.path.join(HERE, "cases.json")) as f:
        case = next(c for c in json.load(f)["cases"] if c["id"] == case_id)

    steps: list[dict[str, Any]] = []

    def step(name: str, ok: bool, **detail: Any) -> None:
        steps.append({"step": name, "ok": bool(ok), **detail})
        print(json.dumps(steps[-1]))

    init = rpc(url, key, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "nandatown-cerata-check", "version": "1"}})
    step("initialize", "result" in init, server=(init.get("result") or {}).get("serverInfo"))
    tools = {t["name"] for t in ((rpc(url, key, "tools/list").get("result") or {}).get("tools") or [])}
    step("tools/list", EXPECTED_TOOLS <= tools, tools=sorted(tools))

    idem = "mcp-" + uuid.uuid4().hex[:24]
    args = {**case["request"], "idempotency_key": idem}
    first = tool_json(rpc(url, key, "tools/call", {"name": "preflight", "arguments": args}))
    want = case["expect"]
    decided_ok = (first.get("decision") in ("block", "hold", "flag")) if want.get("never_clear") else first.get("decision") == want.get("decision")
    step("tools/call preflight", decided_ok, case=case_id, decision=first.get("decision"),
         expected=want.get("decision", "not clear"), receipt_id=first.get("receipt_id"))
    again = tool_json(rpc(url, key, "tools/call", {"name": "preflight", "arguments": args}))
    step("tools/call preflight (same idempotency key)",
         bool(first) and again.get("check_id") == first.get("check_id") and again.get("receipt_id") == first.get("receipt_id"),
         check_id=again.get("check_id"))
    if first.get("receipt"):
        v = tool_json(rpc(url, key, "tools/call", {"name": "verify", "arguments": {"receipt": first["receipt"]}}))
        step("tools/call verify", v.get("valid") is True, kind=v.get("kind"))
    bad = rpc(url, "ak_test_not_a_real_key_000", "tools/list")
    step("refused without a valid key", bad.get("http_status") == 401, http_status=bad.get("http_status"))

    ok = all(s["ok"] for s in steps)
    print(json.dumps({"mcp_check": "PASSED" if ok else "FAILED", "steps": len(steps)}))
    if out_path:
        with open(out_path, "w") as f:
            json.dump({"url_host": url.split("//")[-1].split("/")[0], "case": case_id, "steps": steps,
                       "result": "PASSED" if ok else "FAILED"}, f, indent=1)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
