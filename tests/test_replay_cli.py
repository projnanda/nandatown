"""`nandatown replay` steps through a bundle's events, whatever its mode.

It is a viewer: it neither re-evaluates nor changes the bundle, so after
replaying one, `nandatown verify` still reaches the same conclusion.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from test_path import start_reference_agent

from nandatown.runner import run_town
from nandatown.sim.runner import run_lab

SOURCE = Path(__file__).resolve().parents[1] / "src"


def town(*args, home):
    """Run the nandatown CLI in its own process, as an operator would."""
    environment = dict(os.environ, NANDATOWN_HOME=str(home),
                       PYTHONPATH=str(SOURCE), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-m", "nandatown.cli", *args],
                          env=environment, capture_output=True, text=True,
                          timeout=120)


def digests(bundle_dir):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(bundle_dir).iterdir()) if p.is_file()}


def test_a_path_bundle_replays_through_the_cli(tmp_path):
    home = tmp_path / "home"
    agent, url = start_reference_agent()
    try:
        recorded = town("test-agent", "--url", url,
                        "--out", str(tmp_path / "runs"), home=home)
    finally:
        agent.terminate()
        agent.wait()
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    bundle_dir = next(p for p in (tmp_path / "runs").iterdir() if p.is_dir())
    run = json.loads((bundle_dir / "run.json").read_text())
    events = (bundle_dir / "events.jsonl").read_text().splitlines()
    before = digests(bundle_dir)

    replay = town("replay", str(bundle_dir), home=home)

    assert replay.returncode == 0, replay.stderr
    lines = replay.stdout.rstrip("\n").splitlines()
    assert lines[0] == (f"Replay of {run['run_id']}"
                        f" ({run['profile_name']}, {len(events)} events)")
    assert run["profile_name"].startswith("a2a-capability-fulfillment@")
    assert len([line for line in lines if line.startswith("t=")]) \
        == len(events)
    assert lines[-1] == "Verdict: PASSED"

    cards = town("replay", str(bundle_dir), "--kind", "card_retrieved",
                 "--limit", "1", home=home)
    assert cards.returncode == 0, cards.stderr
    shown = [line for line in cards.stdout.splitlines()
             if line.startswith("t=")]
    assert len(shown) == 1 and " card_retrieved " in shown[0]

    assert digests(bundle_dir) == before
    verified = town("verify", str(bundle_dir), home=home)
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_track_and_lab_replays_still_name_their_profile(tmp_path):
    home = tmp_path / "home"
    track_dir, _ = run_town("quote-clean", str(tmp_path / "track"))
    lab_dir, _ = run_lab("voting", str(tmp_path / "lab"))

    for bundle_dir, name in [(track_dir, "quote-clean"), (lab_dir, "voting")]:
        replay = town("replay", str(bundle_dir), home=home)
        assert replay.returncode == 0, replay.stderr
        assert replay.stdout.splitlines()[0].startswith(
            f"Replay of {Path(bundle_dir).name} ({name}, ")
