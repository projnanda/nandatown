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
Disclosure is a seeded, nested mask of truthful declarations, rounded to a
whole provider count. Missing declarations form one conservative unknown group.

## Campaign and report

```console
nandatown-resilience campaign --seeds 0 --disclosures 0 1 --out runs/resilience-smoke
nandatown-resilience campaign --out runs/resilience-450
```

The smoke design has 30 runs. Defaults produce 450 runs: ten paired seeds,
three disclosures (0, 0.5, 1), three conditions, and five policies.
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
`resilience_provider`, `resilience_buyer`), the `resilience.v2` evaluator, and:

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
`resilience.v2` is the corrected evaluator: provider-local outages must not
poison a whole group. Historical bundles require their matching evaluator.

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

Truthful labels and structured failure reasons are assumptions, not verified
infrastructure facts. There is no inferred dependency graph, adversarial label
model, multi-hop dependency model, real network, or production resilience
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

### Local candidate verification

On Windows / Python 3.12, against upstream baseline
`b7adc6121868742cf0f00cfebbf2d38bdb349057` plus these uncommitted changes:

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
