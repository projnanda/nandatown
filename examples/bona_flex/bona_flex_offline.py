"""Offline verification helpers for Bona receipts and the HW8 sidecar. PURE: standard library + `cryptography`
(already a NandaTown dependency). No `bona` or `bench` import.

- `canonical(obj)`: RFC 8785 (JCS) for the JSON Bona signs, which by Bona's rule holds integers, strings, booleans,
  null, lists and objects only. A float anywhere is refused (ValueError), so this can never silently disagree with
  a full JCS implementation on number formatting. Keys sort by UTF-16 code units, as JCS requires.
- `verify_receipt(receipt, did)`: Ed25519 signature over canonical(receipt minus "signature") with the did:key.
- `sha256_bytes`, `file_sha256`, `dump_bytes`: the sidecar's byte conventions (UTF-8, LF, sorted keys).
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _no_floats(x) -> None:
    if isinstance(x, float):
        raise ValueError("float in signed JSON (Bona signs integers only)")
    if isinstance(x, dict):
        for k, v in x.items():
            if not isinstance(k, str):
                raise ValueError("non-string key")
            _no_floats(v)
    elif isinstance(x, list):
        for v in x:
            _no_floats(v)


def _jcs(x) -> str:
    if isinstance(x, dict):
        items = sorted(x.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _jcs(v) for k, v in items) + "}"
    if isinstance(x, list):
        return "[" + ",".join(_jcs(v) for v in x) + "]"
    return json.dumps(x, ensure_ascii=False)


def canonical(obj) -> bytes:
    _no_floats(obj)
    return _jcs(obj).encode("utf-8")


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + raw


def did_public_key(did: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not did.startswith("did:key:z"):
        raise ValueError("not a did:key")
    data = _b58decode(did.split("#")[0][len("did:key:z"):])
    if data[:2] != b"\xed\x01":
        raise ValueError("not an Ed25519 did:key")
    return Ed25519PublicKey.from_public_bytes(data[2:])


def key_id_for(did: str) -> str:
    return f"{did}#{did[len('did:key:'):]}"


def verify_receipt(receipt: dict, did: str, expected_env: str = "bench") -> bool:
    from cryptography.exceptions import InvalidSignature
    try:
        if receipt.get("env") != expected_env or receipt.get("key_id") != key_id_for(did):
            return False
        sig = receipt["signature"]
        body = {k: v for k, v in receipt.items() if k != "signature"}
        did_public_key(did).verify(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)), canonical(body))
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False


def sign_with(private_key, obj: dict) -> str:
    """Seller-side offer signature (b64url, no padding) over canonical(obj without 'signature')."""
    body = {k: v for k, v in obj.items() if k != "signature"}
    return base64.urlsafe_b64encode(private_key.sign(canonical(body))).decode().rstrip("=")


def dump_bytes(obj) -> bytes:
    return (json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def file_sha256(p: pathlib.Path) -> str:
    return sha256_bytes(pathlib.Path(p).read_bytes())


def receipt_sha256(receipt: dict) -> str:
    """Hash of a receipt as stored in the sidecar (receipts/<seq>.json bytes)."""
    return sha256_bytes(dump_bytes(receipt))
