"""Check the experiment matrix without starting browsers or model calls."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace


def test_runner_executes_each_variant_and_defense_once(tmp_path, monkeypatch):
    script = Path(__file__).parents[1] / "scripts/run_atlas_variants.py"
    spec = importlib.util.spec_from_file_location("atlas_variants", script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    calls = []

    class Server:
        server_port = 12345

        def __init__(self, *args):
            pass

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(runner, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd: calls.append(cmd) or SimpleNamespace(returncode=0))
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    model = tmp_path / "model"
    model.mkdir()
    monkeypatch.setattr(runner.sys, "argv", [str(script), "--out", str(tmp_path / "results"), "--model-dir", str(model)])
    assert runner.main() == 0
    assert len(calls) == 9
    observed = {(c[c.index("--url") + 1].split("/")[-2], c[c.index("--defense") + 1]) for c in calls}
    assert observed == {(v, d) for v in ("clean", "malicious", "malicious_v2")
                        for d in ("baseline", "llm", "piguard")}
