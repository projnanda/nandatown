"""Check the artifacts, not the checkout, for runtime and shipped-test data.

Usage: python scripts/check_distributions.py dist
"""

from pathlib import Path
import sys
import tarfile
import zipfile


def shippable(name: str) -> bool:
    """Exclude local state, secrets and model weights from archive contracts."""
    path = Path(name)
    forbidden = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv",
                 "node_modules", ".next", "models", "runs"}
    return (not forbidden.intersection(path.parts)
            and path.suffix not in {".pyc", ".pyo", ".safetensors", ".ckpt"}
            and not (path.name == ".env" or
                     (path.name.startswith(".env.") and path.name != ".env.example")))


def reject_private(names) -> None:
    forbidden = sorted(name for name in names if not shippable(name))
    if forbidden:
        raise SystemExit(f"distribution contains forbidden members: {forbidden}")


def check(directory: Path) -> None:
    wheels = list(directory.glob("*.whl"))
    sources = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise SystemExit("expected exactly one wheel and one sdist")
    root = Path(__file__).resolve().parents[1]
    runtime = {
        path.relative_to(root / "src").as_posix()
        for pattern in ("src/nandatown/sim/scenarios/*.yaml",
                        "src/nandatown/skills/builtin/*.md",
                        "src/nandatown/_vendor/promptarmor/*.py",
                        "src/nandatown/_vendor/promptarmor/LICENSE",
                        "src/nandatown/_vendor/promptarmor/SOURCE.json")
        for path in root.glob(pattern)
    }
    test_data = {
        path.relative_to(root).as_posix()
        for pattern in ("tests/fixtures/**/*", "schemas/*.json", "examples/*.py",
                        "scripts/setup_web_evidence.py", "scripts/run_offline_checks.py",
                        "scripts/download_piguard.py", "scripts/check_piguard.py",
                        "scripts/validate_piguard_experiment.py", "docs/contracts/*.json",
                        "docs/web-evidence.md")
        for path in root.glob(pattern) if path.is_file() and shippable(path.relative_to(root).as_posix())
    }
    with zipfile.ZipFile(wheels[0]) as wheel:
        reject_private(wheel.namelist())
        missing = runtime - set(wheel.namelist())
        if missing:
            raise SystemExit(f"wheel missing runtime data: {sorted(missing)}")
    with tarfile.open(sources[0]) as source:
        names = {name.partition("/")[2] for name in source.getnames()}
        reject_private(names)
        expected = test_data | {f"src/{name}" for name in runtime}
        missing = expected - names
        if missing:
            raise SystemExit(f"sdist missing data: {sorted(missing)}")
    print(f"Distribution data verified: {len(runtime)} runtime files,"
          f" {len(test_data)} fixture/schema files")


if __name__ == "__main__":
    check(Path(sys.argv[1] if len(sys.argv) > 1 else "dist"))
