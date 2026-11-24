# Findings: Many Agents, One Point of Failure

<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 nanda-resilience contributors -->

**The original 450-run results below are historical source results.** The
[incorrect-declaration follow-up](#incorrect-declaration-follow-up) is a fresh,
separate 100-run comparison on this upstream contribution. Historical sections are adapted from
[nanda-resilience 0.1.0](https://github.com/Shereen20112003/SA_NANDATOWN),
under its [MIT license](../../src/nandatown/experiments/resilience/LICENSE).
The original experiment targeted Nanda Town compatibility baseline
`b7adc6121868742cf0f00cfebbf2d38bdb349057`. That constant is not a claim about
the executing revision of this bundled extension or future runs.
See the [bundled experiment guide](resilience.md) to reproduce the design.

## Incorrect-declaration follow-up

Executed on November 24, 2026, on Windows / Python 3.12, using bundled experiment
0.2.0 and evaluator `resilience.v3`: **100 runs / 16,000 submitted tasks**.
Ten paired seeds (0-9), full disclosure, five unchanged policies, and two
conditions. This extends PR #318's original candidate; it does not involve the
live workbench or model APIs, and does not rerun the original 450-run campaign.

The new `shared-incorrect` case changes only provider-00's declared dependency
from backend-0 to backend-1. It still executes on backend-0 and experiences the
same shared outage. Provider-09 genuinely uses backend-1, so the incorrect
declaration both hides a shared dependency and falsely groups a healthy provider
with the failing one. All other topology, prices, faults, and workload remain
paired. This is one static incorrect declaration, not a simulated migration,
general adversarial strategy, or automatic freshness check.

Means over ten seeds (160 tasks per run, three attempts at most):

| Condition | Policy | Overall completion | Stress-window completion | Cents/success | Attempts/task |
| --- | --- | ---: | ---: | ---: | ---: |
| Truthful shared | Price | 65.00% | 12.50% | 10.000 | 1.800 |
| One incorrect declaration | Price | 65.00% | 12.50% | 10.000 | 1.800 |
| Truthful shared | Dependency-aware | 100.00% | 100.00% | 13.300 | 1.100 |
| One incorrect declaration | Dependency-aware | 100.00% | 100.00% | 13.450 | 1.300 |
| Truthful shared | Oracle | 100.00% | 100.00% | 13.300 | 1.100 |
| One incorrect declaration | Oracle | 100.00% | 100.00% | 13.300 | 1.100 |

For dependency-aware selection, incorrect minus truthful is **0 percentage
points completion**, **+0.150 cents/success (+1.13%)**, and **+0.200
attempts/task (+18.18%)**. Each run still completes 160 tasks, but uses 208
attempts instead of 176 and pays 2,152 cents rather than 2,128. Repeated retries
on an actual previously tried backend rise from zero to 24 per run. These paired
differences are identical across all ten tested seeds, which reflects the
deterministic structure, not confidence about deployment.

Against price-first in the incorrect case, dependency-aware selection retains
**+35 percentage points overall completion** (+87.5 points in the stress cohort)
at **+3.450 cents per success (+34.5%)**. Random and provider-load controls also
remain exactly unchanged per seed: their mean overall completion is 86.875% and
65.0625%, and mean cost is 11.894 and 10.002 cents/success respectively.

**Interpretation:** incorrect labels waste retries and distort grouping, but the
remaining healthy alternatives and three-attempt budget preserve completion in
this case. It would be misleading to claim that this experiment demonstrated a
completion loss or that inaccurate labels are generally harmless.
Costs are synthetic successful-task payments (total payment / completed tasks),
not real compute or network charges. Failed attempts are unpriced, so the
retry increase is reported separately. The stress cohort is tasks created in
`[12, 28)`, including those completing later.

Reproduce from an installed checkout in PowerShell, with a fresh output directory:

```powershell
nandatown-resilience campaign --conditions shared shared-incorrect --disclosures 1 --seeds 0 1 2 3 4 5 6 7 8 9 --out runs\incorrect-comparison
```

All 100 traces passed the scoped evaluator and all ten retained seed-0 bundles
passed native verification. `summary.json`, `runs.csv`, and `report.html` contain
both conditions; the report's charts retain the truthful shared baseline and its
table includes the incorrect case. Generated bundles are not committed.
See the [guide](resilience.md#one-incorrect-dependency-declaration) for the focused
regression command and individual-run verification.

## What was executed

**450 final experiments, 72,000 submitted tasks**, using the actual Nanda Town
Lab engine. Ten paired seeds (0-9), three failure conditions, three disclosure
levels, and five selection policies. Evaluator: `resilience.v2`.

The campaign recipe, all per-run metrics, logical trace fingerprints, aggregate
results, and paired bootstrap intervals are in
[summary.json](https://github.com/Shereen20112003/SA_NANDATOWN/blob/main/results/published/summary.json).
The visual report is
[report.html](https://github.com/Shereen20112003/SA_NANDATOWN/blob/main/results/published/report.html).
The [per-run CSV](https://github.com/Shereen20112003/SA_NANDATOWN/blob/main/results/published/runs.csv)
and representative bundles remain in that source repository, not this checkout.

## Main result: identities are not independent alternatives

Shared backend outage, full dependency disclosure; means over ten seeds:

| Policy | Stress-window completion | Overall completion | Attempts per submitted task | Cents per successful task |
|---|---:|---:|---:|---:|
| Price | 12.50% | 65.00% | 1.800 | 10.000 |
| Random | 67.19% | 86.88% | 1.526 | 11.894 |
| Provider load | 12.66% | 65.06% | 1.800 | 10.002 |
| Dependency-aware | 100.00% | 100.00% | 1.100 | 13.300 |
| Full-information comparator | 100.00% | 100.00% | 1.100 | 13.300 |

Stress-window completion counts tasks **created in [12, 28)**, even if they
complete after the outage ends. Price selection's last stress-window cohort
can succeed on retry after recovery; that explains its nonzero 12.5%.

The dependency-aware policy's paired mean advantage over price selection is
87.5 percentage points. Its bootstrap interval is [87.5, 87.5] because the
comparison is structurally identical across these seeds. That is **not** certainty
about real deployments.

Provider-level load balancing distributes requests among different identities
but, in this topology and workload, almost entirely within the same cheap shared
backend. Before the stress window, its dependency concentration (HHI) is 1.0,
versus 0.25 for fully informed dependency-aware selection.

## Dependency information is doing measurable work

Shared outage, candidate policy:

| Declared dependencies | Stress-window completion | Overall completion | Attempts/task |
|---|---:|---:|---:|
| 0% | 12.66% | 65.06% | 1.800 |
| 50% | 86.88% | 94.75% | 1.530 |
| 100% | 100.00% | 100.00% | 1.100 |

At zero disclosure, all candidates belong to one conservative unknown group.
The candidate reduces to essentially provider-load balancing; it does not
magically recover hidden infrastructure facts. At full disclosure, it matches
the comparator using complete dependency identities, as expected.

At 50%, which providers disclose matters. The full results retain every seed
rather than only showing a favorable mask.

## Costs and the negative control

All policies completed all tasks in the no-outage condition. With full
disclosure, dependency-aware selection cost **13 cents per success versus 10
cents for price selection: a 30% clean-operation premium**.

Under staggered provider-local outages, full-disclosure stress-window completion
was:

- Dependency-aware: **69.38%**.
- Price: **80.00%**.
- Random: **80.16%**.
- Provider load: **48.59%**.

The candidate's paired difference from price is **-10.63 percentage points**.
The ten-seed 95% bootstrap interval is **[-26.72, +6.09] percentage points**:
the point estimate is worse, but this small experiment does not establish a
reliable disadvantage across broader workloads.

**There is no universal winner here.** Static dependency diversity is a useful
hedge against shared failure, but it is not automatically the right objective
for isolated provider failures or normal-operation cost.

The independent condition matches aggregate provider downtime, not the
instantaneous number of unavailable providers or exposure of every task. These
are controlled but different workloads, not interchangeable outage samples.

## What did not work during development

The first exploratory policy treated every failure as a group-level failure.
That incorrectly propagated a provider-local outage to its healthy peers.
The implementation now distinguishes:

- Backend outage or capacity rejection: avoid that dependency group for the
  remainder of this task.
- Provider-local outage: exclude only the failed provider.

A regression test covers the distinction. The complete campaign was rerun under
`resilience.v2`; **the published tables are from the corrected implementation**.
The remaining independent-outage result is retained, not hidden by that fix.

This correction assumes the synthetic service's structured failure reasons are
truthful. It does not solve attribution for generic timeouts, deceptive peers,
or inaccessible infrastructure.

## Historical source verification

These are the source project's reported checks, not claims that hosted CI or
450 fresh runs have been executed for the bundled upstream candidate.

- **104 extension tests** passed locally and in every hosted CI matrix job.
- **143 relevant upstream Lab regression tests** passed.
- Native `nandatown run` with the plugin and a coordination-layer swap passed.
- All final runs passed the scoped trace evaluator.
- All 45 retained final campaign bundles were replay-verified at generation.
- Three representative bundles are included in the shareable repository.
- Wheel execution and bundle verification passed outside the source checkout.
- The HTML report was checked in a browser at desktop and mobile widths.

Local execution used Windows and Python 3.12.
[Hosted CI](https://github.com/Shereen20112003/SA_NANDATOWN/actions/runs/41619744094)
also passed on Windows/Linux with Python 3.11/3.12: each of the four jobs ran all
104 tests and a 30-run smoke campaign from a clean installation.

The first hosted run caught an installation-format issue: pip cannot hash-check
a Git dependency alongside registry package hashes. The corrected setup keeps
registry dependencies hash-locked and installs the immutable Git revision through
the project manifest in the second step. The successful matrix used that setup.

Passing evidence means the observed simulation is internally consistent with
its declared profile. It is **not** proof of real-world dependency truth,
production security, or universal resilience.

## What would make the next contribution more research-worthy?

1. Compare the consistent shared reservation view with delayed, organization-local
   views; measure the information and coordination overhead required.
2. Estimate when failures are correlated before paying the diversity premium.
   Keep price, random, and load controls rather than comparing only to the
   weakest baseline.
3. Test multiple concentration levels, price gaps, capacities, and outage mixtures.
   The current topology intentionally exposes a failure, not a representative
   sample of every possible agent economy.
4. Replace truthful labels with accountable, freshness-bounded dependency
   attestations or privacy-preserving shared-risk identifiers.
5. Port the scenario to Track with real shared services and process failures.

This is a concrete Nanda Town contribution candidate: a scenario, plugins,
versioned evaluator, tests, and evidence. Shared-risk routing itself is
established prior art; novelty and upstream suitability need maintainer and
research-community feedback.
