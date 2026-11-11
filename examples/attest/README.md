# Attest x Town

[Attest](https://github.com/smghaas/attest) is a Know-Your-Agent gate. A receiving agent sends it a sender's
signed credential, mandate chain and request; it answers `allow`, `refuse`, `escalate` or `unknown`
with a receipt signed by the gate. Trust comes from operator stake. This directory runs that gate
inside Town's Track: a seller that asks the real Attest gate (HTTP) before it quotes, and a buyer
that signs its quote request and checks the gate's receipt.

| File | Role |
| --- | --- |
| `world.py` | seeds an Attest gate with a small cast (staked operator, over-wide sub-agent, forger, newcomer) |
| `attest_seller.py` | Town mailbox seller; quotes only on `allow`; journal absorbs redelivery |
| `attest_buyer.py` | Town mailbox buyer; sends `attest` envelope; verifies the receipt |

Tested against Town `6f9a7c0` and Attest `36f59b5`. Attest stays in its own repository.

## Reproduce

```bash
git clone https://github.com/smghaas/attest && git -C attest checkout 36f59b5
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
export ATTEST_HOME=$PWD/attest
python -m pytest -q tests/test_attest_integration.py     # about 90 s, starts the gate on a free port
```

Without `ATTEST_HOME` the tests skip. To run one workflow by hand: `python examples/attest/world.py WORK`,
start the gate with `ATTEST_STATE_DIR=WORK/gate ATTEST_LOG_DIR=WORK/logs uvicorn gate.server:app` from
the Attest checkout, set `ATTEST_URL` and `ATTEST_CAST=WORK/cast.json`, then
`nandatown run quote-clean --agent buyer="cmd:python examples/attest/attest_buyer.py" --agent seller="cmd:python examples/attest/attest_seller.py"`.
Buyer knobs: `ATTEST_AGENT=proc|greedy|forger|newcomer`, `ATTEST_TAMPER=1`. Seller: `ATTEST_JOURNAL=0`.

## What was observed

| Run | Expected | Observed |
| --- | --- | --- |
| `quote-clean`, staked `proc` | quote served, receipt verifies | all stages passed; buyer notes `attest_receipt_verified: true`; `nandatown verify` passes |
| `forger`, `newcomer` | no quote | gate `unknown` / `pow_required`; Town `correct`: not_enough_evidence |
| `greedy` | no quote | `refuse` / `attenuation_violation` |
| `proc` with amount changed after signing | no quote | `refuse` / `bad_request_signature` |
| stock Town buyer | no quote | `refuse`, "no KYA envelope" |
| `quote-duplicate-delivery`, journal on | one application, duplicate absorbed | `processed` and `correct` passed |
| `quote-duplicate-delivery`, journal off | (boundary) | second delivery gets `refuse` / `replay` from the gate for a request already served |

## Not tested

Crash, amnesia and lost-ack profiles (Town applies those faults only to its own scripted participants,
not to `cmd:` harnesses); the proof-of-work path (an `unknown` sender is not served, it never solves the puzzle);
`escalate`; slashing and revocation; stake on chain (Attest keeps stake in a JSON file); several gates; scale.
The buyer and seller are written by the Attest team, so this is a compatibility check, not an
independent-agent result in the sense of docs/testing-an-existing-agent.md.
