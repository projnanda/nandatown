"""Regenerate the mock data: snapshots/mock.json and the two mock scenarios.

    python make_mock.py

Everything derives from mock_keystore.json (seeds that were never funded and
are used on no network), so the committed files are reproducible byte for
byte; the tests check that. Identity, registry and transaction ids are
labelled mock so they cannot be mistaken for chain data.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from sign_claim import sign_claim

HERE = Path(__file__).resolve().parent
IDENTITY = "mock:rotation-demo"
REGISTRY = "mock-registry"


def keys() -> dict[str, Ed25519PrivateKey]:
    seeds = json.loads((HERE / "mock_keystore.json").read_text())["keys"]
    return {k: Ed25519PrivateKey.from_private_bytes(bytes.fromhex(v))
            for k, v in seeds.items()}


def pkh(sk: Ed25519PrivateKey) -> str:
    pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.blake2b(pub, digest_size=28).hexdigest()


def mock_tx(label: str) -> str:
    return "mock-" + hashlib.sha256(label.encode()).hexdigest()[:20]


def snapshot() -> dict:
    k = keys()
    return {
        "schema": "registry-snapshot/v1",
        "source": "mock",
        "network": {"name": "mock"},
        "registry": REGISTRY,
        "key_hash": "blake2b-224 of the raw Ed25519 public key",
        "tip": {"slot": 1000, "block_height": 500, "block_hash": mock_tx("tip")},
        "identities": {IDENTITY: {
            "status": "active", "owner_pkh": pkh(k["k2"]),
            "registry_ref": mock_tx("rotate") + "#0",
            "history": [
                {"event": "registered", "slot": 100, "block_height": 50,
                 "tx": mock_tx("register"), "owner_pkh": pkh(k["k1"])},
                {"event": "controller_rotated", "slot": 400, "block_height": 200,
                 "tx": mock_tx("rotate"), "from_pkh": pkh(k["k1"]),
                 "to_pkh": pkh(k["k2"])},
            ]}},
    }


def scenario(name: str, registry: str, description: str) -> str:
    k = keys()
    current = sign_claim(k["k2"], "provider-current", IDENTITY, REGISTRY)
    former = sign_claim(k["k1"], "provider-former", IDENTITY, REGISTRY)
    claim = lambda c: json.dumps(c)  # noqa: E731
    return f"""name: {name}
description: {json.dumps(description)}
seed: 42
validator: registry_rotation
plugin_files: [../registry_snapshot.py]
layers:
  identity: claims.v1
  auth: ed25519.v1
  registry: {registry}
agents:
  - name: provider-current
    role: seller
    config: {{sku: text-stats, ask_cents: 1995, floor_cents: 1700, stock: 10, balance_cents: 0, claim: {claim(current)}}}
  - name: provider-former
    role: former_controller
    config: {{sku: text-stats, ask_cents: 1500, floor_cents: 1200, stock: 10, balance_cents: 0, claim: {claim(former)}}}
  - name: buyer-1
    role: buyer
    config: {{sku: text-stats, quantity: 1, cap_cents: 1900, balance_cents: 10000, rounds: 1}}
max_time: 60
"""


FILES = {
    "snapshots/mock.json": lambda: json.dumps(snapshot(), indent=2, sort_keys=True) + "\n",
    "scenarios/rotation_mock.yaml": lambda: scenario(
        "rotation_mock", "snapshot.mock.v1",
        "MOCK snapshot. Two providers claim mock:rotation-demo. The snapshot records"
        " that control moved from K1 to K2. The K1 provider's claim and town"
        " signatures are valid; its claim is refused on the record before any"
        " traffic, and it gets no traffic and no money. The buyer trades with K2."),
    "scenarios/rotation_mock_sigonly_control.yaml": lambda: scenario(
        "rotation_mock_sigonly_control", "snapshot.mock.sigonly.v1",
        "MOCK control. The same run with record comparison off: every claim"
        " signature is still checked, the snapshot record is not. K1 undercuts,"
        " is picked and paid, and the run fails on purpose."),
}


def main() -> None:
    for rel, render in FILES.items():
        (HERE / rel).write_text(render(), encoding="utf-8", newline="\n")
        print("wrote", rel)


if __name__ == "__main__":
    main()
