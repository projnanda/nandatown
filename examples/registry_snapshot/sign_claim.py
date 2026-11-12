"""Sign a registry claim once, outside any run.

    python sign_claim.py --skey controller.skey --agent provider-current \
        --identity <identity id> --registry <registry id>
    python sign_claim.py --seed-hex <64 hex> ...

Prints the claim a scenario puts in an agent's config:
{"identity": ..., "controller_pub": ..., "signature": ...}, a signature by
the controller key over the canonical statement

    {"statement": "registry-claim/v1", "agent": ..., "identity": ...,
     "registry": ...}

This is an off-chain signature: no transaction, nothing moves. The private
key is read here and never enters a Lab run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from nandatown.records import canonical_json

STATEMENT = "registry-claim/v1"


def load_key(skey: str | None, seed_hex: str | None) -> Ed25519PrivateKey:
    if seed_hex:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))
    cbor = json.loads(Path(skey).read_text())["cborHex"]
    if not (cbor.startswith("5820") and len(cbor) == 68):
        raise SystemExit(f"{skey}: not a 32-byte Ed25519 signing key")
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(cbor[4:]))


def sign_claim(sk: Ed25519PrivateKey, agent: str, identity: str,
               registry: str) -> dict:
    statement = {"statement": STATEMENT, "agent": agent, "identity": identity,
                 "registry": registry}
    pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return {"identity": identity, "controller_pub": pub,
            "signature": sk.sign(canonical_json(statement).encode()).hex()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    key = ap.add_mutually_exclusive_group(required=True)
    key.add_argument("--skey")
    key.add_argument("--seed-hex")
    ap.add_argument("--agent", required=True)
    ap.add_argument("--identity", required=True)
    ap.add_argument("--registry", required=True)
    a = ap.parse_args()
    print(json.dumps(sign_claim(load_key(a.skey, a.seed_hex), a.agent,
                                a.identity, a.registry)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
