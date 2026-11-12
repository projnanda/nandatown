# TrailProof under Town's quote task

[TrailProof](https://github.com/Andreyivvi/TrailProof) is an append-only
**commitment ledger** for purchases made by AI agents, exposed as an MCP
server. Buyer agent, merchant and payment provider each lock a signed
SHA-256 fingerprint of what they saw at every step of an order, before
anyone knows there will be a dispute; on a dispute each side reveals its
plaintext, the ledger checks it against the committed hashes, and a
deterministic rules engine produces findings and a verdict.

This example runs TrailProof underneath Town's Track: the quote task is the
same, the mailbox contract is the same, but the buyer's user key commits a
MANDATE, the seller commits its OFFER, and the buyer agent orders only after
the ledger's `check_order` says the offer is inside the mandate. Town's
evidence bundle then carries the ledger receipts, and the two trails can be
cross-checked offline.

```
Town coordinator (HTTP mailbox, leases, fences)          TrailProof ledger (MCP over HTTP)
      |                                                          |
buyer ---- quote_request {sku, qty, price, trailproof:{order_id, mandate_commit}} ---> seller
  |  MANDATE (user key) ------------------------------------------>|
  |                                            seller: OFFER ----->|   prev = mandate commit
buyer <--- quote_response {total_cents, trailproof:{offer, offer_commit, receipt}} --- seller
  |  check_order(mandate, offer) -------------------------------->|   scope rules; ORDER if in scope
  |  ack note {correct, trailproof:{in_scope, reasons, order_commit, order_receipt}}
```

What Town judges is unchanged: accepted, claimed, received, processed once,
response correlated, buyer's own `correct` assertion. What TrailProof adds
is attributed, signed and offline-verifiable: who committed what, in which
order, and whether the agent stayed inside the mandate its user signed.

With `TRAILPROOF_SETTLE` set, the order does not stop at the quote. The two
participants carry it through the rest of TrailProof's lifecycle, and they
coordinate through the ledger alone: the seller acts when the buyer's ORDER
appears on the chain, the buyer when the FULFILMENT does.

```
seller's PSP: CHARGE ----------------------------------------------->|   a third role, its own key
seller:       FULFILMENT -------------------------------------------->|   and drops the parcel (a file)
buyer:        DELIVERY of what the parcel actually holds ------------->|
   parcel is not what the offer said:
buyer:        open_dispute -> DISPUTE, case opened ------------------->|
buyer, user:  reveal MANDATE, ORDER, DELIVERY ------------------------>|   each re-hashed against its commit
seller, PSP:  reveal OFFER, FULFILMENT, CHARGE ----------------------->|
              get_case_bundle: findings, verdict, remedy <------------|
seller's PSP: REFUND (closes the case) ------------------------------>|
buyer:        ack note {..., settlement:{case_id, verdict, findings, delivery_receipt, dispute_receipt}}
```

The parcel is the one channel that is neither Town's mailbox nor the ledger:
the seller "ships" by writing a file, and the buyer commits what it finds
there. The buyer settles inside its claim, so the whole exchange has to
finish inside Town's lease; it does, in well under a second on one machine.

## Files

| File | What |
|---|---|
| `trailproof_link.py` | Standard-library MCP streamable-HTTP client, TrailProof's canonical-JSON / commitment / signing conventions re-implemented from its README, Ed25519 via `cryptography`, offline receipt verification, the dispute calls, an offline evidence-pack verifier, and `start_server()` |
| `trailproof_buyer.py` | Town buyer: user key signs a MANDATE, agent key runs `check_order`; fail-open when the ledger is down. With `TRAILPROOF_SETTLE`: commits the DELIVERY it finds, disputes a wrong parcel, reveals, reports the verdict |
| `trailproof_seller.py` | Town seller: commits its OFFER behind the buyer's mandate; `TRAILPROOF_SELLER_MODE=lying_commit` commits a different offer than it sends. `TRAILPROOF_SETTLE=deliver\|wrong_product\|tamper_reveal`: its PSP charges, it ships, answers a dispute and refunds on the ledger's verdict |
| `run_check.py` | The reproducible check: starts the ledger, runs eight Track cases, cross-verifies Town's bundle against the ledger, exports and re-verifies the evidence pack of each dispute, writes `evidence/summary.json` |
| `evidence/` | Recorded run: `summary.json`, the canonical bundle files of each case (verifiable with `nandatown verify evidence/<case>`), and `packs/<case>.json`, the TrailProof evidence pack of each dispute |
| `../../tests/test_trailproof_integration.py` | The same cases as pytest; skipped unless `TRAILPROOF_HOME` is set |

The link module does not import the TrailProof package. A passing run
therefore also shows that a client built only from TrailProof's documented
contract (sorted-key JSON, `sha256(canonical_json(payload))`, signature over
`commit|prev|refs`, receipt over `event_id|commit|ledger_ts`) interoperates
with the server.

## Reproduce

Tested with nandatown `0.2.0` at commit `6f9a7c0` and TrailProof `0.1.0` at
commit `8bd88b9` (also in `evidence/summary.json`), Python 3.12 on Windows.

```bash
# 1. TrailProof, in its own checkout (it runs as a separate MCP server)
git clone https://github.com/Andreyivvi/TrailProof
cd TrailProof && uv sync && uv run pytest        # 74 tests
cd ..

# 2. Town, from this checkout
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'

# 3. Town's own MCP handshake probe against TrailProof (stdio)
nandatown mcp test --cmd "TrailProof/.venv/bin/python -m trailproof.server"
#   -> ok: true, tools: register_party, record_event, check_order, get_trail,
#      open_dispute, reveal, get_case_bundle, export_evidence_pack, attest_history

# 4. The end-to-end check: eight Track runs, cross-verified
TRAILPROOF_HOME=$PWD/TrailProof python examples/trailproof/run_check.py
#   add --record to refresh examples/trailproof/evidence/

# 5. The same as tests
TRAILPROOF_HOME=$PWD/TrailProof python -m pytest -q tests/test_trailproof_integration.py   # 15 passed
```

On Windows the interpreter is `TrailProof\.venv\Scripts\python.exe`; set
`TRAILPROOF_PYTHON` if the checkout has no `.venv`.

To drive one run by hand, start the ledger and point both participants at it:

```bash
(cd TrailProof && uv run python -m trailproof.server --http --port 8765) &
export TRAILPROOF_URL=http://127.0.0.1:8765/mcp
nandatown run quote-clean \
  --agent "buyer=cmd:python examples/trailproof/trailproof_buyer.py" \
  --agent "seller=cmd:python examples/trailproof/trailproof_seller.py"
```

`--http` is the one change this integration needed in TrailProof: its MCP
server was stdio-only, which gives every client process its own ledger. One
shared ledger over streamable HTTP is what lets two Town participants commit
to the same chain.

## Results

Interface under test: Town's Track mailbox contract with two external
`cmd:` participants, and TrailProof's MCP tool surface over streamable HTTP:
`register_party`, `record_event`, `get_trail`, `check_order`, and in the
lifecycle cases `open_dispute`, `reveal`, `get_case_bundle` and
`export_evidence_pack`, eight of its nine tools. Each case is a separate
Town run against one shared ledger.

| Case | Profile | Expected | Observed |
|---|---|---|---|
| clean | quote-clean | Town passed; chain MANDATE, OFFER, ORDER; `in_scope: true` | as expected, 3 receipts verify offline |
| duplicate | quote-duplicate-delivery | Town passed, `duplicate_recognized`; exactly one OFFER on the chain | as expected |
| lying_commit | quote-clean, seller commits a different offer than it sends | Town `response` passed, `correct` failed; buyer refuses with `COMMIT_MISMATCH`; no ORDER | as expected |
| over_budget | quote-clean, mandate 30.00 for a 39.90 quote | Town `correct` failed; buyer refuses with `OVER_BUDGET`; no ORDER | as expected |
| ledger_down | quote-clean, `TRAILPROOF_URL` unreachable | Town passed; buyer note `no_receipt: true`; seller committed nothing | as expected |
| delivered | quote-clean, `TRAILPROOF_SETTLE=deliver` | Town passed; chain MANDATE, OFFER, ORDER, CHARGE, FULFILMENT, DELIVERY written by four keys; no case | as expected |
| wrong_product | quote-clean, `TRAILPROOF_SETTLE=wrong_product` | Town passed; chain ends DISPUTE, REFUND; finding `DELIVERED_NE_OFFER`, verdict `merchant_at_fault`, refund 39.90, case closed; evidence pack verifies offline | as expected |
| tampered_reveal | quote-clean, `TRAILPROOF_SETTLE=tamper_reveal` | Town passed; the seller's FULFILMENT reveal fails its hash; finding `TAMPERED`, verdict `merchant_at_fault`, no REFUND, case open; pack verifies and flags the event | as expected |

Per case, `run_check.py` also checks that both agents saw the same ledger
key, that the seller's committed `offer_commit` is the one the buyer checked,
that every receipt in Town's acknowledgement notes verifies with that key
against its commit, and that the live chain's `prev` links hold and contain
the event ids Town recorded. In the dispute cases it then reads the case back
from the ledger, compares it with what the buyer told Town, exports the
evidence pack and re-verifies it from the JSON alone (every ledger receipt,
every writer signature, the chain, every revealed plaintext against its
commitment), and checks that a doctored copy fails. 8/8 cases, 119 checks,
all pass; see `evidence/summary.json`.

### The boundary cases

**lying_commit** is the case TrailProof exists for. Town sees a correct
quote: the seller answered `3990` cents for the request it was asked, so the
`response` stage passes. The buyer agent, however, is handed an offer whose
hash is not the one the seller put on the ledger, `check_order` returns
`COMMIT_MISMATCH`, and the agent does not order. Town records this as the
buyer's own failed `correct` assertion with the ledger's reason in the note.
The stage note Town renders reads "buyer observed total 3990 against expected
3990", because Town's evaluator only knows the total; the TrailProof reason
is in the acknowledgement note, not in Town's stage note. That is a real
seam between the two evidence systems and is left as is.

**wrong_product** is the whole project in one Town run. Town's verdict is
`passed`, correctly: the quote was right and the order was in scope. What
went wrong happened after the quote, where Town does not look, and that is
where the ledger takes over: the seller committed a FULFILMENT for another
sku, the buyer committed the DELIVERY it actually found, both revealed, and
the rules engine found `DELIVERED_NE_OFFER` with the mandate in scope. The
seller's PSP refunded the 39.90 charge and the REFUND closed the case.

**tampered_reveal** is the failure case for a real counterparty. The seller
ships the wrong sku and, once disputed, reveals a FULFILMENT that claims the
right one. It cannot make that claim stick: the plaintext does not hash to
what it committed before the dispute, the ledger records the failed reveal
as `TAMPERED` against the seller, and the reveal cannot be retried. The case
stays open with no refund, which is the honest outcome: TrailProof decides
and documents, it cannot move money a party refuses to move. The exported
pack still verifies and marks that one event as unrevealed.

**ledger_down** shows TrailProof's fail-open rule inside Town: the purchase
is not blocked by an unreachable ledger, and the absence of a receipt is
recorded rather than silent.

**duplicate** shows the two idempotency mechanisms agreeing: Town's journal
stops a second application, and the ledger chain shows one OFFER.

## What this does and does not show

Shown: TrailProof runs as a separate service that two independent Town
participants share over MCP HTTP; Town's recorded facts and the ledger's
chain agree and can be verified offline from the bundle plus the ledger
public key; the mandate scope check and the commitment check change the
buyer's behaviour in ways Town records.

Also shown, in the lifecycle cases: the complete order lifecycle with three
roles and four keys on one chain, a dispute opened, argued and decided on
the ledger between two Town participants that share no other channel, and an
evidence pack that a third party can check without the ledger.

Not tested here: `attest_history` (the recognised-customer attestation needs
several completed orders between the same parties; a Town run is one order)
and the other dispute claims (`not_received`, `not_authorised`); TrailProof's
own scenarios cover those (`uv run python demo.py`). Settlement happens
inside the buyer's claim, so it is bounded by Town's lease: a slow ledger
would surface as a missing settlement in the note, not as a longer wait. The
PSP is a second key held by the seller process, not an independent
participant, and the parcel is a file. Also not tested: Town's
Lab (no TrailProof layer plugin), portable identity (`--identity` runs hand
out grants, and the stdlib participants join by token only), crash profiles
(`FAULT` reaches only Town's scripted participants), a hosted ledger (both
agents and the ledger ran on one machine), and concurrency on the ledger
(one order per run). The link module is a Town-side client and not an
independently adopted TrailProof SDK, and TrailProof's amounts are 2-dp
strings in one currency while Town's are integer cents, so the mapping
`3990 -> "39.90"` is this example's convention.
