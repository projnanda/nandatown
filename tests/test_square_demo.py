import importlib.util
import json
from pathlib import Path

DEMO = Path(__file__).resolve().parent.parent / "examples" / "town_square_demo.py"


def load_demo():
    spec = importlib.util.spec_from_file_location("town_square_demo", DEMO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_walkthrough_runs_every_built_step(tmp_path, capsys):
    out = tmp_path / "events.jsonl"
    assert load_demo().main(["--events", str(out)]) == 0
    printed = capsys.readouterr().out
    for n in range(1, 10):
        assert f"== Step {n}:" in printed
    assert "Agent A finds: muse-b, openclaw-c" in printed
    assert "REFUSED (no_relationship)" in printed
    assert "REFUSED (revoked)" in printed
    kinds = [json.loads(line)["kind"] for line in out.read_text().splitlines()]
    assert kinds.count("unauthorized_data_request") == 3
    assert "relationship_revoked" in kinds and "slot_agreed" in kinds
