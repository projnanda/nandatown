"""Distribution checks must ignore caches and reject private/generated payloads."""
import importlib.util
from pathlib import Path
import tarfile
import zipfile
import io

import pytest


def checker(tmp_path):
    source = Path(__file__).parents[1] / "scripts/check_distributions.py"
    spec = importlib.util.spec_from_file_location("distribution_check", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    module.__file__ = str(root / "scripts/check_distributions.py")
    fixture = root / "tests/fixtures"
    (fixture / "__pycache__").mkdir(parents=True)
    (fixture / "small.json").write_text("{}")
    (fixture / "__pycache__/cached.pyc").write_bytes(b"local cache")
    return module


def archives(tmp_path, extra=None):
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "town.whl", "w") as archive:
        archive.writestr("nandatown/__init__.py", "")
        if extra:
            archive.writestr(extra, "test bytes")
    with tarfile.open(dist / "town.tar.gz", "w:gz") as archive:
        data = b"{}"
        info = tarfile.TarInfo("town/tests/fixtures/small.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return dist


def test_fixture_cache_is_not_required_in_distribution(tmp_path):
    checker(tmp_path).check(archives(tmp_path))


@pytest.mark.parametrize("extra", ["nandatown/__pycache__/x.pyc", ".env",
                                   "models/model.safetensors", "runs/events.jsonl"])
def test_private_and_generated_archive_members_are_rejected(tmp_path, extra):
    with pytest.raises(SystemExit, match="forbidden"):
        checker(tmp_path).check(archives(tmp_path, extra))
