# loom-portal

Mirror of [jasonpipi/loom-portal](https://github.com/jasonpipi/loom-portal) (v0.1.1, used by HW8) with the HW9 changes (v0.2.1). Install from here with
`pip install "git+https://github.com/JiayiLi21/nandatown@hw9-loom-usability#subdirectory=examples/loom/portal"`.

The LOOM access portal: task-scoped, expiring access to a person's health
context for agents. LOOM decides which tier of the record an agent may
receive (L0 raw signal, L1 readings, L2 patterns, L3 constraints), for how
long, and with how much friction. Standard library only.

```bash
pip install "git+https://github.com/JiayiLi21/nandatown@hw9-loom-usability#subdirectory=examples/loom/portal"
loom-portal --port 8787 --seed-credit cambridge-clinic=85
```

**Agents: start with [`../AGENT_INTERFACE.md`](../AGENT_INTERFACE.md)**, the only document
an agent needs. HW9 cold-start tests with fresh agents: [`../hw9/RESULTS.md`](../hw9/RESULTS.md).

## Rules (`loom-policy-0.2.1`)

| Task | Deepest tier | Fields | Validity | Category |
|---|---|---|---|---|
| shopping_list | L3 | constraints | once | agent_for_user |
| meal_plan | L2 | glucose_patterns, constraints | 7 days | agent_for_user |
| clinical_visit | L2 | glucose_patterns, alerts, constraints | 14 days | care_provider |
| anything else, from an `agent_for_user` agent | as the person approves | | as approved | proposal for the person (0.2.0) |
| anything else, other categories | | | | denied by default, no penalty |

- Overreach (deeper tier or extra field): blocked, publisher −20.
- Fetch after expiry: denied, −5. Clean grant: +1. Baseline credit 60.
- Credit sets friction only. At 80 or above: auto-renew, confirm only beyond the rules. From 50 to 79: confirm on first request. Below 50: confirm every time, one-time snapshot.
- Same `request_id` with the same content (receive time excluded) is a replay: same decision, no credit change. A replay carries data only while the grant is still valid at the replay's time; after expiry it returns `data_withheld: "expired"`. Different content is a 409 conflict.

## API

`GET /health`, `GET /v1/policy`, `POST /v1/<ns>/requests`, `POST /v1/<ns>/fetch`,
`GET /v1/<ns>/credit`, `GET /v1/<ns>/proposals/<id>`; with the owner token:
`GET /v1/<ns>/log` (decisions, never data), `GET /v1/<ns>/owner/proposals`,
`POST /v1/<ns>/owner/decide`. `--access-log FILE` records every exchange. A namespace is one isolated
state, created on first use.

The record is synthetic. Used by the Nanda Town integration in
`examples/loom/` (MIT AI Studio Fall 2026, HW8).

```bash
pip install -e '.[dev]' && python -m pytest -q
```
