# Pre-visit summary: diabetes clinic appointment (tomorrow, day 1)

Prepared by `clinic-1` (publisher `cambridge-clinic`) on day 0 from the patient's LOOM portal (namespace `coldstart-3`). The record is synthetic.

> **Before use:** LOOM returned `confirmation: first_request` for this grant. The patient has to confirm this first use of their data before the clinic acts on it. LOOM reports that requirement but doesn't collect the confirmation, so the front desk or clinician should get it, for example at check-in.

## Glucose over the last 14 days (CGM-derived patterns)

| Metric | Value | Comment |
|---|---|---|
| Mean glucose | 127 mg/dL | Equals an estimated GMI of about 6.3% (computed from the mean, not a lab A1c) |
| Glucose variability (CV) | 14.8% | Well under the 36% stability threshold |
| Time in range | 100% | LOOM doesn't state the range; presumably 70–180 mg/dL |
| Post-meal peak | 177 mg/dL | Just under 180 mg/dL |
| Hypoglycaemia | None flagged | No low alerts in the window; 100% TIR implies no time below 70 mg/dL |

## Alerts

- **Post-meal spikes: 15 in 14 days**, about one a day. This is the only alert type. Peaks stay below 180 mg/dL, so the spikes don't push readings out of range. Still, they're the main pattern worth discussing.

## Patient's stated constraints

- Limit added sugar
- Prefer low-glycaemic foods

## Suggested discussion points

1. **Post-meal excursions.** Ask which meals and foods cause the daily spikes and when they happen. Check whether they match what the patient already tries to do (low-GI, limited added sugar). Consider meal timing, portion size, a walk after meals, or a referral for dietary review.
2. **Overall control looks good.** Mean, CV and TIR are all at target. Reinforce the current regimen; there's no sign of hypoglycaemia risk in this data.
3. **Things to confirm in person** (not available from LOOM): current medications and doses, a recent lab A1c, diabetes type, weight/BP, the patient's own goals, and what range LOOM uses for TIR and what it counts as a "spike".

## Limits of this summary

- It covers derived patterns only (L2). No hourly readings were requested, so I can't say what time of day or after which meal the spikes happen.
- 14 days of data from one source, with no medication, lab or history data.
- GMI is an estimate calculated from mean glucose. It doesn't replace an A1c.

## What I got from LOOM and how I used it

- **Request:** one `POST /v1/coldstart-3/requests`, category `care_provider`, task `clinical_visit` (the built-in task for clinic-side visit prep), tier `L2`, fields `glucose_patterns`, `alerts`, `constraints`, `at: 0`. That's exactly what the task allows. I didn't ask for raw signal or hourly readings (L0/L1), because the task doesn't permit them and a one-page summary doesn't need them.
- **Decision:** `granted` (`task_basis`), grant `g-1456dbfd25`, valid 14 days (expires day 14), not single-use. Publisher credit went from 60 to 61 (medium band). Friction: `confirmation: first_request`, `auto_renew: false`.
- **Data received:** `glucose_patterns` (14-day window: mean 127 mg/dL, CV 14.8%, post-meal peak 177 mg/dL, TIR 100%); `alerts` (`post_meal_spike` × 15); `constraints` (`limit_added_sugar`, `prefer_low_glycemic`).
- **How I used it:** the patterns became the metrics table (plus a GMI estimate I calculated from the mean). The alert became the main discussion point. The constraints were used as context for dietary counselling. No other data sources were used. I made no further fetches; the grant can be fetched again until day 14 if the clinic needs fresher numbers on the visit day.
