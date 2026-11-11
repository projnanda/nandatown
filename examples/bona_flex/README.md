# bona_flex: an energy-flexibility Lab pack backed by a real trust service (VeriFlex / Bona)

Buyer agents compete for one seller's flexibility (load reduction) in a NandaTown Lab run. The trades are mediated
by a desk agent that calls Bona, a real MCP trust service (`python -m bona.server`). Bona assesses offers, signs
holds and confirmations, verifies delivery against metered readings, and keeps per-seller trust. Its receipts are
Ed25519 signatures over RFC 8785 canonical JSON. Town's Lab engine supplies the clock, the seeded transport faults
(a dropped response, a duplicated request), escrow, privacy redaction, the evidence bundle and its attestation.
The `bona_flex` validator judges each run from Town's events alone.

All data is synthetic. Bona's private code is not needed to check the recorded evidence.

## Files

| File | What it is |
|---|---|
| `bona_flex_roles.py` | Lab roles: `flex_seller`, `flex_meter`, `flex_buyer`, `flex_desk` (real Bona over MCP) and `flex_desk_naive` (the control: no Bona, no receipts) |
| `bona_link.py` | Minimal stdlib MCP client (JSON-RPC over stdio) used by the desk |
| `bona_flex_validator.py` | `@validator("bona_flex")`: 12 stages from the trace alone; imports only `nandatown` and the standard library |
| `bona_flex_offline.py` | Stdlib canonical JSON, receipt verification, hashing (used by the checkers) |
| `scenarios/*.yaml` | `flex_clean`, `flex_race_lost_response` (two buyers, 400 kW each, for 500 kW; first response dropped, second request duplicated), `flex_race_naive_desk_control` |
| `scenarios/expected_control_stages.json` | The control's failing stage set, frozen before the first recorded run |
| `run_check.py` | One command: every scenario live on fresh synthetic worlds, with a readable timeline |
| `check_evidence.py`, `check_receipts.py`, `verify.py` | Offline checks of recorded evidence (no `bona` or `bench` import) |
| `evidence/` | The recorded runs: Town bundles plus the hash-bound Bona sidecar, `summary.json` with pins |

## Validator stages

`no_double_sell`, `acceptance_order_respected`, `retry_replays_original`, `retry_sends_identical_request`,
`duplicate_no_extra_commitment`, `settled_once`, `trust_accounted_once`, `receipts_verify`,
`money_follows_verdict`, `capacity_not_disclosed`, `clock_valid` (six clock checks), `sidecar_bound`.

Expected results:
- `flex_clean` and `flex_race_lost_response` pass every stage.
- The control fails exactly `acceptance_order_respected`, `no_double_sell`, `receipts_verify` and
  `retry_replays_original`. `clock_valid` is `not_enough_evidence` for the control, because it has no Bona clock.

## Route 1: offline verification (anyone; no Bona)

```powershell
git clone -c core.autocrlf=false -c core.eol=lf <this repository> nandatown-hw8
cd nandatown-hw8; git checkout integration/veriflex-bona-flex-lab
py -3.13 -m venv .venv; .venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python examples\bona_flex\check_evidence.py examples\bona_flex\evidence
.venv\Scripts\python examples\bona_flex\verify.py examples\bona_flex\evidence\flex_race_lost_response
.venv\Scripts\python examples\bona_flex\check_receipts.py examples\bona_flex\evidence\flex_race_lost_response
.venv\Scripts\python -m pytest -q tests\test_bona_flex_integration.py      # offline tests pass; the live test skips
```

What it shows:
- the recorded bundles and sidecars are intact;
- the validator re-judges the recorded events to the recorded verdicts;
- every receipt verifies against the pinned Bona did:key;
- the six clock checks re-derive from the sidecar;
- the control fails exactly its frozen set.

What it does not show: that Bona produces these results again. Only the live route shows that.

## Route 2: live reproduction (needs the Bona runtime)

Two Python 3.13 environments: the Bona runtime's (which runs the MCP server and the operator helper
`bench.hw8.opserver`) and NandaTown's.

```powershell
# Bona runtime (see its README): its venv from requirements.lock.txt
$env:BONA_HOME = "<bona runtime folder>"
$env:BONA_PYTHON = "$env:BONA_HOME\.venv\Scripts\python.exe"
# From the NandaTown clone above:
.venv\Scripts\python examples\bona_flex\run_check.py --out <new folder> --repeat 3
.venv\Scripts\python -m pytest -q tests\test_bona_flex_integration.py      # offline + live
```

`run_check.py` builds a new synthetic world for every run. It sets Bona's bench-only environment for that world:
- a bench ledger;
- a world-local test signing key;
- a synthetic ENGIE-format fixture rebuilt and checked against its committed pin.

It prints a timeline. Lines marked `LIVE` are printed while the run executes. Lines marked `RENDERED` are rebuilt
afterwards from the recorded events only.

## Design choices and limitations

- **Two environments.** Bona's runtime and Town's run separately. The seller behaviours and the synthetic world
  run unchanged inside Bona's environment (`bench.hw8.opserver`), reached over JSON lines.
- **Fixed run IDs.** The run ID is fixed per (scenario, seed), so reruns can be compared. Every run writes to a
  new folder; no evidence is overwritten.
- **World-local test keys.** Town's keystore and Bona's test signing key live in each world folder. They are never
  distributed.
- **Rerun comparison is normalized.** Reruns are identical once exactly three hash fields are masked:
  `receipt_sha256`, `request_sha256` and `manifest_sha256`. These cover Bona objects signed with wall-clock times.
  Raw identity is also reported, and every original integrity check still applies.
- **Control set frozen in advance.** The control's frozen failing set is kept unchanged.
- **Not prospective validation.** Some validator stages were changed after a development run, so the results are
  not a prospective validation of the final stage semantics.
- **Only buyers compete.** Seller double-selling is not tested. The second synthetic seller is enrolled but not
  traded.
- **Platforms.** Tested on Windows 11 with Python 3.13; Linux and macOS are untested.
