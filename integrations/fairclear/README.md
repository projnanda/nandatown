# FairClear in Nanda Town

FairClear is a clearing service for AI agents. Instead of haggling, a buyer
agent opens a sealed request for quotes, each seller agent submits one sealed
bid, the lowest bid wins and is paid the second-lowest bid, and the result is
signed so anyone can check it. This folder plugs that workflow into the Town
Lab as a working service that Town's own agents discover and use.

## What runs

`fairclear_procurement.yaml` (7 agents, seed 42, no model, no tokens):

1. `fairclear` registers `clearing.sealed2p` in the Town index.
2. `buyer-1` looks it up, holds its reserve x quantity in Town escrow and opens an RFQ (the reserve is never shown to sellers).
3. The clearing service invites every seller registered for `sell.widget`.
4. Each seller sends one bid: a salted SHA-256 commitment signed with its Town identity (auth layer), plus the opening. Only the commitment is recorded while the RFQ is open.
5. Rejected on purpose: a duplicate bid (`seller-c`), a bid after close (`seller-late`) and a bid forged in another seller's name (`seller-forger`).
6. At close the service clears, signs the full result and sends it to the buyer and the sellers.
7. The buyer verifies the signature and recomputes the award from the opened bids, then pays the winner price x quantity through escrow. A result that fails verification is not paid.

The `fairclear_procurement` validator checks these stages from the recorded
events only: `discovery`, `sealed_bids`, `rejections`, `efficient_award`,
`second_price`, `receipt_verified` and `settlement`. Town adds `ledger_conserved`.

## Backends

| Backend | What clears the RFQ | Use |
|---|---|---|
| `reference` (default) | the textbook rule in `fairclear_town.py` | anyone can reproduce |
| `external` | a separately hosted FairClear deployment, via `FAIRCLEAR_CLEARING_CMD` | live compatibility check |

The external contract is JSON on stdin/stdout: input
`{"rfq": {...}, "bids": [{"bidder", "cents"}]}`, output
`{"winner", "price_cents", "result_hash", "verified"}`. The deployment and its
bridge live outside this repository.

## Reproduce

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'
nandatown run integrations/fairclear/fairclear_procurement.yaml
python -m pytest -q tests/test_fairclear_integration.py
# live deployment (optional):
FAIRCLEAR_BACKEND=external FAIRCLEAR_CLEARING_CMD="python /path/to/bridge.py" \
  nandatown run integrations/fairclear/fairclear_procurement.yaml
```

## Not tested here

Track (HTTP mailbox) and Path (A2A) modes; model-driven agents; differentiated
goods (multi-attribute offers); network faults during bidding; collusion
across repeated RFQs. Town's `hmac.v1` auth is a symmetric reference signer;
the external deployment signs with Ed25519 and verifies on its side.
