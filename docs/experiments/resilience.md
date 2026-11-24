# Shared-dependency experiment (opt-in)

This bundled Lab experiment compares five selection policies when different
provider identities share infrastructure. It uses the existing deterministic
Lab engine, not Track, live services, model inference, or a new protocol default.
No credentials, model/coco/dotenv dependencies, external requests, or real money
are needed. Importing `nandatown` or the experiments namespace does not register
anything. Running the experiment or explicitly importing its `plugin` does.

## Install and run

From a checkout in an activated Python 3.11+ virtual environment:

```console
python -m pip install -e ".[dev]"
nandatown-resilience run --condition shared --policy dependency --seed 7 --out runs/resilience-one
nandatown-resilience verify runs/resilience-one
```

`python -m nandatown.experiments.resilience` is equivalent to
`nandatown-resilience` for every subcommand. Output directories must be new;
existing evidence is not overwritten. A run writes the native profile, run
record, events, intents, result, public attestation, and `metrics.json`.
Signing keys are generated in an isolated directory alongside the output and
removed after signing; no personal keystore or live run data is copied.

Settings include `--buyers`, `--rounds`, `--attempts`, `--interval`,
`--service-time`, `--dependency-capacity`, and `--disclosure` (0 to 1).
The default workload has eight buyers, 20 tasks each, at most three attempts,
and 12 providers: nine share one backend; three have distinct backends.
Disclosure is a seeded, nested mask, rounded to a whole provider count.
Declarations are truthful in the original three conditions. The optional
`shared-incorrect` condition changes one declaration, as described below.
Missing declarations form one conservative unknown group.

## Campaign and report

```console
nandatown-resilience campaign --seeds 0 --disclosures 0 1 --out runs/resilience-smoke
nandatown-resilience campaign --out runs/resilience-450
```

The smoke design has 30 runs. Defaults produce 450 runs: ten paired seeds,
three disclosures (0, 0.5, 1), three conditions, and five policies.
`--conditions` selects a subset of conditions; its default remains
`clean shared independent`, so the new case does not change the 450-run recipe.
`--keep-all` retains every bundle; otherwise only the first seed's bundles
are retained for each condition/disclosure/policy (45 for the full design).
All runs undergo scoped trace evaluation; retained bundles additionally undergo
native bundle verification. Outputs are `summary.json`, `runs.csv`, and a
self-contained, script-free `report.html` with seed-paired percentile bootstrap
intervals (2,000 resamples). One seed cannot estimate an interval.

Do not commit generated campaigns. The original published
[450-run dataset](https://github.com/Shereen20112003/SA_NANDATOWN/tree/main/results/published)
and [historical findings](resilience-findings.md) provide prior evidence;
they are not results of a fresh run on this upstream checkout.

## One incorrect dependency declaration

`--condition shared-incorrect` differs from `shared` in exactly one advertised
fact: **provider-00 declares backend-1 but still sends work to backend-0**.
Backend-1 is the real dependency of provider-09. This represents one incorrect
static declaration (for example, an old declaration left after a migration);
no migration or time-to-live mechanism is simulated.

Actual providers, prices, capacity, workload, disclosure mask, retry limit, and
backend-0's outage at `[12, 28)` are unchanged. The declaration is published only
when provider-00 falls within the disclosure mask. At zero disclosure, this case
must behave like the zero-disclosure truthful baseline.

Discovery and policy replay use the advertised label; execution, outage checks,
and actual-dependency metrics use the real backend. Only the existing oracle
uses the real labels for selection. No policy repairs or detects the bad label.
This keeps the case focused on the assumption, not a new mitigation.

### Reproduce the comparison

With the installed CLI, run this in PowerShell (use a new output directory):

```powershell
nandatown-resilience campaign --conditions shared shared-incorrect --disclosures 1 --seeds 0 1 2 3 4 5 6 7 8 9 --out runs\incorrect-comparison
```

This produces **100 runs / 16,000 tasks**: both conditions, all five existing
policies, and ten paired seeds. The original baseline and controls are rerun,
not copied from the historical dataset. Open the generated `report.html` for
completion and cost tables, or inspect `summary.json` / `runs.csv`.
By default the ten seed-0 bundles are retained and verified.
For a ten-run smoke comparison, replace the seed list with `--seeds 7`.

Individual scenario generation, execution, and verification also support the case:

```powershell
nandatown-resilience run --condition shared-incorrect --policy dependency --seed 7 --disclosure 1 --out runs\incorrect-one
nandatown-resilience verify runs\incorrect-one
python -m pytest -q tests\experiments\test_resilience_incorrect.py tests\experiments\test_resilience_cli.py
```

The [new paired findings](resilience-findings.md#incorrect-declaration-follow-up)
show unchanged completion but higher cost and retry use, rather than assuming
one incorrect declaration must cause task loss. Regressions pin these exact
metrics, prove physical routing is unchanged, reject replay with the hidden true
label substituted, verify bundles and repeatability, and check the oracle and
declaration-insensitive controls.

## Scenario generator and native plugin

```console
nandatown-resilience scenario --policy price --seed 7 --out runs/resilience.yaml
```

This prints a native command containing the installed `plugin.py` path. For
PowerShell, a path independent of the source checkout can also be obtained
without registering the plugin:

```powershell
$plugin = python -c "from pathlib import Path; import nandatown.experiments.resilience as r; print(Path(r.__file__).with_name('plugin.py'))"
$env:NANDATOWN_HOME = Join-Path $PWD "runs\resilience-native-keys"
nandatown run runs\resilience.yaml --plugin "$plugin" --layer coordination=resilience.dependency.v1 --out runs\resilience-native
```

Native Lab creates a run-id subdirectory under `--out`; pass that subdirectory
to `nandatown-resilience verify`. Native runs use the standard Town keystore
(isolated above), unlike the experiment wrapper's disposable signer.
Only load trusted local plugin files: loading a plugin executes Python.
The plugin registers four roles (`resilience_clock`, `resilience_backend`,
`resilience_provider`, `resilience_buyer`), the `resilience.v3` evaluator
(plus `resilience.v2` for historical truthful bundles), and:

| Coordination plugin | Selection |
| --- | --- |
| `resilience.price.v1` | Price, reputation, seeded tie-break |
| `resilience.random.v1` | Seeded pseudo-random rank |
| `resilience.load.v1` | Provider in-flight load, then price |
| `resilience.dependency.v1` | Failed-group avoidance, group load, provider load, price |
| `resilience.oracle.v1` | Same rule with all dependency identities; no future outages |

The oracle is an information-rich comparator, not a proven optimum. Reservations
are a consistent shared coordination service, not decentralized discovery.
Provider-local failures exclude only the provider; explicit backend outage or
capacity failures can exclude the group for the remainder of that task.
The evaluator supports this declared layer stack and service-outage design,
not arbitrary layer swaps, altered populations, or additional transport faults.
Generate the seed with `scenario --seed`; do not change it with native
`run --seed`, since the embedded world and policy schedule must stay paired.

## Reproducibility, provenance, and limits

Identical settings reproduce metrics and `logical_trace_fingerprint`, not
byte-identical bundles: run/event IDs, signing keys and attestations vary.
Replay checks causality, policy choices, workload, outages, capacity, payments,
and terminal evidence. Missing completion is incomplete, not a pass.
`resilience.v3` separates declared labels in selection from physical dependencies
in execution for the new case. `resilience.v2` remains registered for historical
truthful cases and rejects `shared-incorrect`; old evidence is not reinterpreted
as if it supported incorrect declarations. Both preserve the corrected behavior
that provider-local outages must not poison a whole group.

Run releases and campaign summaries record the executing Nanda Town version,
bundled experiment version, source experiment version (0.1.0), and the original
**compatibility baseline** `b7adc6121868742cf0f00cfebbf2d38bdb349057`.
The baseline is deliberately not labeled the executing Git commit. No executing
commit is inferred from an installed package. Record `git rev-parse HEAD` and
`git diff` separately when comparing source checkouts, including local changes.
Native `nandatown run` retains its existing release metadata conventions.

Costs are synthetic successful-task prices; failures still consume messages and
retry budget. The original full-disclosure clean run paid a 30% diversity premium
(13 versus 10 cents). Shared-outage improvements did not generalize to staggered
provider-local outages: the candidate's point estimate was worse than price,
with an interval crossing zero. Zero disclosure does not reveal hidden backends.
The independent control matches total provider downtime, not instantaneous
capacity or each task's exposure. Stress metrics use tasks created in [12, 28),
including retries that finish later; this window is fixed for every condition.

Truthful labels (except the one explicit incorrect declaration) and structured
failure reasons are assumptions, not verified infrastructure facts. There is no
inferred dependency graph, general adversarial label model, multi-hop dependency
model, real network, or production resilience
certificate. These are bounded synthetic controls, not universal novelty,
MIT affiliation, endorsement, or independently adopted compatibility claims.
Shared-risk routing has established prior art.

## Tests and imported license

```console
python -m pytest -q tests/experiments
```

The original 100 experiment and four CLI cases are retained, with additional
bundling regressions for opt-in registration, metadata, and entry points.
The nine deterministic modules, plugin, tests and findings derive from
[nanda-resilience 0.1.0](https://github.com/Shereen20112003/SA_NANDATOWN).
Their [MIT copyright and permission notice](../../src/nandatown/experiments/resilience/LICENSE)
is preserved in the package and both distributions. The surrounding Town code
remains Apache-2.0; distribution metadata lists both licenses. No workbench,
model integration, live data, or generated campaign bundles are imported.

### Incorrect-declaration follow-up verification

On November 24, 2026, on Windows / Python 3.12:

- **384 experiment and related native Lab tests passed**, including the new
  declaration, exact completion/cost/retry, replay, CLI, and compatibility cases.
- The fresh 100-run paired comparison completed; all ten retained bundles
  verified. All 50 truthful-baseline runs matched the historical published
  metrics and logical trace fingerprints.
- All three retained historical `resilience.v2` source bundles verified with
  the legacy evaluator.
- The local wheel/sdist build **did not complete**: sandbox policy denied the
  build tool's interpreter-cache write before it could build the package.
  This is not a passing package check. The existing GitHub CI `distribution`
  job builds and checks both artifacts; its outcome must be checked separately
  and may require maintainer approval to run for this external PR.

No live-workbench changes or cloud/model calls were made for this follow-up.

### Original submission verification

The following checks describe the original submission, before the
incorrect-declaration follow-up. On Windows / Python 3.12, against upstream
baseline `b7adc6121868742cf0f00cfebbf2d38bdb349057` plus the original contribution:

- All **104 imported tests passed**; **four new bundling tests passed**,
  including a 15-run campaign with report and retained-bundle verification.
- **305 focused upstream tests passed** across simulation, layers, auction,
  capped influence, consensus, coverage, reputation, owned registry, scenario
  faults, schema synchronization, bundles, and tooling. Three additional bundle
  tests failed identically with clean baseline code: Windows symlink privilege
  (`WinError 1314`) and two historical fixtures whose checkout CRLF bytes differ
  from the recorded LF hashes. Those unrelated tests/files were not changed.
- Wheel and sdist were built with the TLS-verified configured package mirror.
  Archive inspection confirmed 17 existing runtime assets, 60 existing
  fixture/schema/example files, all 13 experiment package files, source tests
  and docs, both licenses, and the console entry point; no generated runs.
  The existing `scripts/check_distributions.py` reports Windows separator false
  positives; equivalent archive-name-normalized checks passed without changing it.
- The extracted wheel passed console/module run and verify, scenario generation,
  native plugin policy swap and verification, and the stock marketplace control
  outside `src`. Validation reused the available dependency environment and did
  not replace its installed Town package; this was not a fresh dependency install.

This local check did not rerun the historical 450-run campaign, launch hosted
CI, or exercise live agents. The original published results remain separately
identified above.
