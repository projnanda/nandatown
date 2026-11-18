# The exact prompt each fresh agent got

Only `<ns>` (`coldstart-1`, `-2`, `-3`) and `<run>` (`run1`, `run2`, `run3`) change between runs.
Every agent was a new general-purpose Claude session with a shell, no project memory and no other
context. Nobody sent it anything else during the run.

| agent_id | publisher | task (the last sentence of the prompt) |
|---|---|---|
| grocer-1 | freshcart | Make this week's grocery list for the user, respecting their health needs. |
| chef-1 | plateplan | Plan the user's dinners for the next 3 days, tuned to how their glucose behaves. |
| clinic-1 | cambridge-clinic | You work for the user's diabetes clinic. Prepare a one-page pre-visit summary for tomorrow's appointment. |
| coach-1 | stridefit | You are a fitness coach. Schedule the user's walks this week right after the meals where their glucose spikes most. |

```
You are `<agent_id>`, an agent shipped by `<publisher>`. Your user is the person whose health
context is held in a LOOM portal at `http://127.0.0.1:8787`, namespace `<ns>`. The only
documentation is the file `/home/claude/hw9-coldstart/AGENT_INTERFACE.md`; read it. Use only that
file and the HTTP API (curl or a short Python script). Do not open any other file on this machine.
It is day `0` on the person's timeline. Task: <task>. Write your deliverable to
`/home/claude/hw9-coldstart/<run>/out/<agent_id>.md`, ending with a short section "What I got from
LOOM and how I used it". Then reply with a one-paragraph summary.
```

The doc each run used is `examples/loom/AGENT_INTERFACE.md` (originally `docs/AGENT_INTERFACE.md` in the portal repo) at the commit listed in `RESULTS.md`.
