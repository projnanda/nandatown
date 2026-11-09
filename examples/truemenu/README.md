# TrueMenu on the Track

[TrueMenu](https://github.com/IntelCodes/truemenu) gives ordering agents
merchant-sourced menu items, prices and open-now status. Every source
carries a freshness label, and each quotable item comes with a signed
quote that any party can check through a public endpoint. TrueMenu runs
outside Town: this directory holds only the adapters that let it take
part in a Track run, the check that exercises it, and adapter unit tests.

```
lunch_buyer.py ── quote_request {menu_request} ──▶ Town coordinator ──▶ truemenu_seller.py
      ▲                                              (leases, fences,        │
      │                                               redelivery, faults)    │ POST /v1/find_items
      │ POST /v1/verify_quote (public)                                       ▼   (X-Agent-Key)
      └──────────────────────────────────── TrueMenu API ◀── simulated POS feed per restaurant
```

- **`truemenu_seller.py`** plays the seller. It turns a `menu_request`
  (area, budget, dislikes, diet, wanted food, meal time) into a TrueMenu
  `find_items` call and answers with up to five offers, each carrying
  TrueMenu's own signed `quote_id`, or with a decline that gives
  TrueMenu's reasons. It never prices anything itself. It journals each
  reply, because TrueMenu signs a new quote on every call.
- **`lunch_buyer.py`** plays the buyer. It finds the seller through the
  town, asks for a low-carb lunch near Kendall Square under $20 with no
  pork or avocado, picks a main dish, and verifies that quote directly
  with TrueMenu, outside the seller's channel: signature, issuer,
  expiry, unchanged price, same item, same price as offered, within
  budget, and source not stale. Its verdict is the `correct` assertion
  Town records.

## Reproduce

TrueMenu first (Python 3.11+, about five minutes; full notes in its
README):

```bash
git clone https://github.com/IntelCodes/truemenu ~/truemenu
cd ~/truemenu && ./setup.sh && ./tests/smoke.sh
```

`setup.sh` writes `~/truemenu/.env` with a quote secret and one agent key.
Give Town's seller its own read-only key, then restart TrueMenu:

```bash
cd ~/truemenu
KEY=tm_$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
sed -i "s/^TRUEMENU_AGENT_KEYS=.*/&,nanda-town-seller:$KEY:read/" .env
echo "TRUEMENU_TOWN_KEY=$KEY" >> .env
scripts/start.sh
```

Then Town, from a checkout of this branch:

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q tests/test_truemenu_example.py

export TRUEMENU_URL=http://127.0.0.1:8088
export TRUEMENU_AGENT_KEY=$(grep '^TRUEMENU_TOWN_KEY=' ~/truemenu/.env | cut -d= -f2)
TRUEMENU_HOME=~/truemenu examples/truemenu/check.sh runs/truemenu
```

One case by hand:

```bash
nandatown run quote-clean \
  --agent seller=cmd:"python examples/truemenu/truemenu_seller.py" \
  --agent buyer=cmd:"python examples/truemenu/lunch_buyer.py"
```

`check.sh` takes about two minutes, writes one evidence bundle per case
and `runs/truemenu/summary.tsv`, and exits 0 only when every case
matches its expected verdict. `nandatown verify <bundle>` replays any of
them.

## What the check runs

| Case | Profile | Expected | What it shows |
|---|---|---|---|
| clean_quote | quote-clean | PASSED | the whole workflow: request, TrueMenu offers, buyer picks and independently verifies a signed quote |
| duplicate_delivery | quote-duplicate-delivery | PASSED | the seller recognizes redelivered work and resends its first reply instead of getting a second quote |
| lost_ack | quote-lost-ack | PASSED | a lost acknowledgement is retried without a second application |
| dropped_wakeup | quote-drop-wakeup | PASSED | the seller still finds the work in the durable inbox |
| naive_adapter_dup | quote-duplicate-delivery | FAILED or INCOMPLETE | negative control: without the journal the seller calls TrueMenu again and gets a newly signed quote; the town refuses that changed reply under the same identity, and records either a second application or a duplicate never acknowledged, depending on timing |
| reference_buyer | quote-clean | INCOMPLETE | boundary: Town's reference buyer asks for two widgets; TrueMenu declines a request it cannot quote, and the reference buyer cannot read the decline |
| stale_sources_decline | quote-clean | PASSED | failure case: every restaurant feed paused past its stale cut; TrueMenu refuses to quote and the buyer, expecting a decline, accepts it |
| stale_sources_expect | quote-clean | FAILED | control for the case above: a buyer that insists on a quote records an incorrect result, so the check can fail |

## Reading the reports honestly

- The report header names Town's built-in task (`quote 2 x widget at
  1995 cents`). These participants ignore that task: the request they
  exchange is the `menu_request` above, and `correct` is the lunch
  buyer's own judgment, recorded in its acknowledgement note.
- Both participants are written for this check. Town's own fixtures are
  not involved, but neither is an independently built third-party agent.
- The buyer's pick is deterministic. TrueMenu's lunch agent normally
  picks with a language model; that step is not exercised here.
- The seller's crash faults (`quote-crash-restart`, the amnesia
  profiles) are acted out by Town's reference seller itself, so they are
  not tested for this adapter. Neither is `--identity` (portable
  identity) or the A2A Path.
- TrueMenu's data is three Kendall Square restaurants with a simulated
  point-of-sale feed, not a live one.
