# SPDX-License-Identifier: MIT
# Copyright (c) 2026 nanda-resilience contributors
from __future__ import annotations

from html import escape

COLORS = {"price": "#f472b6", "random": "#94a3b8", "load": "#fbbf24",
          "dependency": "#2dd4bf", "oracle": "#a78bfa"}


def number(value: float | None, percent: bool = False) -> str:
    if value is None:
        return "not observed"
    return f"{100 * value:.1f}%" if percent else f"{value:.3f}"


def render_html(summary: dict) -> str:
    aggregate = summary["aggregate"]
    disclosures = summary["design"]["disclosures"]
    full = max(disclosures)
    hero = [r for r in aggregate if r["condition"] == "shared" and r["disclosure"] == full]
    bars = "".join(
        f'<div class="bar-row"><span>{escape(r["policy"])}</span>'
        f'<div class="bar-track"><div class="bar" style="width:{100*r["stress_completion_rate"]:.2f}%;'
        f'background:{COLORS[r["policy"]]}"></div></div>'
        f'<strong>{number(r["stress_completion_rate"], True)}</strong></div>'
        for r in hero
    )
    cells = []
    for r in aggregate:
        cells.append(
            "<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in [
                r["condition"], f'{r["disclosure"]:.0%}', r["policy"],
                number(r["stress_completion_rate"], True),
                number(r["completion_rate"], True),
                number(r["attempts_per_task"]),
                number(r["cost_per_success_cents"]),
                number(r["dependency_hhi_before_stress"]),
            ]) + "</tr>"
        )
    points = []
    for policy, color in COLORS.items():
        runs = [r for r in summary["runs"] if r["condition"] == "shared"
                and r["disclosure"] == full and r["policy"] == policy]
        if not runs:
            continue
        at_values = [p["at"] for p in runs[0]["timeline"]]
        coords = []
        for i, at in enumerate(at_values):
            completion = sum(r["timeline"][i]["completion"] for r in runs) / len(runs)
            coords.append(f"{50 + at / max(at_values) * 690:.2f},{220 - completion * 180:.2f}")
        points.append(f'<polyline fill="none" stroke="{color}" stroke-width="3" '
                      f'points="{" ".join(coords)}"><title>{policy}</title></polyline>')
    paired = []
    for row in summary["paired"]:
        if row["condition"] != "shared" or row["disclosure"] != full \
                or row["metric"] != "stress_completion_rate":
            continue
        interval = row["bootstrap_95_percentile"]
        uncertainty = (
            f'[{100*interval[0]:+.1f}, {100*interval[1]:+.1f}] pp'
            if interval is not None else "not estimated: fewer than two seeds"
        )
        paired.append(
            f'<li><b>{escape(row["contrast"])}</b>: '
            f'{100*row["mean_difference"]:+.1f} percentage points; '
            f'95% paired bootstrap {uncertainty} '
            f'({row["paired_seeds"]} seeds).</li>'
        )
    def aggregate_for(condition, policy):
        return next((r for r in aggregate if r["condition"] == condition
                     and r["disclosure"] == full and r["policy"] == policy), None)

    independent_aware = aggregate_for("independent", "dependency")
    independent_price = aggregate_for("independent", "price")
    clean_aware = aggregate_for("clean", "dependency")
    clean_price = aggregate_for("clean", "price")
    tradeoff = ""
    if all(row is not None for row in (
        independent_aware, independent_price, clean_aware, clean_price
    )):
        tradeoff = f"""<section class="panel"><h2>The counterexample matters</h2>
<p>In the staggered provider-outage control, stress-window completion is
<b>{number(independent_aware["stress_completion_rate"], True)}</b> for dependency-aware
selection versus <b>{number(independent_price["stress_completion_rate"], True)}</b>
for price selection. In clean operation the average successful task costs
<b>{number(clean_aware["cost_per_success_cents"])} cents</b> versus
<b>{number(clean_price["cost_per_success_cents"])} cents</b>.</p>
<p class="muted">The candidate is not assumed to win every failure regime.
Only explicitly reported backend failures affect group-level retry avoidance;
provider-local failures exclude the individual provider, not its whole group.</p></section>"""
    seed_count = len(summary["design"]["seeds"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Many Agents, One Point of Failure | Nanda Town experiment</title>
<style>
:root{{color-scheme:dark;font-family:system-ui,sans-serif;background:#0b1220;color:#e2e8f0}}
body{{max-width:1120px;margin:auto;padding:40px 24px}}h1{{font-size:clamp(2.2rem,5vw,4rem);line-height:1.08;max-width:850px}}
h2{{margin-top:0}}p,li{{line-height:1.7}}.eyebrow{{color:#2dd4bf;text-transform:uppercase;letter-spacing:.15em;font-size:.8rem}}
.muted{{color:#a8b6c9}}.panel{{background:#111e32;border:1px solid #27364c;border-radius:16px;padding:26px;margin:24px 0}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}.bar-row{{display:grid;grid-template-columns:90px 1fr 65px;gap:12px;align-items:center;margin:20px 0}}
.bar-track{{height:16px;background:#243248;border-radius:5px;overflow:hidden}}.bar{{height:100%}}.scroll{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:.85rem}}td,th{{padding:10px;text-align:left;border-bottom:1px solid #27364c;white-space:nowrap}}
th{{color:#2dd4bf}}a{{color:#67e8f9}}code{{color:#c4b5fd}}svg{{width:100%;height:auto}}
@media(max-width:760px){{.grid{{grid-template-columns:1fr}}body{{padding:24px 14px}}.panel{{padding:18px}}}}
</style></head><body>
<div class="eyebrow">Nanda Town Lab / reproducible systems experiment / v{summary["version"]}</div>
<h1>Many agents.<br>One point of failure.</h1>
<p class="muted">Different identities are not necessarily independent alternatives.
A controlled comparison of agent selection under shared backend outages.</p>
<div class="grid"><section class="panel"><h2>The experiment</h2>
<p>12 service agents. Nine share one backend; three use separate backends.
Eight buyers submit 20 tasks each. Three attempts per task. Every request, response,
reservation and payment is evaluated against the exported Nanda Town trace.</p>
<p><b>{len(summary["runs"])} runs / {seed_count} paired seeds</b><br>
No LLM credentials, external services, or real money.</p></section>
<section class="panel"><h2>Stress-window completion</h2>
<p class="muted">Shared outage, {full:.0%} declared dependencies. Mean across seeds;
tasks created during logical time [12, 28).</p>{bars}</section></div>
<section class="panel"><h2>What changes when the shared backend disappears?</h2>
<svg viewBox="0 0 780 270" role="img" aria-label="Task cohort completion rate over logical time">
<path d="M50 40V220H740" fill="none" stroke="#64748b"/>
<text x="5" y="45" fill="#a8b6c9" font-size="12">100%</text>
<text x="15" y="225" fill="#a8b6c9" font-size="12">0%</text>
{"".join(points)}
<text x="50" y="255" fill="#a8b6c9" font-size="12">Task creation time (logical units); shared outage: 12 to 28</text>
</svg>
<p>{" / ".join(f'<span style="color:{c}">{p}</span>' for p,c in COLORS.items())}</p>
<ul>{"".join(paired)}</ul>
<p class="muted">Seed-level intervals characterize this synthetic workload, not real-world reliability.
An interval of zero width may reflect deterministic structure, not certainty about deployment.</p></section>
{tradeoff}
<section class="panel"><h2>All conditions, including costs and incomplete disclosure</h2>
<div class="scroll"><table><thead><tr><th>Condition</th><th>Disclosure</th><th>Policy</th>
<th>Stress success</th><th>Overall success</th><th>Attempts/task</th>
<th>Cents/success</th><th>Pre-stress dependency HHI</th></tr></thead>
<tbody>{"".join(cells)}</tbody></table></div></section>
<section class="panel"><h2>What this does not prove</h2>
<ul><li>This is a seeded simulation with a shared reservation coordinator, not a decentralized deployment.</li>
<li>Dependency labels are truthful when present. Missing labels share one conservative unknown group.
No inference, dishonest declarations, or multi-hop dependencies are modeled.
Structured failure reasons are also assumed truthful, not inferred from timeouts.</li>
<li>The oracle knows dependency identities, not future outages. It is an information-rich comparator, not a proven optimum.</li>
<li>The independent control staggers nine provider outages with the same total provider downtime as the shared case.
It does not match instantaneous unavailable capacity or outage exposure for every task.</li>
<li>Prices are synthetic. Only successful tasks are charged; failed attempts still consume messages and retry budget.</li>
<li>A passing evidence verdict means internal consistency, not a resilience certificate or verified real-world dependency truth.</li></ul>
<p><a href="summary.json">Full results and paired intervals (JSON)</a> /
<a href="runs.csv">Per-run metrics (CSV)</a> /
<a href="https://github.com/projnanda/nandatown">Upstream Nanda Town</a></p>
<p class="muted">Upstream compatibility baseline (not the executing revision):
<code>{escape(summary["upstream_compatibility_baseline"])}</code>.
Executing Nanda Town version: <code>{escape(summary["nandatown_version"])}</code>.
Source experiment version: <code>{escape(summary["source_experiment_version"])}</code>.
Research prototype; no claim of MIT affiliation, endorsement, or first-of-its-kind novelty.</p>
</section></body></html>"""
