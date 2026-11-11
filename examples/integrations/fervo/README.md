# Fervo: agent contributions under Town's at-least-once delivery

> MIT AI Studio, Fall 2026, HW8 submission. This example is a course
> integration, not evidence of broad Fervo or Town compatibility.

## Summary

[Fervo](https://github.com/PhilStrub/fervo-dev) is repo management for agent
contributors: a server, runner and CLI that let many coding agents improve one
Git repository. An agent claims a scope, commits in its own worktree and
submits. The runner tests and measures the change against its parent, and the
server merges it only when the policy is met. Every write carries an
idempotency key.

This example connects a Town **Track** seller to a live Fervo server. Its unit of
work is a Fervo contribution. The question it answers:

> When Town redelivers work to a Fervo contributor under its fault profiles,
> does Fervo still record exactly one claim, one node, one merge and one score
> change?

## Integration

| Town | Fervo |
|---|---|
| Buyer sends `quote_request` `q-1` | a task: raise `score.txt` in the managed fixture repository |
| Seller claims the request under a fenced lease | `fervo claim --parent <node> --scope score.txt --checkout --key town-<run>-q-1-claim-0` |
| Seller applies the work | a deterministic commit in the lease's worktree, then `fervo submit --key town-<run>-q-1-submit-0` |
| Seller responds and acknowledges | waits until Fervo reports `merged`, replies with the correct total plus a `fervo` block (node, state, merge commit), acks `processed` |
| Town's redelivery, fences, journal | Fervo's `(actor, key) → response` idempotency table |

The quote payload is unchanged. The side effect of answering it is a real Fervo
contribution, evaluated and merged by a real runner.

`fervo_contributor.py` is a standard-library participant, like
`examples/byoa_seller.py`. It calls the Fervo CLI for keyed writes, which do the
Git work, and reads `context` and `status` over HTTP. Its replay rules:

- **Keys are derived, never random.** They come from the Town run id, the
  message id and the step. The run id is included because Town's buyer uses
  `q-1` in every run.
- **The commit is byte-identical on every retry:** fixed author, committer,
  dates and message. A replayed `submit` therefore sends the same request.
- **The chosen parent is recorded before the claim**, in the seller's
  `STATE_DIR`. After its own merge `main` has moved, and re-reading it would
  turn a replayed claim into a different request. Fervo refuses that with
  `idempotency key reused with a different request`. Everything else (lease,
  worktree, node, merge) is recovered by replaying keys.
- **`next_action` drives the loop:** `wait` polls, `rebase` claims again with
  `--supersedes`, `retry` requeues a failed evaluation, and `stop` answers.
- **`applied: true` goes on every acknowledgement except a re-offer.** Town
  flags as `duplicate` only work it already recorded as processed. Any other
  delivery means no acknowledgement has been recorded yet.

The faults `duplicate_delivery` and `lost_ack` are injected by the coordinator.
`crash_after_claim` is acted on by the participant, as Town's scripted seller
does: after the first Fervo claim it stalls past its Town lease, shows that the
stale fence is refused, and exits with code 3 so the runner restarts it.

Topology: the Fervo server (bare repository + SQLite) runs in a throwaway Lima
VM, `fervo-hw8`, defined by [`lima/fervo-host.yaml`](lima/fervo-host.yaml) with
a pinned image digest and no host mounts. The Town coordinator, buyer, this
seller, the Fervo runner and the contributor clone run on the host, so every
push and API call crosses the VM boundary through `127.0.0.1:8775`.
`setup_fervo.sh --local` runs the same steps without a VM.

## Reproduce

Requirements: Python 3.11+, Node 24+, Git, and Lima 2.x for the default mode.

1. Town, from this checkout:

   ```sh
   python3 -m venv .venv && . .venv/bin/activate
   python -m pip install -e '.[dev]'
   ```

2. A Fervo build archive from the pinned commit (see *Versions*):

   ```sh
   git clone https://github.com/PhilStrub/fervo-dev fervo && cd fervo && git checkout v0.1.0-hw8   # cafaa82, public main
   npm ci && npm run build
   tar czf ../fervo.tgz package.json package-lock.json dist/src skill
   ```

3. The matrix (under a minute per row; a first `--lima` run also downloads the
   Ubuntu image and Node into the VM). Port 8775 must be free on the host:

   ```sh
   export FERVO_TGZ=$PWD/fervo.tgz FERVO_REF=cafaa82
   examples/integrations/fervo/run_matrix.sh --lima      # or --local
   examples/integrations/fervo/teardown_fervo.sh
   ```

   For each row, this resets Fervo to an empty database
   (`setup_fervo.sh`). It then runs

   ```sh
   nandatown test-agent --role seller --profile <profile> \
     --cmd "python3 examples/integrations/fervo/fervo_contributor.py"
   ```

   and cross-checks the bundle with `check.py`, which reads Fervo's event log
   with the operator token. Finally it runs `nandatown verify` and
   `nandatown receipt`, and copies the evidence into `evidence/<row>/`.

4. Offline, with no Fervo needed: `python -m pytest -q tests/test_fervo_example.py`
   verifies the committed bundles and pins the replay rules.

Tokens are written only under `$FERVO_WORK/tokens` (mode 700), outside the
repository. The seller's agent token is in its clone's Git config, as `fervo
clone` stores it. The adapter strips `FERVO_TOKEN` from the CLI's environment,
so an operator token in the shell is never used for the seller.

## Results and limitations

Recorded under `--lima` against Fervo `cafaa82`; every bundle in `evidence/`
passes `nandatown verify`, and its receipt passes `verify-receipt --bundle`.
"Deliveries" counts Town's claims of `q-1` plus its injected re-offer.

| Row | Town fault | Expected | Town verdict | Deliveries | Fervo claims / nodes / merges | `score.txt` | `check.py` |
|---|---|---|---|---|---|---|---|
| clean | none | 1 / 1 / 1, 10 → 20 | PASSED | 1 | 1 / 1 / 1 | 10 → 20 | PASS |
| duplicate-delivery | processed work offered again | re-offer replays claim and submit; still 1 node | PASSED | 2 | 1 / 1 / 1 | 10 → 20 | PASS |
| lost-ack | first `processed` ack dropped (503), retried | applied once | PASSED | 1 | 1 / 1 / 1 | 10 → 20 | PASS |
| crash-restart | seller exits after its Fervo claim; stale fence refused; restarted | the restart replays the claim key and gets the same lease | PASSED | 2 | 1 / 1 / 1 | 10 → 20 | PASS |
| **fresh-keys-crash** (control) | as crash-restart, new key per delivery | more than one claim | PASSED | 2 | **2** / 1 / 1 | 10 → 20 | **FAIL** |
| **fresh-keys-duplicate** (control) | as duplicate-delivery, new key per delivery | a second contribution | INCOMPLETE | 2 | **3 / 3 / 2** | **10 → 30** | **FAIL** |

Every derived-key row matched its expectation. In crash-restart the seller died
holding Fervo lease `l_…` with no commit; the restarted process replayed
`claim-0`, received the same lease, reused the worktree the crashed process
had checked out, committed and
submitted once. In duplicate-delivery the re-offer ran the whole Fervo loop
again: `claim` and `submit` returned the stored responses, and the
acknowledgement under the offer's fence was marked `duplicate`. Every
derived-key row merged the same commit, `f276473`, because the commit is
deterministic.

The controls show the boundary. With fresh keys:

- **fresh-keys-crash:** Fervo created a second claim for the restarted
  seller, so the crashed claim is left active (two hours, overlapping the same
  scope). Town still **passes** this run: its fences cannot see the side
  effect.
- **fresh-keys-duplicate:** the re-offer became new work. Its claim on `n_0`
  produced a node that got `needs_rebase`, the adapter superseded it on the new
  `main`, and Fervo merged a second contribution: one Town request raised the
  score twice. Town noticed only indirectly. The new response named a
  different node, so the coordinator refused it as `identity_reuse`; the seller
  acknowledged the re-offer `failed`, and the run is INCOMPLETE at
  `duplicate_recognized`.

What this shows about compatibility: Fervo's exactly-once contribution holds
under Town's redelivery, lost acknowledgements and seller restarts, provided
the agent derives its Fervo keys from the work's identity (run and message). It
is the agent's keys, not Town's delivery semantics, that provide it: Town
passed one of the two broken runs.

Leases: Town has no lease renewal; its leases are 5 s, or 1.5 s under
`quote-crash-restart`. Under Lima a full pass from Town claim to ack took
about 0.96 s (claim 0.28 s, submit 0.43 s, evaluation and merge 0.2 s), and a
replay pass about 0.5 s, so no lease expired in the recorded runs except the
injected one. Work slower than the lease (a real test suite, model review)
would have its acknowledgement refused as stale and be redelivered; the adapter
treats that as one more replay, but this path is not in the evidence.

Not tested:

- Model judgment and runner review. The fixture policy is `tests` +
  `frontier_gain`, so no API keys are used.
- Edits by a real LLM. The edit is a deterministic `+10` to `score.txt`.
- Claude Code enrollment, `delegate`, and human `review`, `veto` or `merge`.
- Town portable identity (`--identity`), Lab layers, A2A and MCP.
- A real network. The Lima VM is a separate OS, disk and network stack on the
  same host, reached through a localhost port forward. Latency, packet loss and
  TLS are not exercised.
- A crash of the Fervo server itself. Town's faults hit the seller.
- `quote-amnesia-restart`. The participant does not act on `crash_amnesia`.
  Losing `STATE_DIR` would lose the recorded parent; Fervo would then refuse
  the replayed claim key rather than duplicate the work, which is not exercised
  here.

## Versions

| Component | Version |
|---|---|
| Town | `6f9a7c0` (projnanda/nandatown main), `nandatown 0.2.0`, evaluator `0.5.0` |
| Fervo | `0.1.0` at `cafaa82` (tag `v0.1.0-hw8`); archive sha256 in `evidence/*/versions.txt` |
| Host side | macOS 14.4.1 arm64, Python 3.11.4, Node v26.10.0, Git 2.39.2 |
| VM | Lima 2.2.1, Ubuntu 26.04.1 (release-20260927, digest in the yaml), Node v24.22.1 |

## Files

| File | Purpose |
|---|---|
| `fervo_contributor.py` | the Town seller that drives the Fervo CLI |
| `check.py` | cross-checks a Town bundle against Fervo's event log |
| `setup_fervo.sh`, `teardown_fervo.sh` | fresh Fervo server, runner and clone; cleanup |
| `run_matrix.sh` | runs every row and collects evidence |
| `lima/fervo-host.yaml` | the VM definition |
| `fixture/` | the managed repository: a tests command and a `score` metric read from `score.txt` |
| `evidence/<row>/` | `bundle/` (five canonical files, manifest, attestation, receipt), `fervo_events.jsonl`, `fervo_check.{json,txt}`, the seller's log, `verify.txt`, `versions.txt`, `outcome.txt` |
