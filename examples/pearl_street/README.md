# Pearl Street in the Town Lab: a metered peak-reduction market

[Pearl Street](https://github.com/cafetters/pearl-street-public) is a
flexibility exchange for energy agents. Agents register with it, the grid
operator posts a peak-reduction need, agents offer kW, the exchange clears
at one uniform price, settles on metered delivery, and keeps a delivery
record. This example runs that whole loop as a Town Lab scenario, with
Pearl Street's real exchange code in the loop and Town's shared layers
around it.

## How it connects

```
utility ──post_need──▶ ┌──────────────────────────────┐ ──award/settle──▶ utility ──pay, rate──▶ ledger, trust
meter ──asset_limits,─▶│ exchange (Town agent)        │
         telemetry     │   hosts exchange.core        │ ──post_need────▶ providers
providers ─register,──▶│   Exchange.handle(json)      │ ──reject───────▶ sender
           offer       └──────────────────────────────┘
        all of it over Town messaging, with Town auth, registry, data_facts, payments, trust
```

- **Pearl Street side.** `exchange.core.Exchange` comes from the public snapshot
  repo `cafetters/pearl-street-public`, pinned to commit
  `f839d1e2e8482152109c53f2f5234c9a40b3da27` (`requirements.txt`). That repo is a
  snapshot of a private development repo at
  `f7128ab5f2984874b684b1cb0ab881b8c29c152b`. The exchange package is standard
  library only, which is why it installs with `--no-deps`.
- **Town side.** `pearl_street_town.py` (loaded via `plugin_files:`) registers five
  roles: `pearl_exchange` (the bridge that hosts the exchange), `grid_operator`,
  `meter`, `flex_provider`, and `listing_forger` (used only by the
  forged-listing test). The bridge does not reimplement the market. Each
  Town message becomes a Pearl Street envelope whose `sender` is the Town
  sender that Town's auth layer verified and whose `t_utc` is the arrival time,
  so an agent controls neither. Everything the exchange emits goes back out as
  Town messages.
- **Shared layers exercised.**
  - registry: the exchange and the meter publish cards; providers and the utility find them by capability.
  - auth: message signatures and the sender binding above.
  - data_facts: the meter attests every asset's limits and every 15-minute reading.
  - payments: credits (utility to provider) and shortfall charges (provider to utility) move through Town's ledger in cents.
  - trust: the utility rates each provider good or bad from its Pearl Street delivery record.
- **Data.** Asset limits are five real sites from the Engie 80-prosumer
  synthetic dataset (one data center, four PV + battery sites), taken from the
  dataset's 63 KB metadata file. `engie_slice.json` records the source file's
  sha256 and the rule; `make_engie_slice.py` regenerates it.
- **Validator.** `pearl_street_validator.py` does not import Pearl Street. It
  recomputes clearing and settlement from the scenario recipe and the meter's
  attestations, and every check fails if a configured participant is missing,
  so a dropped message cannot turn into a pass.

## The workflow

A utility needs 30,000 kW of reduction from 17:00 to 20:00 Central on
2023-07-25, price cap $1,000/MWh, gate at 16:00.

1. The meter attests each site's limits to the exchange. The providers find the
   exchange in Town's index, register and subscribe.
2. The utility posts the need. The exchange broadcasts it.
3. Offers: `pvb-48` 9,000 kW at $90/MWh, `pvb-35` 18,000 at $100, `dc-08`
   15,000 at $120. `pvb-12` bids 10,000 kW against its own declared 7,170 and
   `pvb-36` bids five minutes after the gate.
4. At the gate the exchange clears.
5. The meter reads delivery every 15 minutes. Each asset follows its award up
   to its attested power.
6. At the window's end the exchange settles. The utility pays credits, the
   short provider pays its charge, and the utility rates each provider.

**Boundary case: an overpromiser.** The rule is that a provider's bids are
capped at its registered limits. `pvb-35`'s metered, attested power is 8,702 kW,
but it declares a mandate of 26,000 kW. Pearl Street checks offers against
the declared mandate only and never compares it with the attested power. So
`pvb-35` wins 18,000 kW it cannot deliver. It also tries to report its own
meter reading of 18,000 kW.

## Reproduce

Python 3.12 or newer (the exchange package requires it).

```bash
python -c "import sys; assert sys.version_info >= (3, 12), sys.version"
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
pip install --no-deps -r examples/pearl_street/requirements.txt
python -m pytest -q examples/pearl_street/test_pearl_street.py
```

Expected: `15 passed`. To see the stage table and write a bundle:

```bash
nandatown run examples/pearl_street/pearl_street_peak.yaml --out runs
```

The other scenarios run the same way:

```bash
nandatown run examples/pearl_street/pearl_street_peak_drop.yaml --out runs   # also _duplicate, _delay, _late
nandatown run examples/pearl_street/pearl_street_repeat.yaml --out runs      # also pearl_street_repeat_policy.yaml
nandatown compare examples/pearl_street/pearl_street_peak_forged_listing.yaml --swap registry=index.owned.v1
python examples/pearl_street/fault_sweep.py                                  # rewrites fault_sweep_results.md
```

Generated scenarios come from `make_fault_variants.py` and `make_repeat_scenarios.py`;
a test checks they match the current base.

The run's verdict is **FAILED on purpose**: `awards_within_attested_limits`
fails. The other eight scenario stages and Town's standing `ledger_conserved`
check pass. Town's standing `privacy` check is **not tested**, because this
scenario declares no redacted fields.

- Town commit: `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`, nandatown 0.2.0,
  Lab evaluator `lab-0.2.6`.
- Pearl Street: `flex-exchange` 0.1.0, `cafetters/pearl-street-public@f839d1e`.
- Tested on Windows 11, Python 3.12.10.
- Full upstream suite on Windows 11 (`python -m pytest -q`): 1,803 passed, 26
  skipped, 36 failed. The same 36 fail on clean upstream main `6f9a7c0`. They
  are Windows-only:
  - 14 need the privilege to create symlinks.
  - 7 report fixture hash mismatches after a CRLF checkout.
  - 2 expect 0600 file modes.
  - 1 uses a filename Windows rejects.
  - 12 more involve other Windows file-system behaviour (by their test names:
    read-only homes, hard links, receipt checks over the same fixtures).

  This example adds nothing to the default test path.

**Verifying the evidence.** `evidence/sim-667774539952/` is the bundle from
the run above: the five records, `manifest.json`, `attestation.json` and
`report.md`. Its `profile.json` names the plugin files by absolute path
(`T:\...`, a temporary drive letter used so no local path was recorded). In
a fresh process, `nandatown verify` reports
`evaluator replay mismatch`, because `verify` does not load a scenario's
`plugin_files`, so the `pearl_street_peak` validator is not registered. The
tests verify the fresh run's bundle and this committed bundle in the same
process after loading the plugin files, and another test pins the gap.

## Results

Expected values were worked by hand (`expected.json`) before the first run.
After that run, two non-numeric fields were corrected: Town's standing check
is named `privacy` and reports `not_tested` (I had written `privacy_clean:
passed`), and the overall `verdict` was added. No numeric value changed.
The `honest_providers_unharmed` stage was added later, with the fault
variants below; it passes in the fault-free run.

| | Expected | Observed |
|---|---|---|
| Rejected | pvb-12 `outside_mandate`, pvb-36 `after_gate_closure`, pvb-35's self-report `unauthorized_sender` | same |
| Awards | pvb-48 9,000, pvb-35 18,000, dc-08 3,000 kW | same |
| Uniform price | $120/MWh (floor of dc-08, the last accepted) | $120/MWh |
| Delivered | 9,000 / 8,702 / 3,000 kW | same |
| Credits | $3,240.00 / $3,132.72 / $1,080.00 | same |
| Shortfall charge | pvb-35 $27,894.00 (9,298 kW x 3 h x $1,000/MWh) | same |
| Ledger | utility 12,044,128 cents, pvb-35 2,523,872, money conserved | same |
| Delivery record, Town reputation | pvb-48 1.0 / +1, dc-08 1.0 / +1, pvb-35 0.483 / -1 | same |
| `awards_within_attested_limits` | failed, pvb-35 18,000 kW against attested 8,702 | failed, same note |

**What the boundary case shows.** Pearl Street enforced the limit the
provider declared: `pvb-12` was refused. It also kept the meter as the only
source of delivery: `pvb-35`'s self-report was refused because the bridge
binds the sender to Town's verified identity. But it accepted a declared
mandate three times the attested power. The overpromise was only priced
after the fact: a $27,894 charge, delivery record 0.48 and a bad rating. The
grid was still 9,298 kW short at peak (20,702 of 30,000 kW delivered), while
`dc-08` had offered 12,000 kW more that went unused. The smallest fix on the
Pearl Street side is to refuse a `register` whose `max_kw` exceeds the attested
`power_kw`; this example leaves the code unchanged and reports the gap.

## Fault injection

Three variants add one Town transport fault each to the same roster, slice
and seed (`pearl_street_peak_{drop,duplicate,delay,late}.yaml`, generated by
`make_fault_variants.py`). Each is a pinned observation in the test file.

| Fault | Question | Observed | Honest agent harmed? |
|---|---|---|---|
| `drop` telemetry #1: pvb-48's reading at 22:00 UTC (17:00 CT), the window's first interval | Is a lost reading treated as under-delivery? | Yes. Pearl Street counts a missing reading as zero. pvb-48 delivered 9,000 kW every step (the meter attested all 12), but it is settled at 8,250 kW, charged $2,250 and rated bad. | **Yes: finding** |
| `duplicate` offer #1: dc-08's offer | Counted twice or deduped? | Counted once. The exchange does not dedupe by `msg_id`: the second copy is refused as `outside_mandate` (15,000 + 15,000 > 19,020). Probed directly, a copy that fits the mandate is accepted silently and overwrites the first under the same id, so dedupe is a side effect, not a check. Clearing and settlement match the fault-free run. | No |
| `delay` offer #1 by 45 min: dc-08's offer | Sent 20:20, arrives 21:05, gate 21:00: refused? Whose time decides? | Refused as `after_gate_closure`. Arrival time decides: the bridge stamps arrival so no agent can backdate, and Pearl Street's gate check reads that stamp. dc-08 loses the award (not charged or rated). The need is filled to 27,000 of 30,000 kW. With dc-08 gone, the overpromiser pvb-35 sets the clearing price ($100/MWh, its floor) and keeps its full 18,000 kW award. Gating on arrival time matches how real markets use receipt time, so this is a design choice, not a defect. | No (lost the sale, not penalized) |

**Drop-rate sweep.** `fault_sweep.py` runs Town's `drop_rate` on all
messages at 1%, 5% and 10%, 20 seeds each, and writes
`fault_sweep_results.md` (a test checks it is current):

| Drop rate | Honest provider charged or rated down | Overpromiser still caught | Ledger conserved |
|---|---|---|---|
| 1% | 5/20 (25%) | 17/20 (85%) | 20/20 (100%) |
| 5% | 12/20 (60%) | 14/20 (70%) | 20/20 (100%) |
| 10% | 11/20 (55%) | 11/20 (55%) | 20/20 (100%) |

With 20 seeds per rate, each share has a standard error of roughly ±11
percentage points (at 50%; a 95% interval is about twice that). So the rise
in honest harm from 1% to 5% (25% to 60%) is meaningful, while the
difference between 5% and 10% is within noise.

What the sweep shows:
- Harm to honest providers comes from missing data treated as non-delivery:
  - a lost `telemetry` reading, at $750 to $2,250 per reading;
  - a lost `award` or `meter_request`, which means no readings at all and the
    full award charged at the price cap (up to $45,000).
  - The second path runs through this example's own roles (the meter reads
    only what the operator forwards), but the charge itself is Pearl
    Street's rule.
- The overpromiser was caught in every run where it won an award. The runs
  where it was not caught are runs where a drop kept it from winning.
- The ledger was conserved in every run. Transfers are local ledger calls,
  not messages, so this shows money was never created or lost. It does not
  show that payments were complete. A lost `settle` message left a credit or
  charge unpaid, and failed `ledger_mirrors_settlement`, in 7 of the 8 sweep
  runs that lost one.

## Split drop-rate sweep and a late reading

The same sweep, with `drop_rate` limited to one message set at a time (`fault_sweep_results.md`, checked by a test):
- **telemetry only** isolates Pearl Street's settlement rule: a missing reading counts as zero;
- **award and meter_request only** isolates this example's metering chain: the operator forwards each award, and the meter reads only what it is told.

| Messages dropped | Drop rate | Honest provider charged or rated down | Overpromiser still caught | Ledger conserved |
|---|---|---|---|---|
| telemetry only | 1% | 3/20 (15%) | 20/20 (100%) | 20/20 (100%) |
| telemetry only | 5% | 13/20 (65%) | 20/20 (100%) | 20/20 (100%) |
| telemetry only | 10% | 17/20 (85%) | 20/20 (100%) | 20/20 (100%) |
| award and meter_request only | 1% | 0/20 (0%) | 20/20 (100%) | 20/20 (100%) |
| award and meter_request only | 5% | 4/20 (20%) | 20/20 (100%) | 20/20 (100%) |
| award and meter_request only | 10% | 8/20 (40%) | 20/20 (100%) | 20/20 (100%) |

Lost readings alone harm honest providers at least as often as dropping every message kind (25%, 60% and 55% above). So the main driver is Pearl Street's settlement rule, not this example's metering chain. The ±11-point noise caveat applies to each cell.

| Fault | Question | Observed | Honest agent harmed? |
|---|---|---|---|
| `delay` +185 min: pvb-48's 22:00 UTC (17:00 CT) reading | Does a reading that arrives after the window ends count? | No. It reaches the exchange at 01:05 UTC, after settlement at 01:00. It is accepted but filed under its arrival time, and nothing reopens the settlement. pvb-48 is settled exactly as in the drop case: 8,250 kW, $2,250 charge, rated bad. Late is the same as lost. | **Y (finding)** |

## Repeat peak event

Two back-to-back evenings (Jul 25 and 26, 17:00 to 20:00 CT), with the same roster and the same offers each day. The validator checks clearing, settlement and coverage per event, and the delivery record cumulatively.

| Arm | Overpromiser wins again on day two? | Day-two peak shortfall | Notes |
|---|---|---|---|
| (a) No feedback | **Yes**, 18,000 kW again | **9,298 kW** (20,702 of 30,000 delivered) | Day two repeats day one. pvb-35's day-two $27,894 charge **bounces**: day one's charge left it 2,523,872 cents. `ledger_mirrors_settlement` fails, and pvb-35 ends day two with more money than after day one. |
| (b) Pearl Street's delivery-record limits | Not run | n/a | Not config-only. The exchange's `delivery_record` option records binding-nomination delivery and, by its own documentation, never charges or limits awards. No exchange setting turns the record into an award limit. |
| (c) Utility skips providers with Town score < 0 | **No**: its day-two offer is refused (`town_policy_excluded`) | **6,000 kW** (24,000 of 30,000 delivered) | Enforced at the exchange's Town gateway, not by Pearl Street. dc-08 fills 15,000 kW instead, at the same $120/MWh. The utility pays $8,640 in day-two credits, with no penalty income. |

This is the system-level form of the overpromiser finding. Pearl Street prices an overpromise after the fact, but nothing in the exchange stops a repeat. The penalty can become uncollectible. Wiring Town reputation into admission is what changes who wins.

## Forged registry listing

A rival republishes the exchange's Town card under the exchange's name with its own key, at 20:00:30 UTC, before anyone looks it up. Run as `nandatown compare ... --swap registry=index.owned.v1`.

| Registry | Can providers find the market? | Observed |
|---|---|---|
| `index.v1` (Town default) | **No** | The unverified card replaces the verified listing, and lookups hide it. The meter, the utility and all 5 providers record `discovery_failed`. No need is posted and no market runs. One forged card is a denial of service for every participant. |
| `index.owned.v1` (swap) | **Yes** | The forged card is refused (`card_publish_refused`), and the run matches the fault-free one stage for stage. |

This matches Town's own `registry_eviction` control, here applied to market infrastructure rather than a seller.

## Not tested

- The exchange's other mechanisms: revised offers, price mode, binding
  nominations and imbalance, and the D65 delivery-record ledger.
- Network transport, real processes, timeouts and retries. These are Lab
  runs, seeded and in logical time. Faults are Town's transport faults
  (drop, duplicate, delay, drop_rate), not crashes or partitions.
- More than two consecutive events, providers that adapt their offers, and
  Town's capped trust plugin (only meaningful now that reputation feeds admission).
- Any agent that is not a scripted Town role, and LLM-driven agents.
- A deployed NANDA Index, and real meter data. Physics here is
  `min(award, attested power)`.
- Independent adoption. The adapter, the exchange and this test share one
  author; per CONTRIBUTING this is not evidence that a separately developed
  agent is compatible.
