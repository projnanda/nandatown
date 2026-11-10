"""JANG-compatible canonical JSON, Ed25519 mandate signing and agreement checks.

JANG (https://github.com/byujan/JANGmarket) signs and verifies over
``JSON.stringify`` of a key-sorted copy of the object. A Python owner can only
lodge a mandate JANG accepts if it reproduces those bytes exactly, so this
module is the cross-language compatibility surface under test.

Two signers share one interface. ``PythonSigner`` (default) signs natively.
``TsSigner`` shells out to JANG's own ``signMandate`` through ``npx tsx``; it
needs ``JANG_REPO`` and Node and exists as a fallback and a cross-check.
Choose with ``JANG_SIGNER=python|ts``.
"""

from __future__ import annotations

import base64
import json
import math
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

MANDATE_BODY_KEYS = (
    "mandateId", "agentId", "ownerId", "role", "itemId", "constraints",
    "preferences", "reservationUtility", "expiresAt", "disclosePriorities",
)
AGREEMENT_ENVELOPE_KEYS = ("algorithm", "jangPublicKey", "signature")


# -- canonical JSON ------------------------------------------------------


def _js_number(value: float | int) -> str:
    """Format a number the way JavaScript's JSON.stringify does."""
    if isinstance(value, bool):
        raise TypeError("booleans are not JANG numbers")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError("non-finite numbers cannot be signed")
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    text = repr(value)  # shortest round-trip, like JS
    if "e" in text:
        mantissa, exp = text.split("e")
        sign = "-" if exp.startswith("-") else "+"
        text = f"{mantissa}e{sign}{int(exp.lstrip('+-'))}"
    return text


def _js_string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_key(key: str) -> list[int]:
    data = key.encode("utf-16-be")
    return [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]


def canonical_json(value: Any) -> str:
    """Byte-for-byte equivalent of JANG's ``canonicalJson`` (canonical.ts)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _js_number(value)
    if isinstance(value, str):
        return _js_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value, key=_utf16_key):  # JS sorts by UTF-16 units
            if value[key] is None:
                continue  # JANG drops undefined; its schemas never allow null
            parts.append(_js_string(key) + ":" + canonical_json(value[key]))
        return "{" + ",".join(parts) + "}"
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


# -- keys and signatures -------------------------------------------------


@dataclass(frozen=True)
class OwnerKey:
    private_pem: str
    public_pem: str

    @classmethod
    def generate(cls) -> "OwnerKey":
        key = Ed25519PrivateKey.generate()
        private_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        return cls(private_pem, public_pem)


def sign_payload(payload: Any, private_pem: str) -> str:
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return base64.b64encode(key.sign(canonical_json(payload).encode())).decode()


def verify_payload(payload: Any, signature_b64: str, public_pem: str) -> bool:
    try:
        key = serialization.load_pem_public_key(public_pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(base64.b64decode(signature_b64, validate=True),
                   canonical_json(payload).encode())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def mandate_body(mandate: dict[str, Any]) -> dict[str, Any]:
    return {k: mandate[k] for k in MANDATE_BODY_KEYS if mandate.get(k) is not None}


class PythonSigner:
    name = "python"

    def sign_mandate(self, body: dict[str, Any], owner: OwnerKey) -> dict[str, Any]:
        clean = mandate_body(body)
        return {**clean, "ownerPublicKey": owner.public_pem,
                "signature": sign_payload(clean, owner.private_pem)}


class TsSigner:
    """Delegate to JANG's own signMandate. Needs JANG_REPO and Node."""

    name = "ts"
    _SCRIPT = (
        "import { signMandate } from './src/lib/jang/mandate.ts';"
        "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{"
        "const {body,priv,pub}=JSON.parse(s);"
        "process.stdout.write(JSON.stringify(signMandate(body,priv,pub)));});"
    )

    def __init__(self, repo: str | None = None):
        self.repo = repo or os.environ.get("JANG_REPO", "")
        if not self.repo:
            raise RuntimeError("TsSigner needs JANG_REPO pointing at a JANGmarket checkout")

    def sign_mandate(self, body: dict[str, Any], owner: OwnerKey) -> dict[str, Any]:
        payload = json.dumps({"body": mandate_body(body), "priv": owner.private_pem,
                              "pub": owner.public_pem})
        out = subprocess.run(["npx", "tsx", "-e", self._SCRIPT], cwd=self.repo,
                             input=payload, capture_output=True, text=True,
                             check=True, timeout=60)
        return json.loads(out.stdout)


def signer_from_env() -> PythonSigner | TsSigner:
    return TsSigner() if os.environ.get("JANG_SIGNER") == "ts" else PythonSigner()


# -- agreements ----------------------------------------------------------


def verify_agreement(agreement: dict[str, Any], trusted_jang_pem: str) -> bool:
    """Signature check against the key the caller trusts, never the embedded one."""
    if not isinstance(agreement, dict) or "signature" not in agreement:
        return False
    body = {k: v for k, v in agreement.items() if k not in AGREEMENT_ENVELOPE_KEYS}
    return verify_payload(body, agreement["signature"], trusted_jang_pem)
