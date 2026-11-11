#!/usr/bin/env python3
"""Reproduce the ActionLedger compatibility check against Town's Track.

    ACTIONLEDGER_HOME=/path/to/actionledger \\
        python examples/actionledger/run_check.py [--cases a,b] [--record]

For every case this starts a fresh ActionLedger and mock tracker from
ACTIONLEDGER_HOME (separate processes, fresh data directory, free ports),
arms a tracker fault if the case has one, runs a Town Track profile with
``actionledger_seller.py`` as the seller (``cmd:`` harness) and Town's stock
buyer, and then checks four things independently of each other:

  town     Town's own verdict and stages (result.json)
  bundle   ``verify_bundle`` on the evidence bundle Town wrote
  tracker  the tracker's SQLite file, read directly: holds for this run
  ledger   ActionLedger's SQLite file: the action row and its final state

The tracker is the ground truth for the external side effect; neither
Town nor the seller's own notes are trusted for the hold count.

--record copies each bundle's canonical files into evidence/<case>/ and
writes evidence/summary.json. Exit status is 0 only if every check holds.
ACTIONLEDGER_PYTHON selects the interpreter for the services (default:
ACTIONLEDGER_HOME/.venv/bin/python, else this interpreter).
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import platform
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from nandatown import __version__ as TOWN_VERSION
from nandatown.bundle import RECORD_FILES, verify_bundle

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SELLER = HERE / "actionledger_seller.py"
EVIDENCE = HERE / "evidence"
AGENT_KEY = os.environ.get("AGENT_KEY", "local-demo-agent")
TRACKER_KEY = os.environ.get("TRACKER_KEY", "local-demo-tracker")
REQUEST_ID = "q-1"  # the stock buyer's quote request identity
BUNDLE_EXTRAS = ["manifest.json", "attestation.json"]


@dataclass
class Case:
    name: str
    profile: str
    path: str  # actionledger | direct
    what: str
    tracker_fault: dict[str, Any] | None = None
    verdict: str = "passed"
    holds: int = 1
    ledger_status: str | None = "CONFIRMED_SUCCESS"
    seller_ack: str = "processed"
    applications: int = 1  # times the seller called its writer
    stages: dict[str, str] = field(default_factory=dict)
    recover_after: bool = False  # restore lookup, then read /status


CASES = [
    Case("clean", "quote-clean", "actionledger",
         "baseline: no fault anywhere"),
    Case("town_lost_ack", "quote-lost-ack", "actionledger",
         "Town drops the seller's first processed ack (503); the client"
         " retries it", stages={"ack_retry_survived": "passed"}),
    Case("town_duplicate", "quote-duplicate-delivery", "actionledger",
         "Town re-offers the completed request; the seller's journal"
         " answers it without a second write",
         stages={"duplicate_recognized": "passed"}),
    Case("crash_restart", "quote-crash-restart", "actionledger",
         "seller stalls past its lease after claiming and exits; Town"
         " fences the stale attempt and redelivers to the restarted seller",
         stages={"stale_fence_rejected": "passed",
                 "recovered_after_restart": "passed"}),
    Case("amnesia", "quote-amnesia-restart", "actionledger",
         "seller places the hold, answers, loses its journal and dies before"
         " acking; the restarted seller applies the request again",
         applications=2, stages={"amnesia_survived": "passed"}),
    Case("amnesia_direct", "quote-amnesia-restart", "direct",
         "negative control: the same fault, the seller writes to the"
         " tracker itself with no ledger in front",
         holds=2, ledger_status=None, applications=2,
         stages={"amnesia_survived": "passed"}),
    Case("tracker_lost_ack", "quote-clean", "actionledger",
         "the tracker commits the hold and then answers 504; ActionLedger"
         " verifies by tag and payload hash",
         tracker_fault={"mode": "lost_ack"},
         ledger_status="RECOVERED_SUCCESS"),
    Case("amnesia_tracker_lost_ack", "quote-amnesia-restart", "actionledger",
         "both faults: tracker loses the ack on the first application, the"
         " seller then forgets everything and applies again",
         tracker_fault={"mode": "lost_ack"},
         ledger_status="RECOVERED_SUCCESS", applications=2,
         stages={"amnesia_survived": "passed"}),
    Case("lookup_down", "quote-clean", "actionledger",
         "boundary: the tracker commits, loses the ack, and lookup is down;"
         " the outcome cannot be verified, so the seller escalates",
         tracker_fault={"mode": "lost_ack", "break_lookup": True},
         verdict="incomplete", ledger_status="AMBIGUOUS", seller_ack="failed",
         stages={"response": "not_enough_evidence"}, recover_after=True),
]


# -- processes --------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_healthy(url: str, proc: subprocess.Popen, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{url} exited with {proc.returncode}")
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return
        time.sleep(0.1)
    raise RuntimeError(f"{url} did not become healthy")


@contextlib.contextmanager
def actionledger(home: Path, python: str, data: Path, log: Path):
    """Tracker and ledger as ActionLedger runs them, one worker each."""
    tracker_port, ledger_port = free_port(), free_port()
    tracker_url = f"http://127.0.0.1:{tracker_port}"
    env = {**os.environ, "DATA_DIR": str(data), "TRACKER_URL": tracker_url,
           "AGENT_KEY": AGENT_KEY, "TRACKER_KEY": TRACKER_KEY,
           "VISIBILITY_BOUND_SECONDS": "0", "PYTHONUNBUFFERED": "1"}
    procs = []
    with open(log, "w", encoding="utf-8") as out:
        try:
            for app, port in (("mock_tracker:app", tracker_port),
                              ("ledger:app", ledger_port)):
                procs.append(subprocess.Popen(
                    [python, "-m", "uvicorn", app, "--host", "127.0.0.1",
                     "--port", str(port), "--workers", "1"],
                    cwd=home, env=env, stdout=out, stderr=subprocess.STDOUT))
                wait_healthy(f"http://127.0.0.1:{port}", procs[-1])
            yield tracker_url, f"http://127.0.0.1:{ledger_port}"
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()


def run_town(case: Case, out: Path, env: dict[str, str]) -> tuple[Path, str]:
    seller = shlex.join([sys.executable, str(SELLER), "--path", case.path])
    cmd = [sys.executable, "-m", "nandatown.cli", "run", case.profile,
           "--agent", f"seller=cmd:{seller}", "--out", str(out)]
    done = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          timeout=180)
    bundles = glob.glob(str(out / "run-*" / "manifest.json"))
    if len(bundles) != 1:
        raise RuntimeError(f"expected one bundle, found {len(bundles)}:\n"
                           f"{done.stdout}\n{done.stderr}")
    return Path(bundles[0]).parent, done.stdout


# -- observations -----------------------------------------------------------

def rows(db: Path, sql: str, *args) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def observe(bundle: Path, data: Path) -> dict[str, Any]:
    result = json.loads((bundle / "result.json").read_text())
    run_id = result["run_id"]
    aid = f"town:{run_id}:{REQUEST_ID}"
    events = read_jsonl(bundle / "events.jsonl")
    seller_acks = [{"status": e["detail"]["status"],
                    "note": e["detail"].get("note", {}),
                    "attempt": e["detail"].get("attempt")}
                   for e in events if e["kind"] == "ack_recorded"
                   and e["observer"] == "seller"]
    responses = [e for e in events if e["kind"] == "message_accepted"
                 and e["detail"].get("kind") == "quote_response"]
    tasks = rows(data / "tracker.sqlite3",
                 "SELECT id, tag, payload FROM tasks ORDER BY id")
    holds = [{"resource_id": f"task_{t['id']}", "tag": t["tag"],
              "payload": json.loads(t["payload"])} for t in tasks]
    ledger = []
    if (data / "ledger.sqlite3").exists():
        ledger = [{"action_id": r["action_id"], "state": r["state"],
                   "result": json.loads(r["result"]) if r["result"] else None}
                  for r in rows(data / "ledger.sqlite3",
                                "SELECT * FROM actions ORDER BY rowid")]
    applications = []
    log = bundle / "state" / "seller" / "actionledger_seller.log"
    if log.exists():
        applications = [{k: v for k, v in a.items() if k != "at"}
                        for a in read_jsonl(log)]
    obs = {"run_id": run_id, "action_id": aid,
           "verdict": result["verdict"],
           "stages": {s["name"]: s["status"] for s in result["stages"]},
           "bundle_problems": verify_bundle(str(bundle)),
           "seller_acks": seller_acks,
           "quote_responses": len(responses),
           "seller_applications": applications,
           "tracker_holds": holds,
           "ledger_actions": ledger}
    return obs


def recover(tracker_url: str, ledger_url: str, aid: str) -> dict[str, Any]:
    """Boundary follow-up: lookup comes back; ask the ledger again."""
    httpx.post(f"{tracker_url}/faults", headers={"X-Tracker-Key": TRACKER_KEY},
               json={"mode": "none", "lookup_available": True}).raise_for_status()
    r = httpx.get(f"{ledger_url}/status/{aid}",
                  headers={"X-Agent-Key": AGENT_KEY}, timeout=30)
    r.raise_for_status()
    return r.json()


def checks(case: Case, obs: dict[str, Any]) -> list[dict[str, Any]]:
    out = []

    def check(name, expected, observed):
        out.append({"check": name, "expected": expected,
                    "observed": observed, "ok": expected == observed})

    check("town verdict", case.verdict, obs["verdict"])
    for stage, status in case.stages.items():
        check(f"town stage {stage}", status, obs["stages"].get(stage))
    check("bundle verifies", [], obs["bundle_problems"])
    mine = [h for h in obs["tracker_holds"] if h["tag"] == obs["action_id"]]
    check("tracker holds for this request", case.holds, len(mine))
    check("tracker holds in total", case.holds, len(obs["tracker_holds"]))
    # The ack that reports an application carries the writer's note; a
    # later duplicate ack from the journal does not.
    applied = [a for a in obs["seller_acks"] if "path" in a["note"]]
    final = applied[-1] if applied else {}
    check("seller acks reporting an application", 1, len(applied))
    check("status of that ack", case.seller_ack, final.get("status"))
    check("seller writer calls", case.applications,
          len(obs["seller_applications"]))
    if case.ledger_status:
        check("ledger status in that ack", case.ledger_status,
              final.get("note", {}).get("ledger_status"))
        check("ledger action rows", 1, len(obs["ledger_actions"]))
        commits = [l for l in obs["service_log"]
                   if l.startswith("TRACKER: committed")]
        check("tracker commits seen by the service", 1, len(commits))
    if case.seller_ack == "processed":
        check("quote responses accepted by Town", 1, obs["quote_responses"])
        # Every application the seller made named the same, existing hold.
        named = sorted({a.get("hold") for a in obs["seller_applications"]})
        if case.path == "actionledger":
            check("every application names the one tracker hold",
                  [h["resource_id"] for h in mine], named)
    else:
        check("quote responses accepted by Town", 0, obs["quote_responses"])
    if case.recover_after:
        rec = obs.get("recovered", {})
        check("status after lookup returns", "RECOVERED_SUCCESS",
              rec.get("status"))
        check("recovered hold is the committed one",
              [h["resource_id"] for h in mine], [rec.get("resource_id")])
    return out


# -- versions ---------------------------------------------------------------

def git_version(path: Path) -> dict[str, Any]:
    def git(*args):
        return subprocess.run(["git", "-C", str(path), *args],
                              capture_output=True, text=True).stdout.strip()
    return {"commit": git("rev-parse", "HEAD") or None,
            "dirty": bool(git("status", "--porcelain",
                              "--untracked-files=no"))}


def versions(home: Path, python: str) -> dict[str, Any]:
    al_py = subprocess.run([python, "--version"], capture_output=True,
                           text=True).stdout.strip()
    return {"nandatown": {"version": TOWN_VERSION, **git_version(REPO)},
            "actionledger": {"repository":
                             "https://github.com/Cecilia1220/actionledger",
                             **git_version(home), "python": al_py},
            "python": platform.python_version(),
            "platform": platform.platform(terse=True)}


# -- main -------------------------------------------------------------------

def run_case(case: Case, home: Path, python: str, work: Path
             ) -> tuple[dict[str, Any], Path]:
    data, out = work / "data", work / "town"
    data.mkdir(parents=True)
    with actionledger(home, python, data, work / "services.log") as (
            tracker_url, ledger_url):
        if case.tracker_fault:
            httpx.post(f"{tracker_url}/faults",
                       headers={"X-Tracker-Key": TRACKER_KEY},
                       json=case.tracker_fault).raise_for_status()
        env = {**os.environ, "LEDGER_URL": ledger_url,
               "TRACKER_URL": tracker_url, "AGENT_KEY": AGENT_KEY,
               "TRACKER_KEY": TRACKER_KEY,
               "NANDATOWN_HOME": str(work / "town-home")}
        bundle, report = run_town(case, out, env)
        obs = observe(bundle, data)
        if case.recover_after:
            obs["recovered"] = recover(tracker_url, ledger_url,
                                       obs["action_id"])
            obs["ledger_actions_after_recovery"] = [
                {"action_id": r["action_id"], "state": r["state"]}
                for r in rows(data / "ledger.sqlite3",
                              "SELECT action_id, state FROM actions")]
    # The services' own lines: every tracker commit and ledger transition.
    obs["service_log"] = [
        line.strip() for line in
        (work / "services.log").read_text(encoding="utf-8").splitlines()
        if line.startswith(("LEDGER:", "TRACKER:"))]
    (work / "town-report.txt").write_text(report)
    return obs, bundle


def record(case: Case, bundle: Path) -> str:
    dest = EVIDENCE / case.name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in RECORD_FILES + BUNDLE_EXTRAS:
        if (bundle / name).exists():
            shutil.copy2(bundle / name, dest / name)
    return str(dest.relative_to(REPO))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cases", default="all",
                        help="comma-separated case names (default: all)")
    parser.add_argument("--record", action="store_true",
                        help="write evidence/<case>/ and evidence/summary.json")
    parser.add_argument("--keep", action="store_true",
                        help="keep the per-case working directories")
    args = parser.parse_args()

    home_env = os.environ.get("ACTIONLEDGER_HOME")
    if not home_env:
        print("set ACTIONLEDGER_HOME to an ActionLedger checkout",
              file=sys.stderr)
        return 2
    home = Path(home_env).resolve()
    if not (home / "ledger.py").exists():
        print(f"{home} is not an ActionLedger checkout", file=sys.stderr)
        return 2
    venv_python = home / ".venv" / "bin" / "python"
    python = os.environ.get("ACTIONLEDGER_PYTHON") or (
        str(venv_python) if venv_python.exists() else sys.executable)

    wanted = (None if args.cases == "all"
              else set(args.cases.split(",")))
    selected = [c for c in CASES if wanted is None or c.name in wanted]
    if wanted and wanted - {c.name for c in selected}:
        print(f"unknown cases: {sorted(wanted - {c.name for c in CASES})}",
              file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="actionledger-town-"))
    summary = {"check": "actionledger-track-compatibility",
               "versions": versions(home, python), "cases": []}
    print(f"Town {summary['versions']['nandatown']['commit'][:7]}"
          f" ({TOWN_VERSION}), ActionLedger"
          f" {(summary['versions']['actionledger']['commit'] or '?')[:7]}")
    failed = 0
    for case in selected:
        started = time.time()
        obs, bundle = run_case(case, home, python, root / case.name)
        results = checks(case, obs)
        bad = [c for c in results if not c["ok"]]
        failed += bool(bad)
        entry = {"case": case.name, "profile": case.profile,
                 "seller_path": case.path, "what": case.what,
                 "tracker_fault": case.tracker_fault,
                 "seconds": round(time.time() - started, 1),
                 "passed": not bad, "checks": results, "observed": obs}
        if args.record:
            entry["bundle"] = record(case, bundle)
        summary["cases"].append(entry)
        mine = len([h for h in obs["tracker_holds"]
                    if h["tag"] == obs["action_id"]])
        status = next((a["note"].get("ledger_status", "-")
                       for a in reversed(obs["seller_acks"])
                       if "path" in a["note"]), "-")
        print(f"{'ok  ' if not bad else 'FAIL'} {case.name:26} town="
              f"{obs['verdict']:7} holds={mine} ledger={status:17}"
              f" {entry['seconds']:5.1f}s")
        for c in bad:
            print(f"     {c['check']}: expected {c['expected']!r},"
                  f" observed {c['observed']!r}")
    if args.record:
        EVIDENCE.mkdir(exist_ok=True)
        with open(EVIDENCE / "summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, sort_keys=False)
            f.write("\n")
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print(f"working directories kept in {root}")
    total = sum(len(c["checks"]) for c in summary["cases"])
    print(f"{len(selected) - failed}/{len(selected)} cases, {total} checks;"
          f" {'all hold' if not failed else f'{failed} case(s) failed'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
