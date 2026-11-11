# Cerata preflight: legal checks for agent actions on the Track

Cerata is a hosted legal check for AI agents. Before an agent calls, texts, emails,
lists a home or posts a job, it tells Cerata what it is about to do. Cerata answers
**clear, flag, hold or block**, with a signed receipt anyone can verify and, for a
clear, a single-use token for that one action.

This example connects the hosted service to Town without moving it into Town. Cerata
runs separately; Town reaches it over HTTPS and over its MCP endpoint. Cerata's
internals are out of scope: the check treats it as a black box and judges only what
an agent sees.

| File | Role |
| --- | --- |
| `cerata_seller.py` | Track seller. A thin, stateless adapter: claims a `quote_request` carrying the action, calls `POST /v1/preflight`, replies with the decision, receipt and token. |
| `agent_buyer.py` | Track buyer: an agent that must ask before it acts. Sends one case, judges the answer, verifies the receipt with Cerata's public `POST /v1/verify`, and uses any token once. |
| `cases.json` | 22 situations a sales, marketing, leasing or recruiting agent meets, with the answer Cerata must give. |
| `check.sh` | The compatibility check: 40 Track runs plus an MCP check, each compared with its expected verdict. |
| `mcp_check.py` | Uses Cerata through MCP as an MCP host would. |
| `results.py` | Verifies every bundle with `nandatown verify` and prints the results tables. |
| `fake_cerata.py` | A **Town fixture**, not Cerata: answers from `cases.json` so CI can test the wiring offline. A pass against it is not compatibility evidence. |

## What is being tested

**Interface.** The Track mailbox exchange (join, claim under a lease, fenced
acknowledgement, redelivery) between two `cmd:` participants, with Cerata's REST API
behind the seller and its public verifier behind the buyer; plus Cerata's MCP endpoint.

**Right answers through shared infrastructure.** Each case is a situation with an
expected answer. Exact cases must get one decision; never-clear cases must get block,
flag or hold, and no token, where more than one cautious answer is acceptable. The
situations cover state limits stricter than federal ones, the recipient's location
and local time (including a daylight-saving change and a UTC timestamp), consent and
do-not-call facts, a rule not yet in force, places and purposes Cerata does not cover
yet (which must never come back clear), email, and a rental listing.

**Proof an agent can check for itself.** Every receipt is verified with Cerata's
public verifier, a channel that goes through neither the seller nor Town. A clear's
token is used once and must be refused the second time. A repeated case must get the
same decision digest.

**Town's faults.** Duplicate delivery, lost acknowledgement, dropped wake-up, crash
after claim, and crash with total memory loss, on a block case and a clear-with-token
case. The adapter keeps no journal: its `Idempotency-Key` is derived from the run and
message identity, so a re-application gets Cerata's stored answer back
(`Idempotent-Replayed: true`) and the reply is byte-identical. The negative control
drops the key.

**Failure and boundary cases.** Cerata unreachable and a rejected API key must both
fail closed (no decision, no token) and pass only for an agent that expects that.
Town's stock buyer, which asks for widgets, is refused, so its run cannot complete.

## Reproduce

Requirements: Python 3.11+ and a Cerata **test** API key (`ak_test_…`). Keep the key in
the environment, never on a command line.

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'

# 1. Offline: the adapter, the buyer and the Track wiring (no key, no network)
python -m pytest -q tests/test_example_cerata_preflight.py

# 2. Optional rehearsal against the local fixture (wiring only, not evidence)
CERATA_FIXTURE=1 examples/integrations/cerata-preflight/check.sh runs/cerata-fixture

# 3. The compatibility check against Cerata
export CERATA_URL=https://clearance-kohl.vercel.app
export CERATA_API_KEY=ak_test_...
examples/integrations/cerata-preflight/check.sh runs/cerata
python examples/integrations/cerata-preflight/results.py runs/cerata
```

One case by hand:

```bash
CERATA_CASE=fl-call-after-8pm nandatown run quote-amnesia-restart \
  --agent "seller=cmd:python examples/integrations/cerata-preflight/cerata_seller.py" \
  --agent "buyer=cmd:python examples/integrations/cerata-preflight/agent_buyer.py"
```

`CERATA_SECTIONS` picks sections (`law faults control repeat mcp`); `CERATA_REPEATS`
sets the listing repeats (default 3). Bundles are written under the output directory
with `summary.tsv`. They hold signed receipts from a private test environment, so the
PR publishes the results tables and bundle fingerprints rather than the bundles;
rerunning the check reproduces them with any test key. `check.sh` fails if the API key
appears in any output.

## What a pass does and does not show

It shows that Cerata, reached as an independently hosted service, gave Town's
participants the expected answer for these situations; that its receipts and tokens
verify through a channel neither the seller nor Town controls; that its idempotency
makes a stateless adapter safe under Town's delivery and crash faults; and that the
integration fails closed.

It does not show correctness beyond these situations, how Cerata reaches its answers,
the human review path (holds are reported, not resolved), latency or load beyond a
sequential run, identity-pinned runs (`--identity`), or a deployed NANDA Index. The
buyer's `correct` is its own assertion, recorded by Town.
