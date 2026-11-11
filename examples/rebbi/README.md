# Rebbi in Nanda Town

Rebbi lends spare token budget between the AI agents of one fleet. When an agent is about to run out
in the middle of a job, it calls `post_handoff` over MCP. Rebbi finds an agent with budget above its
reserve, books the loan in its ledger, and answers `LEND` (or `CANNOT_MOVE`, with each lender's
reason). When the job is done, `settle` returns what the borrower did not spend.

This example runs Rebbi's real MCP server inside a Lab scenario. Town agents use it as shared
infrastructure while they do a paid job. Town also breaks the delivery on purpose: it drops Rebbi's
answer, or it delivers a request twice.

**What Town found:** before the fix, one request that reached Rebbi twice booked two loans, and one of
them was never settled. Rebbi's own `rebbi check` still passed. Rebbi now takes a `request_id` and
returns the first decision when a request comes again.

## How it connects

```
client ──job_request──▶ worker-01 ──budget_query / borrow_request / usage / settle_request──▶ rebbi (adapter)
   ▲  escrow (Town payments)  │                                                                   │ one MCP stdio session per agent
   └────────job_result────────┘          lender-a, lender-b ──headroom──▶ rebbi                    ▼
                                                                                   python -m rebbi.mcp_server
                                                                                   (REBBI_AGENT_ID = sender)
```

- **Rebbi** is the subject. It is hosted in its own repository and runs in its own virtual
  environment. The repository is private for now; graders can get access on request.
- **`rebbi_adapter`** is the only role that talks to Rebbi. It starts one Rebbi MCP server per fleet
  agent (`python -m rebbi.mcp_server`), because Rebbi takes an agent's identity from
  `REBBI_AGENT_ID` and never from a tool argument. Each Town message that gets delivered becomes
  exactly one tool call (`report_headroom`, `get_budget`, `post_handoff`, `settle`), made in the
  session of the sender that Town's auth layer verified. The adapter never removes duplicates, so
  Rebbi sees exactly what Town delivered.
- **`rebbi_meter.py`** runs in Rebbi's own interpreter. It records each work step through Rebbi's
  `Ledger`, the same way Rebbi's gateway records model calls: a step the balance cannot cover is
  refused. At the end it reads Rebbi's ledger and runs Rebbi's own invariant check.
- **`lender-a`, `lender-b`, `worker-01`, `client`** are Town scripts written for this test, not
  independently developed agents. The worker keeps a journal, as Town asks of every participant: a
  second copy of an answer it already acted on is recorded and ignored. If no answer arrives, it
  resends the same request once, with the same `request_id` and the same content.
- **Town parts used:** transport, including the drop and duplicate faults; signed and verified
  senders; escrow in the payments ledger; and the evidence pipeline.

`fleet.toml` is Rebbi's own scenario file for these runs. `worker-01` has 7,000 tokens and the job
needs 25,000. `lender-a` can lend 80,000 above its reserve; `lender-b` has nothing above its reserve.

## The workflow

1. Both lenders publish their reserve (`report_headroom`).
2. The client holds 5,000 cents in escrow and sends the job to `worker-01`.
3. The worker reads its budget (`get_budget`) and asks for the 18,000-token shortfall (`post_handoff`).
4. With `LEND`, it works in three metered steps (8,000 + 9,000 + 6,000 tokens).
5. It delivers. The client releases the escrow to the worker.
6. The worker calls `settle`. The 2,000 tokens it did not spend go back to `lender-a`.
7. Rebbi's ledger is read back and checked.

| Case | What Town breaks | What must hold |
| --- | --- | --- |
| `rebbi_lend` | nothing | one loan, job paid, loan settled with 2,000 returned, Rebbi's invariants pass |
| `rebbi_lost_reply` | drops Rebbi's first `borrow_decision` | the worker's retry gets the original loan back; still one loan, settled |
| `rebbi_duplicate` | delivers the `borrow_request` to Rebbi twice | the second copy gets the original loan back; still one loan, settled |
| `rebbi_no_room` | `lender-a` raises its reserve to 175,000 | `CANNOT_MOVE` with each lender's reason, nothing spent, no loan, client refunded |

## Reproduce

Town base commit `6f9a7c0`, evaluator `lab-0.2.6`, seed 42. Rebbi `07da056` (with the fix)
and `3db016d` (before it). Recorded on Windows with Python 3.13. No model key is needed.

From the root of this checkout (on Windows, use `.venv/Scripts/python`):

```bash
git clone https://github.com/eglismella/rebbi.git ../rebbi      # private for now: ask for access
git -C ../rebbi checkout 07da0565da42d74a224dcd2148c03eac0105d37a
python -m venv ../rebbi/.venv && ../rebbi/.venv/bin/python -m pip install -e ../rebbi
python -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python examples/rebbi/run.py --project ../rebbi --case all --out runs/rebbi
```

`run.py` seeds a new Rebbi ledger for each case using Rebbi's own CLI, runs the scenario, prints every
stage and Rebbi's ledger, and exits non-zero unless every case passed. To see the failure Town found,
check out `3db016d` and run `--case lost-reply` and `--case duplicate`: both fail at
`one_loan_per_request`.

The recorded evidence verifies without Rebbi. `nandatown verify` does not load scenario plugins, so use:

```bash
.venv/bin/python examples/rebbi/run.py --verify examples/rebbi/evidence
.venv/bin/python -m pytest -q tests/test_example_rebbi.py
```

The test checks that each bundle verifies, that it has the verdict reported below, and that it names
the Rebbi commit and the exact adapter and fleet files (by digest) that produced it. With
`REBBI_PROJECT=../rebbi` set, it also runs all four cases live.

## Results

Every stage checks only the recorded events. Each bundle also has Town's `ledger_conserved` check
(the client's 10,000 cents are conserved).

| Bundle (`evidence/`) | Rebbi | Expected | Observed | Bundle fingerprint |
| --- | --- | --- | --- | --- |
| `before-fix-lost-reply` | `3db016d` | one loan; the retry gets loan 1 back | **FAILED** at `one_loan_per_request` (loans 1 and 2 for one request), `loans_settled` (loan 1 still active), `retry_gets_original` | `sha256:8dbee6d0376f8c8a12696f16efc3ac8354c82121cfd950f9cc574fbcf8f1f1dc` |
| `before-fix-duplicate` | `3db016d` | one loan; the second copy gets loan 1 back | **FAILED** at `one_loan_per_request` (loans 1 and 2), `loans_settled` (loan 2 still active), `duplicate_gets_original` | `sha256:1f5fe0f5d0dac59fcd82daa6a07ff385d7cabe0dea442e14745aaf15ee09494e` |
| `after-fix-lend` | `07da056` | one 18,000-token loan, job paid, 2,000 returned | **PASSED**, 9 of 9 stages | `sha256:e6fc43eda0a813597f42c60bbc2224f13fa24c7afb80fecee71308e4736ccbc1` |
| `after-fix-lost-reply` | `07da056` | one loan; the retry gets loan 1 back | **PASSED**, 12 of 12 | `sha256:0562f40e115cb9c990c478aae2d0757ce6951db21e35cbf04c9e17d883acba9e` |
| `after-fix-duplicate` | `07da056` | one loan; the second copy gets loan 1 back | **PASSED**, 11 of 11 | `sha256:ae566fb4f04f320c6221c9fbaa48156e86df26f127c3a7fb7375abd3f2bafcc5` |
| `after-fix-no-room` | `07da056` | `CANNOT_MOVE` with reasons, nothing spent, refund | **PASSED**, 9 of 9 | `sha256:4fcd7873b52d15b32f2cf6320ea7fe75f2244279b472b6651d1e41ae41650b03` |

Before the fix, `rebbi_invariants` passed in both failing runs: Rebbi's own check could not see the
orphaned loan.

## What this shows, and what it does not

**Shown.** Rebbi's MCP server works as shared infrastructure for agents it was not written with. It
holds live stdio sessions with the expected tools, the sender's identity carries through, lending
respects every reserve, the borrower's unspent tokens go back, and both ledgers balance: Rebbi's
tokens and Town's cents. Under Town's at-least-once delivery, a repeated request now books one loan,
not two.

**Found and fixed.** Before the fix, `post_handoff` had no request identity. A retry after a lost
reply, or a duplicated delivery, booked a second loan that nobody settled: 18,000 of `lender-a`'s
tokens were tied up until the loan expired. Rebbi's invariant check passed, because each loan was
sound on its own. The fix (Rebbi commit `07da056`) adds an optional `request_id`. Within one
database transaction, Rebbi returns the first decision marked `replayed`, refuses the same id used
for a different request, and books nothing new. It also makes `settle` safe to resend. Rebbi's own
tests cover concurrent copies across separate database connections and the migration of older
ledgers.

**Not tested here.**
- Real model-driven agents deciding when to borrow. Town scripts stand in for them.
- Rebbi's HTTP gateway and its auto-borrow on a refused model call.
- Loan expiry. Rebbi uses a 900-second wall clock; the Lab runs on logical time.
- Several borrowers competing for one lender at the same moment (covered only by Rebbi's unit test).
- A lost `settle` reply inside Town (settle retry is covered only by Rebbi's unit test).
- Track mode, agents on other machines, A2A, discovery through a NANDA Index, and scale.

The scenario does not declare redacted fields, so Town's privacy stage is `not_tested`.
