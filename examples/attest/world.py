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
    acme, shady, flaky = bw.operator("AcmeCorp"), bw.operator("ShadyCo"), bw.operator("FlakyCo")
    for op, amount in ((acme, 25000.0), (flaky, 20000.0)):
        body = {"stake": True, "issuer": op["pub"], "name": op["name"], "amount": amount}
        assert gate.stake(op["pub"], op["name"], amount, crypto.sign(op["priv"], body))["ok"]
    gate.configure(dispute_window_s=0)      # an undisputed slashing claim is upheld at once
    proc = bw.agent("acme-procurement", acme, "procurement: quotes and orders",
                    ["request_quote", "place_order"], 10000)
    cast = {
        "gate_pub": gate.pubkey,
        "receiver": RECEIVER,
        # staked operator, valid mandate: should be served
        "proc": proc,
        # narrowed sub-agent: quotes up to 5,000 only (valid delegation, depth 2)
        "sub_ok": bw.delegate(proc, "acme-sub-ok", ["request_quote"], 5000),
        # sub-agent whose mandate (15,000) is wider than its parent's (10,000)
        "greedy": bw.delegate(proc, "acme-sub-greedy", ["place_order"], 15000),
        # credential says AcmeCorp but ShadyCo (unstaked) signed it
        "forger": bw.agent("acme-procurement-2", shady, "procurement: quotes and orders",
                           ["request_quote", "place_order"], 10000, claimed_operator="AcmeCorp"),
        # no credential, no mandate
        "newcomer": bw.bare("newcomer"),
        # staked operator whose credential is fine but whose mandate has already expired
        "expired": bw.agent("acme-procurement-old", acme, "procurement: quotes and orders",
                            ["request_quote", "place_order"], 10000, mandate_days=-1),
        # a second staked operator, to be slashed in the slashing test
        "flaky": bw.agent("flaky-buyer", flaky, "procurement for FlakyCo",
                          ["request_quote", "place_order"], 5000),
        # four more roots under AcmeCorp's single stake, for the issuer-exposure test
        **{f"cap{i}": bw.agent(f"acme-cap-{i}", acme, "procurement: orders",
                               ["request_quote", "place_order"], 10000) for i in range(1, 5)},
        # keys the tests need to act as an operator or as the receiver (test keys only)
        "keys": {"acme": acme, "flaky": flaky, "seller": seller_key},
    }
    (workdir / "cast.json").write_text(json.dumps(cast, indent=1))
    return cast


if __name__ == "__main__":
    wd = Path(sys.argv[1])
    wd.mkdir(parents=True, exist_ok=True)
    build(wd)
    print(f"seeded {wd}")
