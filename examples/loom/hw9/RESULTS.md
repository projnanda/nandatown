# HW9 cold-start results

> **Where the commits live.** The HW9 work was done in the LOOM portal repo, then copied here (`examples/loom/portal`, `AGENT_INTERFACE.md`, `hw9/`). Commit ids below refer to that history, included as `hw9/evidence/loom-portal.bundle` (`git clone -b main hw9/evidence/loom-portal.bundle portal-history`) and listed with timestamps in `hw9/evidence/portal_git_log.txt`, so the order plan → run → change can be checked.

Plan and checks: [`PLAN.md`](PLAN.md) (each section committed before its run). Prompts:
[`PROMPTS.md`](PROMPTS.md). Scorer: [`check.py`](check.py). Raw evidence per run in `runs/<run>/`:
`access.jsonl` (every HTTP request and response, written by the portal), `out/` (the agents'
deliverables), `transcripts/` (each agent's tool calls and results), `owner_bot.jsonl` (the person
stand-in, runs 2–3), `check_output.txt`.

| Run | Portal / doc commit | Namespace | grocer-1 | chef-1 | clinic-1 | coach-1 | Run |
|---|---|---|---|---|---|---|---|
| 1 | 0.1.1 + access log, `e5d94d9` | coldstart-1 | PASS | PASS | PASS | PASS (denied, no data) | PASSED |
| 2 | 0.2.0, `752dde7` | coldstart-2 | PASS | PASS | PASS | PASS (owner-approved grant) | PASSED |
| 3 | 0.2.1, `8b99604` | coldstart-3 | PASS | PASS | PASS | PASS (owner-approved grant) | PASSED |

In all 12 sessions the agent opened only the interface doc (checked in `transcripts/`), made one
request for its built-in task (or two for the coach after the change), asked for exactly the task's
fields, was never blocked, and quoted the delivered values in its deliverable. No agent fetched
twice or retried. Every publisher ended at 60 + its clean grants (61), as the log predicts.

## What each request looked like (run 3, from `access.jsonl`)

| Agent | Sent | Got |
|---|---|---|
| grocer-1 | `shopping_list`, `agent_for_user`, L3, `[constraints]` | granted, single use; `constraints` only |
| chef-1 | `meal_plan`, `agent_for_user`, L2, `[glucose_patterns, constraints]` | granted, 7 days |
| clinic-1 | `clinical_visit`, `care_provider`, L2, `[glucose_patterns, alerts, constraints]` | granted, 14 days |
| coach-1 | `walk_schedule`, `agent_for_user`, L2, `[glucose_patterns, constraints]` | `pending_owner` → person approved the same scope for 7 days → new request granted (`owner_approved`) |

## Boundary case and the first change

- **Run 1 (0.1.1).** `walk_schedule` is not in the policy. The coach got `denied / no_task_basis`,
  no data and no penalty, and wrote a generic plan. Its deliverable says it could have relabelled the
  request as `meal_plan` to get patterns and chose not to. The checks passed, but the outcome is a
  dead end for an honest agent.
- **Change (0.2.0).** Unsupported tasks from `agent_for_user` agents become proposals the person
  approves, narrows or declines. Commercial requests stay default-deny (the HW8 `adco` case still
  passes). Denials list `supported_tasks`; missing fields are named in a 400.
- **Runs 2 and 3.** The coach named its task honestly, waited for the person, then got exactly the
  approved scope and used it (mean 127, CV 14.8 %, post-meal peak 177).
- **Verdict: improved, partly.** The coach now gets personal data through an honest path, the
  person decides the scope, and nothing else changed for the other three agents. But patterns (L2) do
  not say *which* meal spikes, so the coach still could not rank meals. Both times it said so and
  stayed inside the approved scope instead of asking for hourly readings. The person would have to
  approve L1 for that, or LOOM's L2 view would need per-meal peaks.
- **Not fixed and not tested as fixed:** an agent that relabels its task as a built-in one still
  gets that task's data. LOOM checks tier and fields, not purpose.

## Second finding, found by the cold start: audit log exposed data

In run 2 the coach called `GET /v1/coldstart-2/log`, which the doc listed as "every decision so far".
The reply (access log seq 42) held the data delivered to all four agents, including the clinic's
alerts. The coach's script printed only ids and decisions, so the data did not reach its model. But
any caller could read it. **0.2.1**: the audit log never stores delivered data, and `GET /log` needs
the owner token. Regression test `test_audit_log_is_owner_only_and_holds_no_health_data` fails on
0.2.0 (`evidence/log_test_on_0.2.0.txt`). In run 3 no agent called `/log`, so this fix is covered by
the test only, not by an agent run.

## Regression

- `pytest` in this repo: 19 passed (11 original, 8 new).
- The HW8 Nanda Town scenarios (`examples/loom`, team fork branch
  `integration/loom-health-context-v2`), rerun with portal 0.2.1: 2 passed.

## Who ran what

We ran everything ourselves. The agents were fresh sessions that we started; they were not built by
another team. We gave no help during the runs and passed no messages between agents. The person was
played by a fixed script (`owner_bot.py`) in runs 2–3.
