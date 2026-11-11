"""Regenerate engie_slice.json from the Engie 80-prosumer dataset's agent metadata.

Only needed to check where the scenario's asset limits come from; the scenario
and test read the committed engie_slice.json and never touch the dataset.

    pip install pyarrow
    python examples/pearl_street/make_engie_slice.py path/to/agents_metadata.parquet

Reads one 63 KB metadata file (80 rows); the 136 MB time series is not used.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

ENTITIES = ["DC_08", "PVB_48", "PVB_35", "PVB_12", "PVB_36"]
COLUMNS = ["entity_id", "entity_type", "flex_down_kw", "flex_duration_h",
           "bess_power_kw", "bess_energy_kwh"]


def attested(row: dict) -> dict:
    """The limit a metering service would attest for one entity.

    A data center's dispatchable reduction is its flex_down_kw for
    flex_duration_h hours; a PV + battery site's is its battery's power and
    energy. Rounded to whole kW and kWh.
    """
    if row["entity_type"] == "datacenter":
        power, energy, kind = (row["flex_down_kw"],
                               row["flex_down_kw"] * row["flex_duration_h"],
                               "datacenter")
    else:
        power, energy, kind = row["bess_power_kw"], row["bess_energy_kwh"], "bess"
    return {"entity_id": row["entity_id"], "entity_type": row["entity_type"],
            "asset_type": kind, "power_kw": round(power),
            "usable_kwh": round(energy), "source_power_kw": power,
            "source_energy_kwh": energy}


def main(path: str) -> None:
    import pyarrow.parquet as pq

    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    rows = pq.read_table(path, columns=COLUMNS).to_pylist()
    by_id = {r["entity_id"]: r for r in rows}
    out = {
        "source": {
            "dataset": "Engie 80-prosumer synthetic dataset (2023, 15-minute)",
            "file": os.path.basename(path),
            "sha256": digest,
            "columns": COLUMNS,
            "note": ("Synthetic data built from public sources (NREL, EIA, "
                     "Census, MISO and others; see the dataset README)."),
        },
        "rule": ("datacenter: power_kw = flex_down_kw, usable_kwh = "
                 "flex_down_kw * flex_duration_h; pv_bess: power_kw = "
                 "bess_power_kw, usable_kwh = bess_energy_kwh; rounded"),
        "assets": [attested(by_id[e]) for e in ENTITIES],
    }
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "engie_slice.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    for a in out["assets"]:
        print(a["entity_id"], a["asset_type"], a["power_kw"], "kW",
              a["usable_kwh"], "kWh")


if __name__ == "__main__":
    main(sys.argv[1])
