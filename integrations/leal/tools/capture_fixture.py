# SPDX-License-Identifier: Apache-2.0
"""Capture the recipe from a live Leal so offline runs replay exactly what Leal served.

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

HERE = Path(__file__).resolve().parent.parent


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
    source = _source_module().LiveSource(root)
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    data = {
        "captured": datetime.date.today().isoformat(),
        "leal_commit": commit,
        "recipes": {"job-applications": source.get_recipe("job-applications")},
    }
    out = HERE / "fixtures" / "recipe.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} from Leal {commit}")


if __name__ == "__main__":
    main()
