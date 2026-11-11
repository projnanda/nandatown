# SPDX-License-Identifier: Apache-2.0
"""Capture Leal's answers from a live Leal so offline runs replay exactly what Leal said.

It captures the recipe (``get_recipe``), Leal's public key (``leal_public_key``) and
Leal's decision and signed attestation (``check_application``) for each application the
scenarios and tests send: the clean packet and one with an invented number.

Example::

    NANDATOWN_LEAL_ROOT=../leal python integrations/leal/tools/capture_fixture.py
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent
# (packet in fixtures/packets.json, scenario whose employer it is sent to)
CHECKED = [("clean", "recipe_application.yaml"), ("invented_number", "recipe_application.yaml")]


def _employer_facts(scenario: str) -> dict:
    spec = yaml.safe_load((HERE / "scenarios" / scenario).read_text(encoding="utf-8"))
    config = next(a["config"] for a in spec["agents"] if a["role"] == "employer")
    return {k: config[k] for k in ("employer", "role", "posting_text")}


def _source_module():
    spec = importlib.util.spec_from_file_location("nandatown_leal_source", HERE / "leal_source.py")
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError("cannot load leal_source.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    root = Path(os.environ["NANDATOWN_LEAL_ROOT"]).resolve()
    mod = _source_module()
    source = mod.LiveSource(root)
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    public_key = source.public_key()
    checks = {}
    for parts, scenario in CHECKED:
        packet = mod.build_application(parts, _employer_facts(scenario))
        response = source.check_application(mod.PROFILE_ID, packet)
        attestation = response["attestation"]
        # Town's hash must equal Leal's, or employers could never match the attestation.
        assert attestation["packet_sha256"] == mod.packet_hash(packet), "hash mismatch"
        assert attestation["public_key"] == public_key, "attestation signed by another key"
        checks[mod._check_key(mod.PROFILE_ID, packet)] = {
            "packet": parts,
            "employer": packet["employer"],
            "response": response,
        }
        print(f"{parts}: Leal says {response['decision']['outcome']}")
    data = {
        "captured": datetime.date.today().isoformat(),
        "leal_commit": commit,
        "public_key": public_key,
        "recipes": {"job-applications": source.get_recipe("job-applications")},
        "checks": checks,
    }
    out = HERE / "fixtures" / "recipe.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} from Leal {commit}")


if __name__ == "__main__":
    main()
