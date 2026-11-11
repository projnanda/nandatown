# Leal in Nanda Town: a job agent follows a free recipe

[Leal](https://github.com/Leal-protocol/leal) helps people trust their agents with jobs
that matter. Its first product is **recipes**: a job someone already figured out, written
down so any agent can follow it. The agent follows the recipe while it works, the way a
cook follows a recipe while cooking, so it does the job well the first time.

This integration puts Leal's recipe service in Town. A candidate's job agent finds a job
posting and Leal through Town's registry, asks Leal for its free job-application recipe
over Town's messaging, and follows the recipe step by step. At the recipe's `self_check`
step, the last one before submitting, it sends the exact application to Leal, which runs
its `check_application` tool and answers approve, ask the candidate, or refuse, with a
signed attestation. The agent submits only on approve and attaches the attestation, and
the employer verifies the signature against the key Leal published in Town's registry.
So two Leal tools run end to end in Town: `get_recipe` and `check_application`.

The recipe was written by a team member from more than 20 real job applications. Its first
step is the one that saved the most work in practice: read the posting before writing
anything, and stop if it bars visa sponsorship. In his first batch, 6 of 17 queued jobs
were impossible for that reason.

## What is in this folder

| File | What it does |
|---|---|
| `plugin.py` | Three roles (`leal_recipes`, `employer`, `job_agent`) and two validators |
| `leal_source.py` | Gets the recipe and Leal's check from the live Leal service, or from the captured copy; verifies attestations |
| `scenarios/recipe_application.yaml` | The main workflow |
| `scenarios/sponsorship_bar.yaml` | The boundary case, with a comparison agent that has no recipe |
| `fixtures/recipe.json` | Leal's answers exactly as its MCP tools returned them: the recipe, its public key, and its decision and attestation for each packet (Leal commit in the file) |
| `fixtures/packets.json` | A made-up candidate's resume and letter, plus the same letter with one invented number; no real person's data |
| `tools/leal_bridge.py` | Runs inside Leal's environment and calls Leal's MCP server over stdio |
| `tools/capture_fixture.py` | Refreshes `fixtures/recipe.json` from a live Leal |
| `../../tests/test_leal_integration.py` | 11 tests, all offline |

## How Town connects to Leal

Leal runs as its own MCP server, outside Town. Town imports no Leal code and adds no
dependency.

- **Live mode.** The `leal_recipes` participant starts `tools/leal_bridge.py` inside a Leal
  checkout. The bridge launches Leal's MCP server over stdio, exactly as an agent host
  does, and calls its `get_recipe`, `check_application` and `leal_public_key` tools. Set `NANDATOWN_LEAL_MODE=live` and
  `NANDATOWN_LEAL_ROOT=<path to Leal>`; it needs `uv`. Leal's repo is private while the
  team builds it.
- **Offline mode, the default.** Leal's answers come from `fixtures/recipe.json`, captured
  from the live tools. Anyone can replay every scenario with no Leal checkout and no model
  account. A packet that was not captured gets no answer, so offline mode never invents a
  decision.

Inside Town, everything goes through Town's own layers: the employer and Leal publish cards
to the registry, the job agent looks both up, the recipe and the check travel as ordinary
Town messages, and every step lands in the event log, so the result is judged from the
trace alone. The employer verifies Leal's Ed25519 signature with `cryptography`, which Town
already depends on.

## Reproduce

Nanda Town base commit `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`, nandatown 0.2.0,
Python 3.12. Leal commit `98e308a` for live mode.

```bash
python -m pip install -e '.[dev]'
python -m pytest tests/test_leal_integration.py -v
nandatown run integrations/leal/scenarios/recipe_application.yaml
nandatown run integrations/leal/scenarios/sponsorship_bar.yaml
```

Live mode, with a Leal checkout next to this repo:

```bash
NANDATOWN_LEAL_MODE=live NANDATOWN_LEAL_ROOT=../leal \
  nandatown run integrations/leal/scenarios/recipe_application.yaml
```

## Results

| Run | Expected | Observed |
|---|---|---|
| `recipe_application`, offline and live | The agent finds the job and Leal, gets the recipe, follows its six steps in order, has Leal check the exact application at `self_check`, applies with Leal's attestation, the employer verifies it, and the agent records the confirmation word for word | All stages passed. Steps: screen > research > tailor_resume > write_letter > self_check > submit. Leal approved the exact packet that was submitted (8 checks); the employer verified the signature |
| Letter with an invented number (test) | The letter says 900+ live transactions where the confirmed fact is 700+. Leal refuses at `self_check` and nothing is submitted | `recipe_self_check_passed` failed with "Not in the bullet bank: 900+"; no application reached the employer |
| Application changed after Leal checked it (test) | The employer's check rejects it | "the application changed after Leal checked it"; a wrong key and a forged signature are rejected too |
| Leal unavailable at `self_check` (test) | The agent fails closed and does not submit | Application held; no application reached the employer |
| `sponsorship_bar`, offline and live | The agent with the recipe stops at the screening step and writes nothing. The same agent without the recipe applies and is rejected | Passed. Stopped on "will not be providing visa sponsorship" with 0 documents written; the agent without the recipe wrote 2 documents and was rejected |
| Recipe with its screening step removed (test) | The stop disappears, which shows the stage measures the recipe, not a rule built into the agent | `recipe_agent_stopped_at_screening` failed, as expected |
| Leal missing from the registry (test) | The agent still applies, records that it had no recipe, and the run does not pass | `recipe_served_by_leal` reported missing evidence; verdict not passed |

## What this shows, and what it does not

It shows that Leal works as a Town participant with two tools: discovered through the
registry, called over Town's messaging, and live against Leal's real MCP server. It shows
two concrete benefits: an agent that follows the recipe does not spend work on a job its
candidate cannot get, and an application with a fact the candidate never confirmed does not
go out in their name. It also shows the employer can tell, from Leal's signature alone,
that what arrived is exactly what Leal checked.

The Town agents are scripted, so this does not measure how much a recipe improves a real
model's work. That needs real agents writing real documents, with and without the recipe,
compared on quality, time and tokens. It is the next test (course HW9).

Also untested here: the other recipe steps' effect on document quality, recipe versioning
across runs, and Town's `nandatown verify` in a fresh process. That command does not yet
load a scenario's `plugin_files`, so it cannot replay these validators outside the process
that loaded them. The tests verify each bundle in the same process instead, after loading
the plugin.

On Windows, 36 of Town's existing tests fail before and after this change because they
create symlinks, which need administrator rights there. This change adds no failures.
