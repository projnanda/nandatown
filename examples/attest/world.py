"""Seed an Attest gate with a small cast for the Town compatibility check.

Attest (https://github.com/smghaas/attest) is a Know-Your-Agent gate: a
receiving agent asks it whether a sender is who it claims, is staked, and is
inside its mandate. This file builds the world the check runs against, using
the Attest checkout's own helpers (ATTEST_HOME), and writes it to disk so the
gate server and the Town participants share it:

    ATTEST_HOME=... python examples/attest/world.py WORKDIR

WORKDIR/gate/gate_state.json   state the gate server loads
WORKDIR/cast.json              each agent's keys and signed documents (test keys)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ATTEST_HOME = os.environ["ATTEST_HOME"]
sys.path.insert(0, ATTEST_HOME)

from bench import world as bw  # noqa: E402  (Attest's own test-world helpers)
from gate import crypto  # noqa: E402
from gate.core import Gate  # noqa: E402

RECEIVER = bw.RECEIVER          # "supplierco"
MIN_STAKE = bw.MIN_STAKE        # 10,000: a sender's issuer must be staked this much


def build(workdir: Path) -> dict:
    gate_dir = workdir / "gate"
    os.environ.setdefault("ATTEST_LOG_DIR", str(workdir / "logs"))
    gate = Gate(gate_dir)
    seller_key = bw.operator(RECEIVER)
    gate.register_receiver(RECEIVER, min_stake=MIN_STAKE,
                           policy={"escalate_above": {"place_order": bw.ESCALATE_ABOVE}},
                           pubkey=seller_key["pub"])
    acme, shady = bw.operator("AcmeCorp"), bw.operator("ShadyCo")
    body = {"stake": True, "issuer": acme["pub"], "name": "AcmeCorp", "amount": 25000.0}
    assert gate.stake(acme["pub"], "AcmeCorp", 25000.0, crypto.sign(acme["priv"], body))["ok"]
    proc = bw.agent("acme-procurement", acme, "procurement: quotes and orders",
                    ["request_quote", "place_order"], 10000)
    cast = {
        "gate_pub": gate.pubkey,
        "receiver": RECEIVER,
        # staked operator, valid mandate: should be served
        "proc": proc,
        # sub-agent whose mandate (15,000) is wider than its parent's (10,000)
        "greedy": bw.delegate(proc, "acme-sub-greedy", ["place_order"], 15000),
        # credential says AcmeCorp but ShadyCo (unstaked) signed it
        "forger": bw.agent("acme-procurement-2", shady, "procurement: quotes and orders",
                           ["request_quote", "place_order"], 10000, claimed_operator="AcmeCorp"),
        # no credential, no mandate
        "newcomer": bw.bare("newcomer"),
    }
    (workdir / "cast.json").write_text(json.dumps(cast, indent=1))
    return cast


if __name__ == "__main__":
    wd = Path(sys.argv[1])
    wd.mkdir(parents=True, exist_ok=True)
    build(wd)
    print(f"seeded {wd}")
