"""Create the local environment needed by the web-evidence workflows."""

from __future__ import annotations

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
    if not (VENV / ("Scripts" if os.name == "nt" else "bin")).exists():
        run(sys.executable, "-m", "venv", str(VENV))

    executable_dir = VENV / ("Scripts" if os.name == "nt" else "bin")
    python = executable_dir / ("python.exe" if os.name == "nt" else "python")
    cli = executable_dir / ("nandatown.exe" if os.name == "nt" else "nandatown")
    run(str(python), "-m", "pip", "install", "-e", ".[dev,web-evidence]")
    run(str(python), "-m", "playwright", "install", "chromium")

    print("\nSetup complete. Set DEEPSEEK_API_KEY, then provide a local URL and question:")
    if os.name == "nt":
        print(f"  & '{cli}' web-evidence --url http://127.0.0.1:8765/page.html --question 'Your question'")
    else:
        print(f"  {shlex.quote(str(cli))} web-evidence --url http://127.0.0.1:8765/page.html --question 'Your question'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
