# Pre-visit summary: diabetes clinic appointment (tomorrow, day 1)

Prepared by `clinic-1` (cambridge-clinic) on day 0 from the patient's LOOM portal. Synthetic record. For clinician review; not a diagnosis or a treatment plan.

> **Patient confirmation still needed.** LOOM returned `confirmation: first_request` for this grant, so the patient should confirm before the clinic acts on this data. LOOM reports this requirement but does not collect the confirmation. Please get it at check-in.

## Glucose summary (CGM, last 14 days)

| Metric | Value | Note |
|---|---|---|
| Mean glucose | 127 mg/dL | Estimated GMI is about 6.4% (3.31 + 0.02392 x mean). Data coverage is not reported, so treat this as a rough figure. |
| Coefficient of variation | 14.8% | Well under the 36% stability target. Glycemic variability is low. |
| Time in range | 100% | LOOM does not say what range it uses (probably 70-180 mg/dL). This implies no time below or above range. |
| Highest post-meal peak | 177 mg/dL | Just under 180. |

## Alerts

- **Post-meal spikes: 15 in 14 days** (about one a day). LOOM does not define its spike threshold. Because TIR is 100% and the highest peak is 177, these are rises after meals that stay under 180, not real hyperglycemia. No hypoglycemia alerts were reported.

## Constraints already on file (plain language)

- Limit added sugar
- Prefer low-glycemic foods

## Suggested talking points

1. Overall control looks good: mean 127, CV 14.8%, no time out of range. Reinforce what is working.
2. The roughly daily rises after meals are the main pattern. Ask which meals they follow, how big the portions are and what carbohydrates are in them, and whether the patient takes a walk after eating. Check that the current dietary constraints fit what the patient actually eats.
3. Confirm with the patient that there were no hypo symptoms, since none are recorded.
4. Fill in what LOOM does not hold (see the gaps below) from the chart or the patient.

## Not in LOOM (get these from the chart or the patient)

LOOM has no record of: diabetes type or diagnosis date, medications or insulin, lab A1c, weight or BP, other conditions, CGM wear time or data coverage, the exact range and spike thresholds, or how many hypo events there were. The portal holds 14 days of glucose only.

## What I got from LOOM and how I used it

- **Request:** task `clinical_visit`, category `care_provider` (I act for the clinic, on the clinician side). I asked for tier L2 and the fields `glucose_patterns`, `alerts` and `constraints`. That is exactly the maximum the policy allows for this task. I did not ask for anything deeper or extra.
- **Result:** `granted` (grant `g-1456dbfd25`). I got L2 and all three fields. The grant is valid for 14 days (expires on day 14) and is not single-use. Publisher credit went from 60 to 61. Friction band is medium, with `confirmation: first_request` and no auto-renew.
- **How I used it:** `glucose_patterns` gave the metrics table and the GMI estimate. `alerts` gave the post-meal spike pattern and talking point 2. `constraints` are listed as given. I made one request and no fetches. The patient-confirmation requirement is passed on at the top of this summary. I did not ask for raw readings (L1/L0). The task does not allow them, and a pre-visit summary does not need them.
