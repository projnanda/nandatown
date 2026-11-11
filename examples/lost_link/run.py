"""Reproduce Lost Link compatibility in Town Lab, or verify saved evidence.

python examples/lost_link/run.py --project-root ../Lost-Link --out /tmp/lost-link-town
python examples/lost_link/run.py --verify examples/lost_link/evidence
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.sim.runner import load_plugin_files, run_lab

HERE = Path(__file__).resolve().parent
TOWN_ROOT = HERE.parents[1]
TOWN_BASE = "6f9a7c064486c4a0ae9242f1ff341b8a4aad0244"
PROJECT_COMMIT = "c26cddbfc9721c12acafa4603f70265c6f37bd11"
CASES = {"clean": "passed", "duplicate-and-misuse": "passed", "dropped-review": "incomplete"}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def git_sha(path):
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


@contextmanager
def service(project, temporary):
    """Start the separately checked-out service; never import its code into Town."""
    temporary = Path(temporary)
    credentials = {name: secrets.token_urlsafe(24) for name in ("finder", "owner", "operator")}
    credentials_file = temporary / "client-credentials.json"
    write_json(credentials_file, credentials)
    secret_file = temporary / "service-credentials.json"
    write_json(secret_file, {
        "agents": {credentials["finder"]: "finder", credentials["owner"]: "owner"},
        "humans": {credentials["operator"]: {"name": "scripted-holder", "acts_for": "finder"}}})
    db = temporary / "lostlink.db"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "LOSTLINK_DB": str(db), "LOSTLINK_SECRETS": str(secret_file)}
    previous = {k: os.environ.get(k) for k in ("LOSTLINK_URL", "LOSTLINK_TEST_CREDENTIALS", "NANDATOWN_HOME")}
    os.environ.update({"LOSTLINK_URL": f"http://127.0.0.1:{port}",
                       "LOSTLINK_TEST_CREDENTIALS": str(credentials_file),
                       "NANDATOWN_HOME": str(temporary / "town-home")})
    with (temporary / "service.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "lostlink.server:app",
                                    "--host", "127.0.0.1", "--port", str(port),
                                    "--no-access-log", "--log-level", "warning"],
                                   cwd=project, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError((temporary / "service.log").read_text()[-1500:])
                try:
                    if httpx.get(os.environ["LOSTLINK_URL"] + "/openapi.json", timeout=.5).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
            else:
                raise RuntimeError("Lost Link did not start within 15 seconds")
            yield db, credentials
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def snapshot(db):
    """Independent service-state check, outside the participant roles."""
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return {
            "reports": [dict(r) for r in conn.execute(
                "SELECT report_id,kind,owner_agent,status FROM reports ORDER BY kind")],
            "cases": [dict(r) for r in conn.execute(
                "SELECT case_id,status,pickup_instructions FROM cases")],
            "human_actions": [r[0] for r in conn.execute("SELECT action FROM human_actions ORDER BY rowid")],
            "audit": [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY rowid")],
        }


def run_suite(project, out):
    project, out = Path(project).resolve(), Path(out).resolve()
    if git_sha(project) != PROJECT_COMMIT:
        raise ValueError(f"Check out Lost Link {PROJECT_COMMIT} before running")
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    for name, expected in CASES.items():
        with tempfile.TemporaryDirectory(prefix="town-lost-link-") as td:
            with service(project, td) as (db, credentials):
                start = time.perf_counter()
                bundle_dir, result = run_lab(str(HERE / (name + ".yaml")), str(out / name))
                elapsed = time.perf_counter() - start
                state = snapshot(db)
                assert len(state["reports"]) == 2 and len(state["cases"]) == 1, state
                expected_status = "submitted" if name == "dropped-review" else "returned"
                assert state["cases"][0]["status"] == expected_status, state
                assert state["human_actions"] == ([] if name == "dropped-review" else
                                                    ["need_info", "approve", "returned"]), state
                if name == "dropped-review":
                    assert state["cases"][0]["pickup_instructions"] is None
                else:
                    assert all(r["status"] == "closed" for r in state["reports"])
                problems = verify_bundle(bundle_dir)
                assert not problems, problems
                assert result.verdict == expected, result
                # Tokens, private answers and keystore files never leave the temp dir.
                from nandatown.sim.agents import ROLES
                # Town does not install user-plugin modules in sys.modules.
                private = ROLES["lostlink.finder"].on_start.__globals__["PRIVATE_DETAIL"]
                exported = "".join(p.read_text() for p in Path(bundle_dir).iterdir() if p.is_file())
                assert private not in exported
                assert all(token not in exported for token in credentials.values())
                write_json(Path(bundle_dir).parent / "service_snapshot.json", state)
                events = load_bundle(bundle_dir)["events"]
                calls = [e for e in events if e.kind == "lostlink.http"]
                results[name] = {
                    "expected_verdict": expected, "actual_verdict": result.verdict,
                    "service_case_status": expected_status, "service_reports": len(state["reports"]),
                    "service_cases": len(state["cases"]), "http_calls": len(calls),
                    "elapsed_seconds": round(elapsed, 3), "model_calls": 0,
                    "bundle": str(Path(bundle_dir).relative_to(out)), "verification_problems": problems,
                    "stages": {s.name: s.status for s in result.stages},
                }
    summary = {
        "town_base_commit": TOWN_BASE, "town_checkout_commit": git_sha(TOWN_ROOT),
        "project_repository": "https://github.com/cathyliu0510/Lost-Link",
        "project_commit": PROJECT_COMMIT, "integration_contract": "lost-link-lifecycle@1",
        "adapter_sha256": hashlib.sha256((HERE / "adapter.py").read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "dependencies": {p: importlib.metadata.version(p) for p in
                         ("nandatown", "fastapi", "httpx", "pydantic", "uvicorn")},
        "results": results,
    }
    write_json(out / "summary.json", summary)
    return summary


def verify_saved(root):
    load_plugin_files([str(HERE / "adapter.py")])
    root = Path(root)
    summary = json.loads((root / "summary.json").read_text())
    for name, row in summary["results"].items():
        problems = verify_bundle(str(root / row["bundle"]))
        assert not problems, (name, problems)
        result = load_bundle(str(root / row["bundle"]))["result"]
        assert result.verdict == CASES[name] == row["actual_verdict"]
        print(f"{name}: {result.verdict}; bundle integrity and evaluator replay verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--out", type=Path, default=Path("/tmp/lost-link-town"))
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        verify_saved(args.verify)
    elif args.project_root:
        print(json.dumps(run_suite(args.project_root, args.out), indent=2))
    else:
        parser.error("supply --project-root for a live check, or --verify for offline replay")


if __name__ == "__main__":
    main()
