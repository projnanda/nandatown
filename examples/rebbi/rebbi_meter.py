"""Rebbi's metering side for the Town runs. Runs with Rebbi's own interpreter.

In Rebbi, model usage is written to the ledger by its gateway or harness,
never by the agent, and a call the balance cannot cover is refused. Town's
worker makes no model calls, so the adapter meters each work step the same
way, through Rebbi's own Ledger, and reads the final state back with it.

    REBBI_DB=... python rebbi_meter.py usage AGENT TOKENS NOTE
    REBBI_DB=... python rebbi_meter.py snapshot

Prints one JSON object. Nothing here changes how Rebbi lends.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

from rebbi.ledger import Ledger
from rebbi.rules import lendable


def usage(ledger: Ledger, agent: str, tokens: int, note: str) -> dict:
    balance = ledger.balance(agent)
    if tokens > balance:
        return {"recorded": False, "balance": balance,
                "reason": "balance cannot cover this step"}
    state = ledger.record_usage(agent, tokens, datetime.now(UTC), note)
    return {"recorded": True, "balance": state.balance}


def snapshot(ledger: Ledger) -> dict:
    keys = ("loan_id", "lender", "borrower", "tokens", "status",
            "returned_tokens")
    return {
        "agents": [
            {"agent_id": a.agent_id, "balance": a.balance,
             "reserve_tokens": a.reserve_tokens,
             "expected_demand_tokens": a.expected_demand_tokens,
             "will_lend": a.will_lend, "lendable": lendable(a)}
            for a in ledger.agents()
        ],
        "loans": [{k: loan[k] for k in keys} for loan in ledger.loans()],
        "decisions": [
            {"decision_id": d["decision_id"], "requester": d["requester"],
             "decision": d["decision"],
             "request_id": d["request"].get("request_id", ""),
             "loan_id": d["response"].get("loan_id")}
            for d in ledger.decisions()
        ],
        "invariant_problems": ledger.check_invariants(datetime.now(UTC)),
    }


def main(argv: list[str]) -> int:
    ledger = Ledger(os.environ["REBBI_DB"])
    if argv[:1] == ["usage"] and len(argv) >= 3:
        out = usage(ledger, argv[1], int(argv[2]),
                    argv[3] if len(argv) > 3 else "")
    elif argv == ["snapshot"]:
        out = snapshot(ledger)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
