"""The TrailProof link: how a Town participant talks to the TrailProof ledger.

TrailProof (https://github.com/Andreyivvi/TrailProof) is an append-only
commitment ledger for agent purchases, exposed as an MCP server. Each party
locks ``sha256(canonical_json(payload))`` of what it saw at every step of an
order, signs ``commit|prev|refs`` with its Ed25519 key, and the ledger
countersigns ``event_id|commit|ledger_ts``. Plaintext stays with the parties.

This module is deliberately independent of the TrailProof package: it speaks
MCP streamable HTTP with the standard library and re-implements the
canonical-JSON, commitment and signing-message conventions from the
TrailProof README, so a passing run also shows that a third-party client
built from the documented contract interoperates with the server.

Only ``cryptography`` (already a Town dependency) is used, for Ed25519.

Environment:
  TRAILPROOF_URL     the ledger's MCP endpoint, e.g. http://127.0.0.1:8765/mcp
  TRAILPROOF_HOME    a TrailProof checkout; ``start_server`` runs it with
                     TRAILPROOF_PYTHON or its ``.venv`` interpreter
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PROTOCOL_VERSION = "2025-06-18"


class LedgerUnreachable(Exception):
    """Transport failure. TrailProof clients fail open: the purchase goes on
    without a receipt, and says so."""


class ToolError(Exception):
    """The ledger answered, and refused: ``{"error": CODE, "detail": ...}``."""

    def __init__(self, result: dict[str, Any]):
        super().__init__(f"{result.get('error')}: {result.get('detail', '')}")
        self.code = result.get("error")
        self.detail = result.get("detail", "")
        self.result = result


# --------------------------------------------------------------------------
# TrailProof conventions (README, "How an order looks on the ledger")
# --------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def commit(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def event_signing_message(commit_hex: str, prev: str | None, refs: list[str]) -> str:
    return f"{commit_hex}|{prev or ''}|{','.join(refs)}"


def receipt_signing_message(event_id: str, commit_hex: str, ledger_ts: str) -> str:
    return f"{event_id}|{commit_hex}|{ledger_ts}"


def cents_to_amount(cents: int) -> str:
    """Town prices are integer cents; TrailProof amounts are 2-dp strings."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def iso_z(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"


class Keypair:
    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()

    @property
    def public_hex(self) -> str:
        return self.private.public_key().public_bytes_raw().hex()

    def sign(self, message: str) -> str:
        return self.private.sign(message.encode("utf-8")).hex()


def verify_signature(public_hex: str, message: str, signature_hex: str) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        key.verify(bytes.fromhex(signature_hex), message.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_receipt(ledger_public_hex: str, receipt: dict[str, Any], commit_hex: str) -> bool:
    """Anyone holding the ledger's public key can check a receipt offline."""
    try:
        return verify_signature(
            ledger_public_hex,
            receipt_signing_message(receipt["event_id"], commit_hex, receipt["ledger_ts"]),
            receipt["ledger_sig"],
        )
    except (KeyError, TypeError):
        return False


# --------------------------------------------------------------------------
# A minimal MCP streamable-HTTP client (JSON-RPC 2.0 over POST)
# --------------------------------------------------------------------------


class TrailProofClient:
    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout
        self._id = 0
        self.calls: list[str] = []

    def _post(self, message: dict[str, Any]) -> dict[str, Any] | None:
        req = urllib.request.Request(self.url, data=json.dumps(message).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise LedgerUnreachable(f"HTTP {exc.code} from {self.url}") from exc
        except (urllib.error.URLError, OSError, socket.timeout) as exc:
            raise LedgerUnreachable(f"{self.url}: {exc}") from exc
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        reply = self._post(message)
        if reply is None:
            raise LedgerUnreachable(f"empty reply to {method}")
        if "error" in reply:
            raise LedgerUnreachable(f"{method}: JSON-RPC error {reply['error']}")
        return reply["result"]

    def initialize(self) -> dict[str, Any]:
        info = self.rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                       "clientInfo": {"name": "nandatown-trailproof-link", "version": "0.1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return info

    def list_tools(self) -> list[str]:
        return [t["name"] for t in self.rpc("tools/list", {})["tools"]]

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """Call a TrailProof tool. Returns its dict; a ``{"error": ...}`` answer raises ToolError."""
        self.calls.append(tool)
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        payload = result.get("structuredContent")
        if payload is None:
            texts = [c.get("text") for c in result.get("content", []) if c.get("type") == "text"]
            payload = json.loads(texts[0]) if texts else {}
        if result.get("isError"):
            raise LedgerUnreachable(f"{tool}: tool error {payload}")
        if isinstance(payload, dict) and "error" in payload:
            raise ToolError(payload)
        return payload


# --------------------------------------------------------------------------
# A party: one key, one party_id, plaintexts kept locally
# --------------------------------------------------------------------------


class Party:
    def __init__(self, client: TrailProofClient, role: str, operator: str):
        self.client = client
        self.role = role
        self.operator = operator
        self.kp = Keypair()
        self.party_id: str | None = None
        self.ledger_public_key: str | None = None
        self.vault: dict[str, dict[str, Any]] = {}  # event_id -> payload

    def register(self) -> str:
        res = self.client.call("register_party", role=self.role, public_key=self.kp.public_hex, operator=self.operator)
        self.party_id = res["party_id"]
        self.ledger_public_key = res["ledger_public_key"]
        return self.party_id

    def head(self, order_id: str) -> str | None:
        return self.client.call("get_trail", order_id=order_id, party_id=self.party_id)["head"]

    def trail(self, order_id: str) -> dict[str, Any]:
        return self.client.call("get_trail", order_id=order_id, party_id=self.party_id)

    def record(self, order_id: str, type_: str, payload: dict[str, Any], prev: str | None,
               refs: list[str] | None = None) -> dict[str, Any]:
        """Commit ``payload`` as one event, stating ``prev`` explicitly.

        Stating prev is what makes a stale view fail loudly (BAD_PREV / BAD_SIG)
        instead of silently appending behind someone else's event.
        """
        c = commit(payload)
        sig = self.kp.sign(event_signing_message(c, prev, refs or []))
        receipt = self.client.call("record_event", order_id=order_id, type=type_, party_id=self.party_id,
                                   commit=c, refs=refs or [], sig=sig, prev=prev)
        self.vault[receipt["event_id"]] = payload
        return {"commit": c, "receipt": receipt}

    def check_order(self, order_id: str, mandate: dict[str, Any], offer: dict[str, Any],
                    prev: str | None, offer_commit: str | None = None) -> dict[str, Any]:
        """The buyer agent's scope check: both plaintexts go to the ledger, which
        verifies them against the committed hashes, runs the mandate rules and,
        if in scope, records ORDER.

        ``offer_commit`` is the hash the seller *says* it committed; passing it
        (rather than recomputing from the plaintext) lets the ledger report a
        plaintext that does not match that commitment as COMMIT_MISMATCH."""
        mc, oc = commit(mandate), offer_commit or commit(offer)
        qty, total, currency = offer["qty"], offer["total"], offer["currency"]
        order_payload = {"mandate_commit": mc, "offer_commit": oc, "qty": qty, "total": total, "currency": currency}
        sig = self.kp.sign(event_signing_message(commit(order_payload), prev, [mc, oc]))
        res = self.client.call("check_order", order_id=order_id, party_id=self.party_id, mandate_payload=mandate,
                               mandate_commit=mc, offer_payload=offer, offer_commit=oc, qty=qty, total=total,
                               currency=currency, sig=sig)
        if res.get("receipt"):
            self.vault[res["receipt"]["event_id"]] = order_payload
        return res


# --------------------------------------------------------------------------
# Running the ledger (for the check script and the tests)
# --------------------------------------------------------------------------


def trailproof_python(home: str | None = None) -> str | None:
    """The interpreter that has TrailProof installed, or None."""
    explicit = os.environ.get("TRAILPROOF_PYTHON")
    if explicit:
        return explicit
    home = home or os.environ.get("TRAILPROOF_HOME")
    if not home:
        return None
    for candidate in (os.path.join(home, ".venv", "Scripts", "python.exe"),
                      os.path.join(home, ".venv", "bin", "python")):
        if os.path.exists(candidate):
            return candidate
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(home: str | None = None, port: int | None = None, db_path: str | None = None,
                 timeout: float = 20.0) -> tuple[subprocess.Popen, str]:
    """Start ``python -m trailproof.server --http`` from a TrailProof checkout.

    Returns ``(process, url)``; the caller terminates the process. The ledger
    key is generated by that process, so every participant of a run must use
    this one server, which is the point: one shared ledger.
    """
    home = home or os.environ.get("TRAILPROOF_HOME")
    python = trailproof_python(home)
    if not home or not python:
        raise RuntimeError("set TRAILPROOF_HOME to a TrailProof checkout (and TRAILPROOF_PYTHON if it has no .venv)")
    port = port or free_port()
    env = dict(os.environ)
    env["TRAILPROOF_DB"] = db_path or ":memory:"
    env.setdefault("PYTHONUTF8", "1")
    process = subprocess.Popen([python, "-m", "trailproof.server", "--http", "--port", str(port)], cwd=home, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=(os.name == "posix"))
    url = f"http://127.0.0.1:{port}/mcp"
    client = TrailProofClient(url, timeout=3.0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"TrailProof server exited with {process.returncode}")
        try:
            client.initialize()
            return process, url
        except LedgerUnreachable:
            time.sleep(0.2)
    process.terminate()
    raise RuntimeError("TrailProof server did not come up in time")


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


if __name__ == "__main__":  # a tiny probe: python trailproof_link.py http://127.0.0.1:8765/mcp
    c = TrailProofClient(sys.argv[1] if len(sys.argv) > 1 else os.environ["TRAILPROOF_URL"])
    print(json.dumps({"server": c.initialize().get("serverInfo"), "tools": c.list_tools()}, indent=2))
