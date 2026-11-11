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
- **Town side.** `pearl_street_town.py` (loaded via `plugin_files:`) registers four
  roles: `pearl_exchange` (the bridge that hosts the exchange), `grid_operator`,
  `meter` and `flex_provider`. The bridge does not reimplement the market. Each
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

Expected: `3 passed`. To see the stage table and write a bundle:

```bash
nandatown run examples/pearl_street/pearl_street_peak.yaml --out runs
```

The run's verdict is **FAILED on purpose**: `awards_within_attested_limits`
fails and all other stages pass.

- Town commit: `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`, nandatown 0.2.0,
  Lab evaluator `lab-0.2.6`.
- Pearl Street: `flex-exchange` 0.1.0, `cafetters/pearl-street-public@f839d1e`.
- Tested on Windows 11, Python 3.12.10.

**Verifying the evidence.** `evidence/sim-637f85c72035/` is the bundle from
the run above: the five records, `manifest.json`, `attestation.json` and
`report.md`. Its `profile.json` names the plugin files by absolute path
(`T:\...`, a temporary drive letter used so no local path was recorded). In
a fresh process, `nandatown verify` reports
`evaluator replay mismatch`, because `verify` does not load a scenario's
`plugin_files`, so the `pearl_street_peak` validator is not registered. The
test verifies the bundle in the same process after the plugin files are loaded,
and a third test pins the gap.

## Results

Expected values were worked by hand (`expected.json`) before the first run.

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

## Not tested

- The exchange's other mechanisms: revised offers, price mode, binding
  nominations and imbalance, and the D65 delivery-record ledger.
- Network transport, real processes, timeouts and retries. This is a Lab run,
  seeded and in logical time, with no faults injected.
- Any agent that is not a scripted Town role, and LLM-driven agents.
- A deployed NANDA Index, and real meter data. Physics here is
  `min(award, attested power)`.
- Independent adoption. The adapter, the exchange and this test share one
  author; per CONTRIBUTING this is not evidence that a separately developed
  agent is compatible.
