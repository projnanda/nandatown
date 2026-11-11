# Locard: proof of coverage at the door, in Town's Track quote task

**Locard** is a risk market for AI agents. An agent's operator submits the agent's tool manifest and trace history; Locard scores the risk, prices cover, binds a policy and returns a signed policy token. A counterparty checks that token with Locard before letting the agent in, and Locard's live monitoring suspends the policy the moment the agent breaks its declared limits. The service lives in its own repository ([zaccytoymas/locard-mvp](https://github.com/zaccytoymas/locard-mvp), `locard/`, FastAPI).

This integration puts the whole Locard workflow inside Town's existing **Track quote task**, between separate processes over Town's HTTP mailbox:

```
                   Locard service (separately hosted, :8700)
          submit -> quote -> accept           verify (token, agent, amount)
          monitoring event (violation => suspend)            ^
                    ^                                         |
                    |                                         |
  buyer.py  ---- quote_request + policy token ---->  vendor.py (seller role)
  (insured agent)   via Town coordinator (Track)      no valid cover => quote_refused
            <--- quote_response / quote_refused ---   covered        => quote_response
```

- **`buyer.py`** (buyer role) plays the Locard MVP's procurement agent. Before messaging the vendor it goes through Locard end to end (underwriting, quote, binding), then sends Town's `quote_request` with its policy token attached and judges the reply like Town's stock buyer.
- **`vendor.py`** (seller role) is a coverage-gated vendor. It calls Locard's `/v1/verify` with the token, agent id and order value before doing any work. Covered: it quotes. Not covered, or Locard unreachable: it refuses (`quote_refused`, ack `rejected`, `applied: false`). Locard's verdict travels in the vendor's acknowledgement note, so it is recorded in Town's evidence bundle.
- **Town's own scripted buyer** is the "other agent": it knows nothing about Locard, so it shows what the vendor does with an uninsured counterparty.
- Town's **coordinator** provides delivery, leases, fencing, fault injection and the evaluator; the integration changes nothing in `src/`.

| File | What it is |
|---|---|
| `buyer.py`, `vendor.py` | The two Track participants (`cmd:` harnesses) |
| `locard_client.py` | Standard-library client for the Locard API |
| `fixtures/` | The procurement agent's manifest and 150 of its synthetic trace events (Locard needs at least 100) |
| `run_checks.py` | Runs every case through `nandatown run`, compares expected and observed, verifies each bundle, writes `evidence/RESULTS.md` |
| `fake_locard.py` | A stand-in Locard (same API shapes, HMAC tokens, no risk model) so `tests/test_locard_integration.py` runs offline |
| `evidence/` | The bundles from the run against the real Locard service, plus the failing bundle that found a bug (below) |

## Reproduce

**1. Town** (this repository, Python 3.11+):

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'
```

**2. Locard** (separate repository, separate terminal):

```bash
git clone https://github.com/zaccytoymas/locard-mvp && cd locard-mvp && git checkout 96b6c51
cd locard && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
LOCARD_DB=/tmp/locard-town.db .venv/bin/uvicorn app.main:app --port 8700
```

**3. The compatibility check** (from this repository's root):

```bash
python integrations/locard/run_checks.py --locard-version 96b6c51
```

It prints one line per case and exits 0 only if every case behaves as expected and every bundle passes `nandatown verify`. About 70 seconds, most of it the scripted buyer waiting out its deadline after being refused.

One case by hand:

```bash
nandatown run quote-clean \
  --agent seller=cmd:"python integrations/locard/vendor.py" \
  --agent buyer=cmd:"python integrations/locard/buyer.py --mode insured --agent-id procurement-bot-try1"
```

**4. Tests without the Locard service** (stand-in Locard): `python -m pytest -q tests/test_locard_integration.py`

**5. Check the recorded evidence offline:** `nandatown verify integrations/locard/evidence/insured-buyer`

## What is tested

The interface under test is the **Track mailbox quote workflow** (`quote_request` / `quote_response`, leases, fenced acknowledgements), with a Locard coverage check at the seller's door. The full table, with run ids, is in [`evidence/RESULTS.md`](evidence/RESULTS.md).

| Case | Town profile | Expected | Observed |
|---|---|---|---|
| Insured agent asks for a quote | `quote-clean` | Quote served; Town verdict passed | ✅ as expected; Locard verified once |
| Town's scripted buyer, no policy | `quote-clean` | Refused, `coverage_required`, without calling Locard | ✅ as expected |
| Agent reports an over-cap purchase to Locard monitoring, then asks | `quote-clean` | Refused, `policy_suspended:over_spend_cap` | ✅ as expected |
| Policy token signature altered | `quote-clean` | Refused, `invalid_signature` | ✅ as expected |
| Locard unreachable from the vendor | `quote-clean` | Refused, `locard_unreachable` (fail closed) | ✅ as expected |
| Town delivers the same request twice | `quote-duplicate-delivery` | Quote served; applied once; Locard asked once | ✅ as expected (after the fix below) |
| Town drops the vendor's first ack | `quote-lost-ack` | Quote served; retry safe | ✅ as expected |

In the refused cases Town's verdict is "not passed" with the first broken stage `received`: the vendor rejected the work, which is the intended outcome, not a delivery failure. The buyer exits 6 (refused); Town's scripted buyer exits 5 (no quote response before its deadline).

### The failure Town found

The first version of `vendor.py` resent its original acknowledgement note on a redelivered request, so the duplicate also said `applied: true`. Under `quote-duplicate-delivery`, Town's evaluator failed the run: **"applied 2 times, expected once"** ([`evidence/duplicate-delivery-before-fix`](evidence/duplicate-delivery-before-fix)). For a coverage gate this matters: anything downstream that counts applied orders (a ledger, a claims record) would count the order twice, while the buyer still received one correct quote, so no buyer-side test would have noticed. The fix acknowledges a redelivery with `{"duplicate": true}` only, and the coverage check still runs once. `test_duplicate_delivery_checks_coverage_once_and_applies_once` is the regression test: it fails against the old behaviour and passes now.

## Limits and what remains untested

- **Locard is the MVP.** It is insurance-shaped (the agent's operator buys a policy); the market design Locard is moving to (the counterparty as protection buyer and sole payee, outside protection sellers, incident rulings and payouts) is not built and not tested here.
- **The participants are deterministic programs.** The buyer plays a Locard-insured agent; an LLM-driven buyer (Town's `quote-llm` profiles) was not tested with a policy token, and model-agnostic token handling is unproven.
- **One task shape.** Only Town's quote task, at 3,990 cents, well under the policy's 100,000-cent cap. Over-cap orders, expired policies and fingerprint (model-swap) mismatches are covered by Locard's own tests, not by Town runs.
- **Crash faults not exercised.** In `quote-crash-restart` and `quote-amnesia-restart` the crash is acted out by Town's own scripted seller, which this integration replaces. With `vendor.py` in the seller role nothing crashes, and Town correctly reports those runs as INCOMPLETE ("no lease end followed by redelivery"). Whether the vendor survives a real crash between verifying cover and acknowledging is untested; its journal is designed for it, but no run shows it.
- **No portable identity.** Runs use Town's short-lived join tokens; Locard's agent id is not bound to a Town identity grant (`--identity`).
- **Local only.** Locard runs on `127.0.0.1`; the shared coordinator and a remote Locard were not tested.
- **Evidence contents.** Each bundle's `intents.jsonl` holds the buyer's quote request, including a Locard policy token. Those tokens are signed by a throwaway key on a local Locard for fictional agents and are useless elsewhere; no API keys or personal data are in the evidence.
- The stand-in Locard used by the tests is not Locard: it mirrors the API shapes and the suspension rule only.
