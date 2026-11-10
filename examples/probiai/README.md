# ProbiAI: an insured ledger for Town buyers

[ProbiAI](https://probiai-personalagent.vercel.app) is insurance for AI agents. An agent's own runtime sends
ProbiAI a copy of each action. ProbiAI keeps a signed, facts-only record (kind, amount, place, result; never
message content), and it opens a claim by itself when it sees a covered mistake, such as paying twice for one
order. A person reviews every claim.

This folder connects ProbiAI to Town as shared payments infrastructure. ProbiAI runs elsewhere, as a hosted
service; nothing of its own code is here. Town's ledger is the answer key for ProbiAI's verdict.

## Integration

| Piece | What it does |
| --- | --- |
| `probiai-insured.v1` (payments plugin) | Town's `ledger.v1`, unchanged, plus a copy of each payment an insured participant settles. The copy goes to ProbiAI's `/v1/copies`, the same endpoint its agent add-ons use, with that participant's ProbiAI agent key. ProbiAI's reply (the record it kept, and any claim the payment opened) is recorded as Town events: `insurer_copy_kept`, `insurer_claim_opened`, `insurer_unreachable`. |
| `careless_buyer` (role, test fixture) | Takes a duplicated delivery message as a new bill and pays the seller again directly. Town's escrow refuses a second release of the same order, so this is the realistic path to a double payment. |
| `probiai-insured` (validator) | `insurer_received`: did every insured payment reach ProbiAI? `double_payment_verdict`: does ProbiAI's claim, or its absence, match what Town's ledger shows for each order? Town adds its own `ledger_conserved` stage. |
| Three scenarios | `insured-careless` (the mistake), `insured-clean` (control: the reference buyer recognizes the duplicate), `insured-repeat` (boundary: two separate orders from one seller at one price). All use the marketplace setup with one duplicated delivery. |

The plugin fails open: if ProbiAI can't be reached, Town's run is unchanged and the miss is recorded. Each copy
carries a wall-clock time, because ProbiAI's double-payment window is 30 minutes of real time; Town's logical
trace and its other stages are unaffected. Sellers are named `<seller>.<run id>.town` in copies, because Town
identities belong to one run.

## Reproduce

Town commit: `6f9a7c0` (main, 4 Nov 2026). ProbiAI: the hosted service as deployed on 8 Nov 2026 (version
0.1.0, per `/.well-known/agent-card.json`), the first version whose `/v1/copies` reply lists the claims a copy
opened.

1. Get a ProbiAI agent key. Sign up at https://probiai-personalagent.vercel.app/connect (any email; test mode,
   no payment), choose "OpenAI Agents SDK (an agent you built)", and copy the agent key (`pk.ag_...`) from your
   page. Keep it out of the repository.
2. Run:

```bash
python3 -m venv .venv && source .venv/bin/activate && python -m pip install -e '.[dev]'
export NANDATOWN_HOME=$(mktemp -d)
export PROBIAI_KEY=pk.ag_...           # insures buyer-1; PROBIAI_KEYS='{"name": "key"}' for several
nandatown run examples/probiai/insured-careless.yaml --out /tmp/probiai-town
nandatown run examples/probiai/insured-clean.yaml --out /tmp/probiai-town
nandatown run examples/probiai/insured-repeat.yaml --out /tmp/probiai-town
PROBIAI_URL=http://127.0.0.1:9 nandatown run examples/probiai/insured-careless.yaml --out /tmp/probiai-town
python examples/probiai/verify.py /tmp/probiai-town/sim-*        # or the recorded runs in evidence/
python -m pytest -q tests/test_probiai_insured_ledger.py          # offline: a local stand-in for ProbiAI
PROBIAI_LIVE_KEY=$PROBIAI_KEY python -m pytest -q tests/test_probiai_insured_ledger.py   # adds the live check
```

`nandatown verify` alone reports an evaluator replay mismatch for these bundles: it replays the evaluator without
loading a scenario's `plugin_files`, so the `probiai-insured` validator is missing. `verify.py` loads the plugin
first, then runs Town's own `verify_bundle`.

## Results

Recorded against the hosted ProbiAI on 8 Nov 2026; the bundles are in `evidence/` and all four verify.

| Scenario | Expected | Town's ledger | ProbiAI | `double_payment_verdict` |
| --- | --- | --- | --- | --- |
| `insured-careless` (run `sim-64e309e2e79d`) | A claim for the paid-twice order | `order-buyer-1-1` paid twice, 3,590 cents each (ev-37, ev-43) | Kept both copies; opened claim `double_charge` for $35.90 (ev-45) | passed |
| `insured-clean` (`sim-83e5a418e6a9`) | No claim | `order-buyer-1-1` paid once (ev-37); duplicate recognized (ev-43) | Kept the copy; no claim | passed |
| `insured-repeat` (`sim-a455e3eaab54`) | No claim: two orders, each paid once | `order-buyer-1-1` and `order-buyer-1-2`, 3,590 cents each (ev-37, ev-62) | Opened claim `double_charge` on the second order (ev-64) | **failed: false alarm** |
| `insured-careless`, ProbiAI unreachable (`sim-9a66d9c60c06`) | Town unchanged; the verdict untested | Paid twice (ev-37, ev-43); `ledger_conserved` passed | Both copies failed (ev-38, ev-44) | not enough evidence |

What this shows about compatibility: ProbiAI's add-on copy format works as a Town payments layer, the hosted
service keeps every insured payment and replies within the run, and its claim is checkable against Town's
ledger from the bundle alone. It also shows `ledger_conserved` passing while a buyer pays twice: conserved money
is not correct money.

## Limitations and boundary

- **The boundary found:** ProbiAI's published rule (same amount, same place, within 30 minutes) can't tell two
  orders at one price from one order paid twice: each copy names its order, but ProbiAI keeps only facts (kind,
  amount, place, result), so the rule never sees the order (`insured-repeat`). The tests pin this as a known false alarm.
- **Fixtures, not adopted agents:** the buyers and sellers are Town's scripted reference participants, and
  `careless_buyer` is our fixture. They are not independent agents, and this is not evidence that any real agent
  adopted ProbiAI. The real system under test is the hosted ProbiAI service.
- **Untested:** the Track and model-driven participants; several insured participants; payments outside Town's
  ledger; ProbiAI's claim review and payout; latency budgets (Town settles each payment first; the run then waits one HTTPS round
  trip for ProbiAI's reply, with a 10-second timeout).
- `nandatown verify` does not load scenario plugin files (see Reproduce).
