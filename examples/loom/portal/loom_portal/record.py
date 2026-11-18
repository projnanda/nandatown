"""A synthetic chronic-care record for one person managing type 2
diabetes. Deterministic, so every namespace starts from the same data.
No real person's data is used anywhere in LOOM's tests."""

from __future__ import annotations

import math
import statistics


def synthetic_record(days: int = 14) -> dict:
    # L1: one reading every 5 minutes is 288 a day; keep it to hourly
    # here so payloads stay small. Values in mg/dL.
    readings = []
    for h in range(days * 24):
        hour = h % 24
        meal = 45 if hour in (8, 13, 19) else (25 if hour in (9, 14, 20)
                                                 else 0)
        value = 118 + 14 * math.sin(h / 7.0) + meal
        readings.append({"t_hour": h, "mg_dl": round(value)})
    values = [r["mg_dl"] for r in readings]
    mean = statistics.fmean(values)
    patterns = {                                   # L2
        "window_days": days,
        "mean_mg_dl": round(mean),
        "cv_pct": round(100 * statistics.pstdev(values) / mean, 1),
        "post_meal_peak_mg_dl": max(values),
        "time_in_range_pct": round(100 * sum(70 <= v <= 180 for v in values)
                                   / len(values)),
    }
    alerts = [{"kind": "post_meal_spike", "count": sum(v > 170
                                                       for v in values)}]
    constraints = ["limit_added_sugar", "prefer_low_glycemic"]   # L3
    return {"readings": readings, "patterns": patterns,
            "alerts": alerts, "constraints": constraints,
            "raw_signal": []}
