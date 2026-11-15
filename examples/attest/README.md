# Attest x Town

[Attest](https://github.com/smghaas/attest) is a Know-Your-Agent gate. A receiving agent sends it a sender's
signed credential, mandate chain and request; it answers `allow`, `refuse`, `escalate` or `unknown`
with a receipt signed by the gate. Trust comes from operator stake. This directory runs that gate
inside Town's Track: a seller that asks the real Attest gate (HTTP) before it quotes, and a buyer
that signs its quote request and checks the gate's receipt.

| File | Role |
| --- | --- |
| `world.py` | seeds an Attest gate with a cast: two staked operators, delegated and over-wide sub-agents, forger, newcomer, expired and cap agents |
| `attest_seller.py` | Town mailbox seller; quotes only on `allow`; journal absorbs redelivery; every refusal says how to get served |
| `attest_buyer.py` | Town mailbox buyer; sends `attest` envelope; verifies the receipt; can learn the gate from the seller |

Tested against Town `6f9a7c0` and Attest `main` at `b3d5a5c` (the sandbox, SDK and signed checks). Attest stays in its own repository.

## How a Town agent finds the gate

Town lists participants by role and capability, so a buyer finds this seller as the `quote.read` seller,
like any other. The gate is not a Town participant: it is a separate HTTP service. A buyer learns about it
from the seller. Every `quote_refusal` carries `how_to_get_served`:

```json
{"checked_by": "attest", "gate": "<gate URL>", "gate_pubkey": "<hex>", "receiver": "supplierco",
 "info": "<gate URL>/info", "guide": "https://github.com/smghaas/attest/blob/main/AGENTS.md",
 "attach": "...", "no_credentials": "..."}
```

With `ATTEST_DISCOVER=1` the buyer starts like Town's stock buyer (no envelope, no gate URL, no receiver
name), reads that, and asks again with an envelope. A stranger with no credential then gets a puzzle and,
once it is solved, a public quote. An operator that registered itself on a sandbox gate (`attest operator
create`, `attest agent create`) gets its quotes and orders checked like anyone else. Set `ATTEST_PUBLIC_URL`
on the seller when other agents must reach the gate at a different address from the seller.

## Reproduce

```bash
git clone https://github.com/smghaas/attest
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
export ATTEST_HOME=$PWD/attest
pip install -e "$ATTEST_HOME"                            # the gate's own dependencies and its SDK
python -m pytest -q tests/test_attest_integration.py     # 22 tests, about 4 min, starts a gate per test on a free port
```

Without `ATTEST_HOME` the tests skip. To run one workflow by hand: `python examples/attest/world.py WORK`,
start the gate with `ATTEST_STATE_DIR=WORK/gate ATTEST_LOG_DIR=WORK/logs uvicorn gate.server:app` from
the Attest checkout, set `ATTEST_URL` and `ATTEST_CAST=WORK/cast.json`, then
`nandatown run quote-clean --agent buyer="cmd:python examples/attest/attest_buyer.py" --agent seller="cmd:python examples/attest/attest_seller.py"`.
Buyer knobs: `ATTEST_AGENT=proc|sub_ok|greedy|forger|newcomer|expired|flaky|cap1..cap4`, `ATTEST_ACTION=request_quote|place_order`,
`ATTEST_AMOUNT` (USD), `ATTEST_SIGNER`, `ATTEST_TAMPER=1`, `ATTEST_SOLVE_POW=1`, `ATTEST_DISCOVER=1`,
`ATTEST_IDENTITY` (an agent file from Attest's CLI, instead of the cast), `ATTEST_RECEIVER`.
Seller: `ATTEST_JOURNAL=0`, `ATTEST_RECEIVER`, `ATTEST_PUBLIC_URL`.

The quote request carries the action it asks the gate to approve (`request_quote` or `place_order`); the seller serves the
quote only when the gate allows that action, or answers `unknown`/`pow_ok` (read-only, puzzle solved).

## What was observed (one Town run each unless noted)

| Capability | Case | Expected | Observed |
| --- | --- | --- | --- |
| identity + stake | staked `proc`, quote | served, receipt verifies | all stages pass; `attest_receipt_verified: true`; `nandatown verify` passes |
| delegation | `sub_ok` (quotes up to 5,000) asks a quote | served | `allow`, chain depth 2 |
| attenuation | `greedy` (15k under a 10k parent) | refused | `refuse` / `attenuation_violation` |
| scope | `sub_ok` asks for an order | refused | `refuse` / `out_of_scope` |
| mandate limit | `proc` orders 12,000 against a 10,000 mandate | refused | `refuse` / `over_limit` |
| valid order | `proc` orders 4,000 | served | `allow` |
| receiver policy | `proc` orders 9,000 (policy: above 8,000 needs a human) | not served | `escalate` / `policy_escalation` |
| order splitting | two runs of 6,000 under one 10,000 mandate | 1st served, 2nd refused | `allow`, then `refuse` / `cumulative_over_limit` |
| revocation | only the signer may revoke; root revoked, then `sub_ok` asks | wrong key rejected (HTTP 400); sub-agent refused | `refuse` / `mandate_revoked` (cascade) |
| expiry | `expired` | refused | `refuse` / `mandate_expired` |
| signatures | amount changed after signing; request signed with another key | refused | `refuse` / `bad_request_signature` (both) |
| forged name | `forger` (says AcmeCorp, signed by unstaked ShadyCo) | not served | `unknown` / `pow_required` |
| slashing | `flaky` (staked) orders; receiver files a breach claim; same agent orders again | served, then refused | `allow`, then `refuse` / `unstaked_money_action` |
| proof of work | `newcomer` asks a quote, solves the puzzle, asks again | puzzle, then quote | `unknown`/`pow_required`, then `unknown`/`pow_ok`, quote served (3,990); an unstaked order is refused |
| issuer stake cap | four roots of one 25,000 stake order 7,000 each | first three served, fourth refused | `refuse` / `issuer_exposure_over_stake` |
| Town compatibility | stock Town buyer (sends no envelope) | refused, told how to get served | `refuse`, "no KYA envelope", `how_to_get_served` sent; Town reports `correct` as not_enough_evidence |
| discovery | stranger told nothing about Attest asks plainly, then follows the refusal | gate learned from the seller; public quote | `refuse`, then `unknown`/`pow_required`, then `unknown`/`pow_ok`; quote 3,990; receipt verifies |
| self-registration | operator and seller sign themselves up on a sandbox gate (SDK: faucet, register receiver, agent) | quote served; order above the seller's own policy escalated | `allow` (after one plain ask); then `escalate` / `policy_escalation` |
| delivery semantics | `quote-duplicate-delivery`, journal on | one application | `processed` and `correct` pass |
| boundary case | same, journal off | one application | before HW9: second delivery got `refuse` / `replay`. Now the gate answers it as a `repeat` of the first check (same receipt) and the seller records a duplicate; `processed` passes |

Discovery takes two quote requests (plain, then with an envelope). Town's `quote-clean` grading expects one
request and one response, matched to the first request, so Town's own `correct` stage does not pass on those
runs; the tests check the buyer's verdict and the gate's receipt instead. A Town profile for a two-step
exchange would fix that on Town's side.

## Not tested

Town's crash, amnesia and lost-ack faults (Town applies those only to its own scripted participants, not to `cmd:` harnesses);
the dispute and arbiter path of slashing (claims here are undisputed); stake and slashing on chain (Attest keeps them in a JSON file);
several gates or concurrent senders; scale. The puzzle is seeded by the latest Base Sepolia block when the gate can reach it and by a
local fallback otherwise, so the test does not depend on the network. The buyer and seller are written by the Attest team, so this is
a compatibility check, not an independent-agent result in the sense of docs/testing-an-existing-agent.md.
