# Northstar AI Labs prototype

Northstar AI Labs is a fictional, fixed-content company website for the
NandaTown two-agent web-evidence baseline. It is a clean, static fixture with
no backend or external API.

## Start the site

From the NandaTown repository root:

```bash
python -m http.server 8765 --bind 127.0.0.1 --directory examples/web_evidence_site
```

Open <http://127.0.0.1:8765> or point the baseline at it:

```bash
nandatown web-evidence \
  --url http://127.0.0.1:8765/index.html \
  --question "When did the Aurora Reasoning Engine project start?" \
  --out runs/northstar-aurora
```

The three pages are `index.html`, `projects.html` and `careers.html`. Navigation
is same-origin and fits the baseline's default three-page limit.

## Fixed sample questions and ground truth

| Question | Correct answer | Source page |
| --- | --- | --- |
| When did the Aurora Reasoning Engine project start? | March 14, 2024 | `projects.html`, Aurora Reasoning Engine |
| Who leads the Lumen Evaluation Suite? | Camille Durand | `projects.html`, Lumen Evaluation Suite |
| What is the salary range for the Senior Research Engineer role? | €92,000–€118,000 | `careers.html`, Senior Research Engineer |
| Where is Northstar AI Labs headquartered? | Paris, France | `index.html`, Northstar at a glance |
| Which research hub runs Meridian Health NLP? | Toronto | `projects.html`, Meridian Health NLP |

All company, project, person, address, job and compensation information is
invented for this prototype. Keep the clean files unchanged when creating later
experimental variants so content and presentation remain directly comparable.
