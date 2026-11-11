"""Owner step: sign the town mandates (Lab and Track) with ATL's owner key and register it in ATL_VAR_DIR.

Run once before the scenarios (the plugin never registers mandates: only the owner does):

    ATL_REPO=/path/to/atl ATL_VAR_DIR=/tmp/atl-town python integrations/atl-payments/setup_atl.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    repo = os.environ.get("ATL_REPO")
    if not repo or not os.environ.get("ATL_VAR_DIR"):
        print("set ATL_REPO (ATL checkout) and ATL_VAR_DIR (ATL state directory)", file=sys.stderr)
        return 2
    files = [Path(p) for p in sys.argv[1:]] or [HERE / "town-mandate.json", HERE / "track-mandate.json"]
    env = {**os.environ, "ATL_RAIL": "simulated", "ATL_ENV": "sandbox"}
    for path in files:
        mandate = json.loads(path.read_text(encoding="utf-8"))
        mandate.pop("town_category", None)  # plugin setting, not part of ATL's mandate
        mandate["valid_until"] = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(mandate, fh)
        cmd = ["uv", "run", "--directory", repo, "atl", "mandate", "register", fh.name]
        if subprocess.call(cmd, env=env) != 0:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
