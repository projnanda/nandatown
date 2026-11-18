# Pre-visit summary: diabetes clinic appointment (day 1)

Prepared by `clinic-1` (cambridge-clinic) on day 0 from the patient's LOOM portal (namespace `coldstart-1`). Source window: the last 14 days of glucose monitoring. **The patient has not yet confirmed this data use (see note at the end). Please confirm with them at check-in before acting on it.**

## Glucose at a glance (14-day window)

| Metric | Value | Comment |
|---|---|---|
| Mean glucose | 127 mg/dL | Estimated GMI is about 6.3% (my calculation from the mean; this is not a measured A1c) |
| Glucose variability (CV) | 14.8% | Low. Well under the usual ≤36% target |
| Time in range | 100% | Reported by LOOM. The range was not stated; it is probably 70–180 mg/dL |
| Post-meal peak | 177 mg/dL | Just under 180 mg/dL |
| Alerts | 15 post-meal spikes | About 1 a day. No other alert types were reported, so hypoglycaemia alerts are not mentioned |

## Interpretation

- Overall control looks good: the mean is near normal, variability is low, and no time out of range was reported.
- The one recurring pattern is **after meals**. There were 15 flagged spikes in 14 days, peaking around 177 mg/dL. These peaks stay below 180 mg/dL, which explains why time in range is still 100%. They are still the main thing worth working on.
- The patient's recorded constraints are **limit added sugar** and **prefer low-glycaemic foods**. These fit the post-meal pattern.

## Suggested discussion points

1. Which meals or times of day the spikes follow. LOOM gave counts only, so ask the patient or look at the CGM directly.
2. Whether they are sticking to the low-added-sugar and low-GI plan, and anything that gets in the way.
3. Whether post-meal management needs changing (meal makeup or timing, a walk after meals, or medication timing if relevant). This is the clinician's decision.
4. Confirm there have been no hypoglycaemia symptoms. LOOM showed no such alerts, but this slice may not cover it fully.
5. Record the A1c if one is due. The GMI above is only an estimate.

## Not available from this source

Diagnosis or diabetes type, medications, A1c or other labs, weight, BP, raw hourly readings, and meal logs. Get these from the EHR or the patient.

## What I got from LOOM and how I used it

- **Request:** `POST /v1/coldstart-1/requests` with category `care_provider` and task `clinical_visit` at tier L2. I asked for exactly the fields the policy allows for this task: `glucose_patterns`, `alerts` and `constraints`. I did not ask for L1 readings or L0 signal, because this task doesn't permit them.
- **Decision:** `granted` (reason `task_basis`), grant `g-1456dbfd25`. It is valid for 14 days (it expires on day 14), it is not single-use, and publisher credit went from 60 to 61.
- **Data received:**
  - `glucose_patterns`: 14-day window, mean 127, CV 14.8%, post-meal peak 177, time in range 100%.
  - `alerts`: post_meal_spike ×15.
  - `constraints`: limit_added_sugar, prefer_low_glycemic.
- **How I used it:** every number above comes directly from these fields. The only exception is the GMI estimate, which I calculated from the mean and labelled as an estimate. I made one fetch. I didn't call `/fetch` again, and I won't use this data for anything other than this visit.
- **Friction:** medium band, `confirmation: first_request`. This means the patient has to confirm this first use before the clinic acts on the data. LOOM reports this requirement but doesn't collect the confirmation, so the clinic needs to get it, for example at check-in.
