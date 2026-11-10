# TermsMet in Town: acceptance workflow and lost-reply recovery

[TermsMet](https://github.com/wywyou56/termsmet-runtime) is a shared acceptance
and conditional-delivery service for buyer and provider agents. Both parties
agree on one versioned CSV-to-JSON task. The buyer confirms **simulated**
funding. The provider submits records, which the server checks
deterministically, and gets one repair. After a PASS and a review window, the
server records a **simulated** release, and only then can the buyer retrieve
the delivery.

This directory runs that product inside a Town Lab scenario. It also tests one
failure: Town transport drops the reply that carries a real PASS.

## What runs

```text
Town Lab tm_buyer ── Town messages ── Town Lab tm_provider
        │                                    │
        └── tm_tool_request / tm_tool_result ┘
                         │
                    tm_adapter  (only role that calls TermsMet)
                         │  mcp_bridge.TermsMetBridge, MCP over stdio
          ┌──────────────┴──────────────┐
   TermsMet server (buyer)     TermsMet server (provider)
          └──── one fresh SQLite database per run ────┘
```

- `tm_buyer` and `tm_provider` are scripted state machines in
  `termsmet_plugin.py`. They change state only from Town messages they actually
  receive. They never read the bridge records, the observer events or
  `fixtures/expected_delivery.json`.
- `tm_adapter` maps the Town envelope sender to a fixed server identity
  (`buyer` or `provider`). Each identity is a separate real TermsMet MCP process
  with `TERMSMET_IDENTITY` set. The adapter returns each server result to the
  requester unchanged. It also records a `tm_server_call` observer event with
  the request, the result and the real request/return timestamps.
- The TermsMet server is the real public runtime. It is not a stub, and every
  verdict, deadline and settlement comes from it. The only scripted fault on the
  product side is the provider's first draft, which leaves out order A102
  (`fixtures/initial_delivery.json`).
- No LLM and no model key are used.

| File | Purpose |
| --- | --- |
| `termsmet_plugin.py` | Roles `termsmet_buyer/provider/adapter`, validators `termsmet_clean` and `termsmet_lost_reply`, `close_termsmet()` |
| `termsmet_workflow.yaml` | Clean scenario, no faults |
| `termsmet_lost_reply.yaml` | Same scenario plus one rule: drop the first `tm_repair_result` |
| `mcp_bridge.py`, `run.py` | Synchronous MCP bridge and bounded runner (see `interface.md`) |
| `fixtures/` | Source CSV, controlled first draft, independent expected delivery (used only by the validator) |
| `evidence/clean`, `evidence/lost_reply` | The two recorded bundles |
| `project_version.json` | Pinned versions and settings |
| `YOU_HANDOFF.md` | Historical stage-one handoff notes |

## Reproduce

Both environments use Python 3.11.5 (check that `python3.11 --version` prints
`Python 3.11.5`) and `mcp==2.3.0`. The runner refuses to
start when the versions differ from `project_version.json`. Live runs need
macOS or Linux, because the runner uses `ps` to supervise processes.

Run these from one parent directory, so `termsmet-runtime/` and `nandatown/` are
siblings and `--project ../termsmet-runtime` resolves from inside `nandatown/`.

```sh
git clone https://github.com/wywyou56/termsmet-runtime.git termsmet-runtime
git -C termsmet-runtime checkout 7ee09295226d2a41555293f391e13a8c46020031
python3.11 -m venv termsmet-runtime/.venv        # or: uv venv --python 3.11.5 --seed termsmet-runtime/.venv
termsmet-runtime/.venv/bin/python -m pip install -r termsmet-runtime/requirements.txt

git clone https://github.com/wywyou56/nandatown.git nandatown
cd nandatown
git switch integration/termsmet-acceptance
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip install -r examples/integrations/termsmet/requirements.lock
```

**Offline verification.** This needs no runtime, server, MCP SDK or key. It
loads this plugin's validators, checks bundle integrity and the attestation,
and replays the evaluator over the recorded events:

```sh
.venv/bin/python examples/integrations/termsmet/run.py --verify examples/integrations/termsmet/evidence
# Both bundles verified
```

Plain `nandatown verify` does not load this plugin's validators, so use
`run.py --verify`.

**Live reproduction.** Each run uses a new, empty output directory and a fresh
database:

```sh
.venv/bin/python examples/integrations/termsmet/run.py --project ../termsmet-runtime --case clean --out /tmp/termsmet-hw8-clean
.venv/bin/python examples/integrations/termsmet/run.py --project ../termsmet-runtime --case lost-reply --out /tmp/termsmet-hw8-lost
```

**Tests.**

```sh
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_termsmet_*.py' -v
.venv/bin/python -m pytest -q
git diff --check
```

`tests/test_termsmet_integration.py` replays the validators over the recorded
bundles. It checks that mutated traces do not pass: a missing funding or
retrieval event, a wrong or float amount, a missing row, a wrong agreement
version, a recovery reply that never reached the provider, an early release or an added refund, a lost-reply trace without a
real drop, a dropped message that was not the PASS, a provider that received
the PASS early, an extra accepted submission, and tampered bundle files.

## Pinned versions

| Item | Value |
| --- | --- |
| Town base (`projnanda/nandatown` main) | `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244` |
| Integration code used for the recorded runs | `beee4a7` on `integration/termsmet-acceptance` |
| TermsMet public runtime | `7ee09295226d2a41555293f391e13a8c46020031` (from source `e797d7cc8f3fdabd6ff3e2ceec90433e4e6b56be`) |
| Python / MCP SDK, both sides | 3.11.5 / 2.3.0 |
| Seed, review window, first-submit window | 42, 2 s, 5 min |
| Bridge limits | 20 s startup per session, 10 s per call, 120 s per run, polling no faster than every 0.1 s |
| Evaluator | `lab-0.2.6`, nandatown 0.2.0 |

## Expected compared with actual results

Both bundles were recorded on 2026-11-10 from a clean checkout of `beee4a7`, and each
case passed on its first formal attempt. They replace an earlier passing pair
recorded from `d50be99` (still in git history). After review, that version's buyer
probed retrieval after seeing `IN_REVIEW`. Such a probe can wrongly fail if the real
deadline passes between calls, so it was removed, and the lost-reply validator was
tightened to require that the recovery status response reached the provider.

| Stage | Expected | Clean | Lost reply |
| --- | --- | --- | --- |
| `agreement_funding` | v1 proposed; provider counters to a 10-minute repair window (v2); both accept v2; stale v1 acceptance returns `VERSION_MISMATCH`; counter after lock returns `INVALID_STATE`; simulated `FUNDED` | passed | passed |
| `delivery_repair` | submission 1 FAIL with `MISSING_ORDER A102`; provider receives the report before repairing; submission 2 PASS | passed | passed |
| `release_gating` | retrieval before acceptance returns `PERMISSION_DENIED`; the buyer retrieves only after a poll observes the release; exactly one simulated funding and one simulated release, no refund; release time at or after the real review deadline | passed (19 polls, release 2.05 s after PASS) | passed (19 polls, release 2.04 s after PASS) |
| `retrieved_artifact` | buyer receives exactly the independent expected JSON (3 orders, 6 units, 3,560 cents, integer amounts); receipt shows v1/v2, acceptances on v2, submissions 1 and 2 on v2, FAIL then PASS, unchanged source hash and checker version; one job in the database | passed | passed |
| `lost_reply_recovery` | see below | n/a | passed |
| Town verdict | passed | **passed** | **passed** |

| Bundle | Run | Bundle fingerprint | Events / server calls / submit attempts |
| --- | --- | --- | --- |
| `evidence/clean` | `sim-53a1730b6f20` | `sha256:70394e5b895a83d91609da59ff0196947a4da540309e4822e5b19ad681f61ae2` | 217 / 37 / 2 |
| `evidence/lost_reply` | `sim-8c7ca6564d71` | `sha256:e50385b96823779cd95e8e1478e0b1d9985bfdd935b5463558f47413f2002039` | 228 / 39 / 3 |

### What the provider knew during recovery

In the lost-reply bundle the server returned submission 2 as a PASS (`ev-88`).
The adapter sent that result as `tm_repair_result`, and Town transport dropped
it (`ev-90`), so it was never delivered. Two logical seconds later, the provider
had no reply. It recorded the request as **`unconfirmed`** (`ev-91`). This is a
different state from the checker's `UNKNOWN`, and a missing reply is not a
failure. The provider then sent one retry with the same job and the identical
delivery, under a new request id that points back to the original. The server
rejected the retry with `INVALID_STATE` and created no new submission. The
provider did not treat the rejection as evidence for either verdict. It
queried `get_job` (status `IN_REVIEW`) and `get_result(job, 2)` (PASS on
agreement v2). Only after both replies arrived did it record `confirmed_pass`
(`ev-108`) and tell the buyer the delivery was ready. The validator requires
both replies to have been delivered to the provider first. The validator also
checks that the dropped message was the server's actual submission-2 PASS and
not some other message of that kind.

## Two clocks

Town Lab time is **logical**. `later(2)` does not let two real seconds pass.
TermsMet deadlines use **real** UTC time. The buyer therefore sleeps 0.1 s of
real time before each release poll, and a poll after the deadline causes the
server to record the release. The `tm_server_call` events carry real
timestamps. Event `at` values are logical. Each live run took about 2.1 s of
real time between the first and last server call: 10.3 logical seconds for
the clean run and 12.5 for the lost-reply run.

## Limitations

- The participants are scripted state machines. These runs do not test LLM
  decisions, independent adoption, A2A or full NANDA conformance, crashes,
  concurrency, or more than one fault.
- Funding, release and refund are simulated records, not payments or custody.
- The roles respect their knowledge limits by convention, because the plugin
  is written that way. Nothing isolates them as a sandbox would. Server
  identities come from process environment variables, not from cross-organization
  authentication.
- `tm_server_call` events and receipts are what the adapter observed from the
  server. They are not independent proof of the server's internal state. The
  independent checks are the hand-computed expected delivery and the
  fixture's source hash.
- Validators read `fixtures/` at replay time, so offline verification needs this
  checkout.
- Each case was recorded once. Live reruns produce new job ids, timestamps and
  fingerprints, while the stage results should stay the same.
