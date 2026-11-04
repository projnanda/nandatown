"""Create the local environment needed by the web-evidence workflows."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"


def run(*parts: str) -> None:
    print("+", " ".join(shlex.quote(part) for part in parts), flush=True)
    subprocess.run(parts, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--piguard', action='store_true',
                        help='Install optional CPU PIGuard dependencies in this repository venv')
    args = parser.parse_args()
    if not (VENV / ("Scripts" if os.name == "nt" else "bin")).exists():
        run(sys.executable, "-m", "venv", str(VENV))

    executable_dir = VENV / ("Scripts" if os.name == "nt" else "bin")
    python = executable_dir / ("python.exe" if os.name == "nt" else "python")
    cli = executable_dir / ("nandatown.exe" if os.name == "nt" else "nandatown")
    if args.piguard:
        run(str(python), "-m", "pip", "install", "torch==2.4.1",
            "--index-url", "https://download.pytorch.org/whl/cpu")
    extra = ".[dev,web-evidence,piguard]" if args.piguard else ".[dev,web-evidence]"
    run(str(python), "-m", "pip", "install", "-e", extra)
    run(str(python), "-m", "playwright", "install", "chromium")

    print("\nSetup complete. All three workflows use OPENAI_API_KEY by default; baseline Agents use GPT-4.1 at temperature zero. Provide a local URL and question:")
    if os.name == "nt":
        print(f"  & '{cli}' web-evidence --url http://127.0.0.1:8765/page.html --question 'Your question'")
    else:
        print(f"  {shlex.quote(str(cli))} web-evidence --url http://127.0.0.1:8765/page.html --question 'Your question'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
