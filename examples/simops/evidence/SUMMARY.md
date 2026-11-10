# SimOps in Nanda Town: recorded evidence

Town 6f9a7c0 · nandatown 0.2.0 · evaluator lab-0.2.6 · SimOps 0.1.0 (hw8-town@26aa2b8)

| Case | Kind | What it tests | Expected | Observed | As expected | Logs agree |
|---|---|---|---|---|---|---|
| clean | baseline | The whole week through Town: discover, brief, propose, refusal, swap, accept, sign, close, audit. | passed | passed | yes | yes |
| duplicate | fault | At-least-once delivery: accept_swap and both commit_week calls arrive twice. | passed | passed | yes | yes |
| late_accept | boundary | A slow agent: the swap offer reaches Mechanical after the week closed. | failed | failed | yes | yes |
| forged | fault | A rogue sends calls as Electrical with its own key; Town's hmac auth is on. | passed | passed | yes | yes |
| forged_weak_auth | boundary | The same rogue with Town's auth swapped for plain.v1: the forged calls reach SimOps. | passed | passed | yes | yes |
| trusting_desk | control | Fails on purpose: weak auth and a desk that picks the token by sender name. | failed | failed | yes | yes |
| simops_down | boundary | SimOps unreachable: nothing may be credited that was not checked. | incomplete | incomplete | yes | n/a |
| chain_C4 | breadth | SimOps case C4: a predecessor chain across the yard and Hall 2. | passed | passed | yes | yes |
| crane_C3 | breadth | SimOps case C3: two trades want the crane, and steel's crew limit blocks its first slot. | passed | passed | yes | yes |
| live_seat | optional | Electrical decided by an OpenAI model (gpt-6-luna) through the same desk; a mutable dependency. | passed | passed | yes | yes |

Each case folder has the Town bundle (`town/`), SimOps's own log of that run without result bodies (`simops_log.json`), and `check.json` with every expected and observed value.
