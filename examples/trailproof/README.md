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

## Files

| File | What |
|---|---|
| `trailproof_link.py` | Standard-library MCP streamable-HTTP client, TrailProof's canonical-JSON / commitment / signing conventions re-implemented from its README, Ed25519 via `cryptography`, offline receipt verification, and `start_server()` |
| `trailproof_buyer.py` | Town buyer: user key signs a MANDATE, agent key runs `check_order`; fail-open when the ledger is down |
| `trailproof_seller.py` | Town seller: commits its OFFER behind the buyer's mandate; `TRAILPROOF_SELLER_MODE=lying_commit` commits a different offer than it sends |
| `run_check.py` | The reproducible check: starts the ledger, runs five Track cases, cross-verifies Town's bundle against the ledger, writes `evidence/summary.json` |
| `evidence/` | Recorded run: `summary.json` plus the canonical bundle files of each case, verifiable with `nandatown verify evidence/<case>` |
| `../../tests/test_trailproof_integration.py` | The same cases as pytest; skipped unless `TRAILPROOF_HOME` is set |

The link module does not import the TrailProof package. A passing run
therefore also shows that a client built only from TrailProof's documented
contract (sorted-key JSON, `sha256(canonical_json(payload))`, signature over
`commit|prev|refs`, receipt over `event_id|commit|ledger_ts`) interoperates
with the server.

## Reproduce

Tested with nandatown `0.2.0` at commit `6f9a7c0` and TrailProof `0.1.0`
(the commit is in `evidence/summary.json`), Python 3.12 on Windows.

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

# 4. The end-to-end check: five Track runs, cross-verified
TRAILPROOF_HOME=$PWD/TrailProof python examples/trailproof/run_check.py
#   add --record to refresh examples/trailproof/evidence/

# 5. The same as tests
TRAILPROOF_HOME=$PWD/TrailProof python -m pytest -q tests/test_trailproof_integration.py
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
`cmd:` participants, and TrailProof's MCP tool surface over streamable HTTP
(`register_party`, `record_event`, `get_trail`, `check_order`). Each case is
a separate Town run against one shared ledger.

| Case | Profile | Expected | Observed |
|---|---|---|---|
| clean | quote-clean | Town passed; chain MANDATE, OFFER, ORDER; `in_scope: true` | as expected, 3 receipts verify offline |
| duplicate | quote-duplicate-delivery | Town passed, `duplicate_recognized`; exactly one OFFER on the chain | as expected |
| lying_commit | quote-clean, seller commits a different offer than it sends | Town `response` passed, `correct` failed; buyer refuses with `COMMIT_MISMATCH`; no ORDER | as expected |
| over_budget | quote-clean, mandate 30.00 for a 39.90 quote | Town `correct` failed; buyer refuses with `OVER_BUDGET`; no ORDER | as expected |
| ledger_down | quote-clean, `TRAILPROOF_URL` unreachable | Town passed; buyer note `no_receipt: true`; seller committed nothing | as expected |

Per case, `run_check.py` also checks that both agents saw the same ledger
key, that the seller's committed `offer_commit` is the one the buyer checked,
that every receipt in Town's acknowledgement notes verifies with that key
against its commit, and that the live chain's `prev` links hold and contain
the event ids Town recorded. 5/5 cases, all checks pass; see
`evidence/summary.json`.

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

Not tested here: the dispute path (`open_dispute`, `reveal`,
`get_case_bundle`, `export_evidence_pack`) and `attest_history`, because the
quote task has no charge, fulfilment or delivery step to dispute. TrailProof's
own scenarios cover those (`uv run python demo.py`). Also not tested: Town's
Lab (no TrailProof layer plugin), portable identity (`--identity` runs hand
out grants, and the stdlib participants join by token only), crash profiles
(`FAULT` reaches only Town's scripted participants), a hosted ledger (both
agents and the ledger ran on one machine), and concurrency on the ledger
(one order per run). The link module is a Town-side client and not an
independently adopted TrailProof SDK, and TrailProof's amounts are 2-dp
strings in one currency while Town's are integer cents, so the mapping
`3990 -> "39.90"` is this example's convention.
