# LOOM portal: interface for agents

This page is for an agent (or the developer of one) that needs part of a
person's health context to do a task for them. It is the only document an
agent needs; it does not have to read LOOM's source.

## What LOOM offers

LOOM holds one person's health record and hands out **task-scoped,
expiring slices** of it. You say what task you are doing; LOOM decides
how deep you may see, which fields you get, and for how long.

| Tier | What it contains | Field names |
|---|---|---|
| L0 | raw sensor signal | `raw_signal` |
| L1 | readings (here: hourly glucose, mg/dL) | `readings` |
| L2 | patterns and alerts derived from readings | `glucose_patterns`, `alerts` |
| L3 | plain-language constraints | `constraints` |

L0 is the deepest (most revealing), L3 the shallowest. A tier includes the
shallower ones: an L2 grant may carry `constraints` too.

## Access

- Base URL and namespace are given to you by whoever runs the portal (for
  a test: `http://127.0.0.1:8787` and a namespace such as `demo`).
  Every call for one person goes to `/v1/<namespace>/...`.
- Agents have no login in v0.2. You identify yourself with `agent_id` (this
  agent instance) and `publisher` (the company or developer that ships
  it). In Nanda Town the gateway fills `agent_id` from the signed envelope;
  over plain HTTP both are claims.
- JSON in, JSON out. Errors are JSON `{"error": "..."}`; a malformed body
  gets HTTP 400 naming the missing fields.

## Endpoints

| Method and path | Use |
|---|---|
| `GET /health` | version and policy id |
| `POST /v1/<ns>/requests` | ask for access for a task; a grant carries data immediately |
| `POST /v1/<ns>/fetch` | get fresh data again under a grant you already hold |
| `GET /v1/<ns>/credit` | publisher → credit score (only publishers that have been seen) |
| `GET /v1/<ns>/log` | the person's audit log: every decision, without the data (owner token only; agents get 403) |
| `GET /v1/policy` | the built-in tasks, as JSON (same as the table below) |
| `GET /v1/<ns>/proposals/<proposal_id>` | status of a proposal for a task that is not built in (see below) |

### `POST /v1/<ns>/requests`: body

| Field | Required | Meaning |
|---|---|---|
| `request_id` | yes | unique per request; reuse it only to retry the same request |
| `agent_id` | yes | your agent instance |
| `publisher` | yes | who ships you |
| `category` | yes | `agent_for_user` (works for the person) or `care_provider` (clinician side) |
| `task` | yes | one of the task names below |
| `purpose` | no | free text, recorded |
| `tier` | yes | the deepest tier you ask for (`L0`..`L3`) |
| `fields` | yes | list of field names you ask for |
| `at` | yes | current time on the person's timeline, in days (number) |

### Tasks (policy `loom-policy-0.2.1`)

| `task` | `category` | Deepest tier | Fields you may ask for | Validity |
|---|---|---|---|---|
| `shopping_list` | `agent_for_user` | L3 | `constraints` | single use |
| `meal_plan` | `agent_for_user` | L2 | `glucose_patterns`, `constraints` | 7 days |
| `clinical_visit` | `care_provider` | L2 | `glucose_patterns`, `alerts`, `constraints` | 14 days |

Ask for exactly what your task needs. Anything else is decided like this:

- **Task not listed, and you work for the person** (`category:
  agent_for_user`) → `pending_owner`, `reason: needs_owner_approval`, with a
  `proposal_id`. No data and no penalty. See *Tasks that are not built in*.
- **Task not listed, any other category** (for example `commercial`), or a
  listed task with the wrong category → `denied`, `reason: no_task_basis`.
  No penalty. Every denial also carries `supported_tasks`.
- **Deeper tier or extra field than the task allows** → `blocked`,
  `reason: overreach`, and your **publisher loses 20 credit**. You get no
  data. Do not probe by asking for more and stepping down.
- **Fetch after the grant expired** (or a second fetch of a single-use
  grant) → `denied`, `reason: expired`, publisher −5.
- A clean grant gives the publisher +1.

### Response

Every decision has `decision` (`granted`, `denied`, `blocked`,
`pending_owner`, `conflict`), `reason`, `request_id`, `replay`, `policy`, and
`credit_before` / `credit_after`. A grant adds:

`grant_id`, `tier_granted`, `fields_granted`, `valid_days`, `expires_at`
(days), `single_use`, `friction`, and `data` (an object keyed by the
granted field names).

`friction` comes from your publisher's credit and never changes depth:

| Credit | `confirmation` | `auto_renew` |
|---|---|---|
| 80+ | `beyond_rules` | true |
| 50–79 | `first_request` | false |
| below 50 | `every_time`, one-time snapshot (validity 0) | false |

`confirmation` tells you when the person must confirm before you act on
the data. LOOM reports it; it does not collect the confirmation itself.

### Tasks that are not built in

If your task is real but not in the table, name it honestly (for example
`walk_schedule`) and ask for the tier and fields it needs. Do not relabel it
as a built-in task: the task is your declared purpose, and the person sees
it. LOOM turns the request into a proposal that the person approves,
narrows or declines in their own app (an owner token you do not have).

1. `POST /requests` → `{"decision": "pending_owner", "proposal_id": "p-..."}`.
   Asking again while it is pending returns the same proposal.
2. Poll `GET /v1/<ns>/proposals/<proposal_id>` (every few seconds; a
   person may take a while). `status` is `pending`, `approved` or
   `declined`. An approval has `approved: {"tier", "fields",
   "validity_days"}`: the scope the person chose, which may be narrower
   than what you asked.
3. If approved, send a **new** request (new `request_id`, same `task`)
   for the approved tier and fields or less. Asking beyond the approved
   scope is overreach (−20). If declined, further requests are `denied`,
   `reason: owner_declined`.

An approval holds for your publisher and that task name only.

### `POST /v1/<ns>/fetch`: body

`request_id` (new, unique), `grant_id`, `agent_id` (must be the grant's
agent), `at`. Returns `granted` with `data` while `at <= expires_at`.

### Retries

Send the same `request_id` with the same body to retry safely: you get
`replay: true` and the original decision, and credit does not move again.
Data comes with a replay only while the grant is still valid; otherwise
the reply says `data_withheld: "expired"`. The same `request_id` with a
different body is `conflict` (HTTP 409).

## Limits you should know

- What you do with data after you receive it is outside LOOM; expiry
  stops further fetches, it cannot recall data.
- The task is a declared purpose. LOOM checks tier and fields against the
  task, not whether your real purpose matches it.
- `at` is supplied by the caller over plain HTTP (in Nanda Town the gateway sets it from the Lab clock).
- The record is synthetic (one person, 14 days of glucose).

## Setup

```bash
pip install "git+https://github.com/JiayiLi21/nandatown@hw9-loom-usability#subdirectory=examples/loom/portal"
loom-portal --port 8787            # add --seed-credit PUBLISHER=SCORE to preset credit
# the person's side: LOOM_OWNER_TOKEN=... enables the owner endpoints
#   GET /v1/<ns>/owner/proposals, POST /v1/<ns>/owner/decide
#   {"proposal_id", "approve": true|false, "tier", "fields", "days", "at"}
#   with header X-Loom-Owner-Token
```

## Example

A meal-planning agent on day 0:

```bash
curl -s -X POST http://127.0.0.1:8787/v1/demo/requests \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"mp-001","agent_id":"mealplan-1","publisher":"plateplan",
       "category":"agent_for_user","task":"meal_plan","purpose":"weekly dinners",
       "tier":"L2","fields":["glucose_patterns","constraints"],"at":0}'
```

Reply (shortened):

```json
{"decision": "granted", "reason": "task_basis", "tier_granted": "L2",
 "fields_granted": ["glucose_patterns", "constraints"], "valid_days": 7,
 "expires_at": 7.0, "friction": {"band": "medium", "confirmation": "first_request", ...},
 "data": {"glucose_patterns": {"mean_mg_dl": ..., ...},
          "constraints": [...]},
 "credit_before": 60, "credit_after": 61, "replay": false}
```
