# JANG in a Town marketplace

[JANG](https://github.com/byujan/JANGmarket) is a negotiation service for autonomous agents.
An owner signs a **mandate**: the limit their agent may not cross on each term, how much each
term matters, and the point below which no deal is worth doing. A buyer's agent and a seller's
agent then exchange offers across several terms at once. JANG enforces each mandate on every
offer and accept, never shows a mandate to the counterparty, and signs the agreed package with
Ed25519.

This example connects JANG end to end to a Lab marketplace. Town supplies discovery, messaging
and its faults, escrow, payment and reputation. JANG supplies the negotiation and the signed
agreement. The JANG service is the only participant developed independently of Town. Owners,
mandates, the TV item and the two adapter roles are fixtures of this example.

## How the run is wired

```
JANG (separate process, npm run dev on :3939)         Town Lab run (this directory)
──────────────────────────────────────────────        ─────────────────────────────────────────────
                                    ◄── health, register, lodge signed mandate ── tv-seller (jang_seller)
                                                       tv-seller publishes card sell.tv-65oled (no limits)
                                                       tv-buyer  lookup → quote_request → quote_response
                                    ◄── register, lodge signed mandate ───────── tv-buyer  (jang_buyer)
                                                       tv-buyer → tv-seller: jang_open
 POST /api/sessions, offers, accept ◄── each side reads the session, decides, and offers or accepts;
                                        only {session_id, offer_id} travel through Town as jang_turn
 Ed25519-signed agreement ─────────────►  tv-seller → tv-buyer: invoice {signed agreement}
                                         tv-buyer verifies JANG's signature with the key from
                                         /api/health, audits the terms against its own mandate,
                                         then escrow_hold → purchase_order → delivery → release → rating
```

| File | What it is |
| --- | --- |
| `jang_plugin.py` | `jang_buyer` and `jang_seller` roles, plus one validator per scenario |
| `jang_client.py` | the small httpx client for JANG's agent REST API |
| `jang_sign.py` | JANG-compatible canonical JSON, Ed25519 mandate signing, agreement verification. `JANG_SIGNER=ts` swaps in JANG's own TypeScript `signMandate` as a cross-check |
| `jang_strategy.py` | a Python port of JANG's deterministic negotiator (`agents/rule-strategy.ts`). JANG's REST API has no server-side negotiator, so each agent brings its own. Also `ClaudeStrategy`, which asks Claude through the `claude` CLI |
| `parity/` | 135 recorded session views, each decided by the Python port and by JANG's TypeScript original |
| `fixtures/` | canonical-JSON strings produced by JANG, and one real JANG-signed agreement |
| `*.yaml` | the scenarios below |
| `run_all.sh`, `verify_bundle.py` | run everything and verify each bundle with this example's validators loaded |
| `evidence/` | the bundles behind the table below |

## Reproduce

Versions used: Town `6f9a7c0` (main), evaluator `lab-0.2.6`, Python 3.14; JANG
[`byujan/JANGmarket@4fb4eca`](https://github.com/byujan/JANGmarket/commit/4fb4eca), Node 24.

```bash
# 1. JANG, in its own checkout. No database or API key is needed: without
#    DATABASE_URL it uses an in-memory store and open agent registration.
git clone https://github.com/byujan/JANGmarket && cd JANGmarket && git checkout 4fb4eca
npm install
npm run dev                      # leave running on http://localhost:3939

# 2. Town, from this checkout
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
nandatown run examples/jang/jang_market.yaml --out runs
python examples/jang/verify_bundle.py runs/<run id>
bash examples/jang/run_all.sh            # every scenario, verified; add --claude for the Claude run
python -m pytest -q tests/test_example_jang.py   # CI-safe, no JANG needed
```

`JANG_URL` overrides the JANG address. To recheck the strategy port against JANG's TypeScript
source, which needs a JANG checkout and a running server:

```bash
JANG_REPO=../JANGmarket python examples/jang/parity/record_cases.py
(cd ../JANGmarket && npx tsx "$OLDPWD/examples/jang/parity/ts_decide.mts" "$OLDPWD/examples/jang/parity/decide_cases.full.json")
python examples/jang/parity/record_cases.py compact
```

## What is judged

`jang_market`: a buyer finds the seller through the Town registry and both negotiate a 65-inch
TV on four terms (price, delivery days, warranty months, soundbar). The fault plan duplicates
the second `jang_turn` signal and the first `delivery`.

| Stage | Must hold |
| --- | --- |
| `jang_reachable` | JANG answered health, registration and mandate lodging for both roles. Otherwise every other stage is inconclusive, never passed or failed |
| `discovered` | the buyer found the seller through the Town registry and got a quote |
| `mandates_lodged` | JANG accepted both mandates, each signed in Python by its owner's own key |
| `negotiated` | offers alternated and closed in exactly one agreement covering all four terms |
| `agreement_verified` | each side verified JANG's signature against the key it trusts (`/api/health`, never the key embedded in the agreement) and found the terms inside its own mandate |
| `duplicate_turn_absorbed` | the duplicated turn signal was recognised from JANG's session state and produced no extra offer |
| `settled_once` | one escrow of exactly agreed price × quantity, released once; the duplicated delivery recognised |
| `reputation` | Town's receipt-driven score is consistent |
| `mandate_not_shared` | every inter-agent message carried only protocol keys, with no mandate field at any depth |

Boundary and failure cases:

| Scenario | What is injected | Must hold |
| --- | --- | --- |
| `jang_mandate_violation` | the buyer's first move is one price step above its own owner's ceiling | `violation_refused`: JANG refuses with 422 `MANDATE_VIOLATION`, and Town records only the dimension, never the limit. `recovered_in_bounds`: the buyer re-decides from JANG's state, and an in-bounds agreement is verified and settled |
| `jang_tampered_agreement` | the seller invoices an agreement altered to USD 90 more than JANG signed, which still fits the buyer's mandate | `tamper_detected`: only JANG's signature exposes it, and the buyer rejects it. `no_payment_on_tamper`: no escrow, payment or delivery follows |
| `jang_tampered_agreement_unverified` | the same tampering, but the buyer skips the signature check | **FAILS on purpose**: the overcharge is paid, which is what the signature check is for |
| `jang_no_zopa` | the buyer's ceiling (USD 1,000) is below the seller's floor (USD 1,050) | `no_deal_no_money`: the session ends without an agreement and no money moves |
| JANG down (CI test) | `JANG_URL=http://127.0.0.1:9` | every scenario INCOMPLETE, every JANG stage inconclusive |
| `jang_claude` | Claude, through `claude -p`, makes every buyer move | the `jang_market` stages, judged the same way |

## Observed

Town `6f9a7c0`, evaluator `lab-0.2.6`, JANG `4fb4eca`, signer `python`. Every bundle below is in
`evidence/` and passes `verify_bundle.py`. `evidence/summary.json` lists each run's stages.

| Run | Negotiators | Expected | Verdict | Observed | Bundle fingerprint |
| --- | --- | --- | --- | --- | --- |
| `jang_market` | rule | all 9 stages pass | **PASSED** | agreed USD 1,200 · 18 days · 36-month warranty · no soundbar, in 10 offers; duplicate turn and delivery absorbed | `sha256:0f6ce4c91262bd51…` |
| `jang_mandate_violation` | rule | refused with 422, then recover | **PASSED** | 422 `MANDATE_VIOLATION` on dimensions `*`, `price` (no limit recorded); same USD 1,200 agreement then verified and settled | `sha256:b8cd6ac8cb602eab…` |
| `jang_tampered_agreement` | rule | tamper rejected, no money moves | **PASSED** | buyer: `signature_ok: false`, `within_mandate: true`, so rejected; no escrow, payment or delivery | `sha256:938bf01c14db38fd…` |
| `jang_tampered_agreement_unverified` | rule | FAILED (control) | **FAILED** | signature check skipped: escrow of 129,000 cents (USD 1,290) held and paid | `sha256:7294f61947361f9d…` |
| `jang_no_zopa` | rule | no agreement, no money | **PASSED** | 12 offers, then JANG 409 `ROUND_LIMIT`; no agreement, no escrow | `sha256:4d82af5fbffb5b05…` |
| `jang_claude` | Claude buyer, rule seller | all 9 stages pass | **PASSED** | agreed USD 1,290 · 24 days · 24-month warranty · soundbar bundled, in 10 offers; no strategy errors. An earlier run on the same versions agreed USD 1,220 · 18 days · 30 months · no soundbar in 8 offers: Claude's moves vary between runs | `sha256:7e553d034a4254b7…` |
| JANG down (`tests/test_example_jang.py`) | — | nothing judged | **INCOMPLETE** | every JANG stage `not_enough_evidence`; ledger and privacy pass | CI |

## What this shows about compatibility

- **Signatures interoperate across languages.** JANG's TypeScript server accepted mandates signed
  by a Python owner. Python verified agreements signed by JANG, and rejected tampered and
  re-signed forgeries. That holds only because `jang_sign.canonical_json` reproduces JANG's
  `canonicalJson` byte for byte, checked against strings JANG produced (`fixtures/`):
  - integer-valued floats are written as `999`, not `999.0`
  - exponents are written as `1e-7` and `1e+21`
  - keys sort by UTF-16 code unit
  - non-ASCII text is not escaped

  Any one of these differences makes JANG answer `INVALID_MANDATE`. As a cross-check,
  `JANG_SIGNER=ts` signs with JANG's own TypeScript `signMandate` instead, and `jang_market`
  also passes that way.
- **JANG's guarantees hold under Town's transport faults.**
  - A duplicated turn signal is harmless, because each side re-reads JANG's session state before
    moving, so the duplicate cannot become a second offer.
  - The duplicated delivery is settled once.
  - The mandate refusal surfaces in Town as `{code, dimensions}` only. `jang_client.JangRefusal.public_detail`
    strips the `limit` and `offered` values that JANG's error carries, so a private limit never
    enters the bundle.
- **The signature is what protects payment.** The tampered price still sat inside the buyer's
  mandate, so a mandate check alone would have paid it. The unverified control shows that.
- **The strategy port matches.** On 135 recorded views from both of JANG's bundled scenarios and
  six strategy settings (toughness, respectBounds, deadlineAware, MESO, disclosed priorities), the
  Python port made the same move as JANG's TypeScript `decide` every time. CI rechecks this
  against the stored TypeScript decisions without Node.

### Findings

- **No early exit when no deal is possible.** In `jang_no_zopa` the two strategies exchange all
  12 offers. JANG then ends the session only by refusing the 13th with 409 `ROUND_LIMIT`. Neither
  side can learn sooner that no package fits both mandates, which is by design, since mandates are
  never shared, but it costs the full round budget.
- **The documented `respectBounds` defect reproduces.** While recording parity cases, JANG's
  shipped default (`respectBounds: false`) tried to accept a cambridge-rental package outside its
  own hard bound twice, and JANG refused it (`MANDATE_VIOLATION`). JANG's HW7 study describes this
  defect. These scenarios therefore run with `respectBounds: true`.

## Limitations and what remains untested

- **Fixtures, not independent agents.** Owners, mandates and both adapter roles are fixtures of
  this example; only the JANG service is independent of Town. Both negotiators are the Python
  port of JANG's own strategy. The Claude runs are two observations, not a campaign.
- **Non-deterministic identifiers.** The Lab engine is deterministic, but JANG is an external
  process: agent ids, session ids, agreement ids, JANG's signing key (regenerated per JANG process)
  and timestamps differ between runs, so bundle fingerprints differ as well. The sequence of moves
  for the rule strategy repeats.
- **No replay after the fact.** `nandatown verify` does not load scenario `plugin_files`, so it
  reports "evaluator replay mismatch" for these bundles. Use `verify_bundle.py`, which loads this
  one reviewed plugin first.
- **Not tested:**
  - JANG's Postgres store and Clerk web console
  - JANG's MCP front door (`mcp/server.ts`); the adapter uses its REST API
  - concurrent sessions, real network latency, a JANG restart mid-session (the in-memory store
    loses state)
  - mandate expiry during a run
  - more than one seller competing in one session
  - Track or Path compatibility: JANG is neither a Town mailbox participant nor an A2A endpoint
- **Unpinned trust root.** The trusted JANG key is fetched from `/api/health` at the start of
  each run. That is trust on first use, not a pinned key.
