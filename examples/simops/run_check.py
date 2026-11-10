"""Play every SimOps case through the Lab, cross-check Town's record against SimOps's own log, and save it.

    SIMOPS_URL=https://<simops host> python examples/simops/run_check.py           # every case, live
    SIMOPS_URL=... python examples/simops/run_check.py --only clean,duplicate      # some of them
    python examples/simops/run_check.py --verify examples/simops/evidence          # offline, no SimOps

A live run plays each scenario against a fresh SimOps run (the playground opens one per case), then checks:

  expected   the verdict and the stages this case is about, written down here before the run
  cross      every SimOps call the desk recorded is in SimOps's own log, in the same order with the same
             outcome; calls SimOps refused at the door (no valid seat token) are not in its log; and the
             audit digest Town recorded is the one SimOps reports for that run

and writes evidence/<case>/: the Town bundle (town/), SimOps's log for the run without result bodies
(simops_log.json) and the check (check.json), plus evidence/summary.json and evidence/SUMMARY.md.

--verify needs no SimOps and no network. It loads the plugin (so the custom validator is registered; plain
`nandatown verify` does not load plugin files), runs Town's verify_bundle on every bundle, and re-runs the
cross-check from the saved files.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

CASES = [
    {"id": "clean", "yaml": "simops_week9.yaml", "kind": "baseline",
     "tests": "The whole week through Town: discover, brief, propose, refusal, swap, accept, sign, close, audit.",
     "expect": {"verdict": "passed", "stages": {"referee_discovered": "passed", "briefs_private": "passed",
                "rules_enforced": "passed", "swap_signed_by_all": "passed", "week_committed": "passed",
                "simops_verified": "passed", "privacy": "passed"}}},
    {"id": "duplicate", "yaml": "simops_week9_duplicate.yaml", "kind": "fault",
     "tests": "At-least-once delivery: accept_swap and both commit_week calls arrive twice.",
     "expect": {"verdict": "passed", "stages": {"applied_once": "passed", "week_committed": "passed",
                "simops_verified": "passed"}, "repeated_calls_min": 3}},
    {"id": "late_accept", "yaml": "simops_week9_late.yaml", "kind": "boundary",
     "tests": "A slow agent: the swap offer reaches Mechanical after the week closed.",
     "expect": {"verdict": "failed", "stages": {"stale_calls_refused": "passed", "week_committed": "failed",
                "simops_verified": "passed"}, "unscheduled": ["ELE-roughin"]}},
    {"id": "forged", "yaml": "simops_week9_forged.yaml", "kind": "fault",
     "tests": "A rogue sends calls as Electrical with its own key; Town's hmac auth is on.",
     "expect": {"verdict": "passed", "stages": {"forged_seat_refused": "passed", "week_committed": "passed"},
                "forged_stopped_by": "town"}},
    {"id": "forged_weak_auth", "yaml": "simops_week9_forged_weak_auth.yaml", "kind": "boundary",
     "tests": "The same rogue with Town's auth swapped for plain.v1: the forged calls reach SimOps.",
     "expect": {"verdict": "passed", "stages": {"forged_seat_refused": "passed", "week_committed": "passed"},
                "forged_stopped_by": "simops"}},
    {"id": "trusting_desk", "yaml": "simops_week9_trusting_desk.yaml", "kind": "control",
     "tests": "Fails on purpose: weak auth and a desk that picks the token by sender name.",
     "expect": {"verdict": "failed", "stages": {"forged_seat_refused": "failed"},
                "placed": {"ELE-roughin": [43, 45]}}},
    {"id": "simops_down", "yaml": "simops_week9.yaml", "kind": "boundary", "url": "http://127.0.0.1:9",
     "tests": "SimOps unreachable: nothing may be credited that was not checked.",
     "expect": {"verdict": "incomplete", "stages": {"referee_discovered": "passed",
                "week_committed": "not_enough_evidence", "simops_verified": "not_enough_evidence"}}},
    {"id": "chain_C4", "yaml": "simops_chain.yaml", "kind": "breadth",
     "tests": "SimOps case C4: a predecessor chain across the yard and Hall 2.",
     "expect": {"verdict": "passed", "stages": {"rules_enforced": "passed", "week_committed": "passed",
                "simops_verified": "passed"}}},
    {"id": "crane_C3", "yaml": "simops_crane.yaml", "kind": "breadth",
     "tests": "SimOps case C3: two trades want the crane, and steel's crew limit blocks its first slot.",
     "expect": {"verdict": "passed", "stages": {"rules_enforced": "passed", "week_committed": "passed",
                "simops_verified": "passed"}}},
    {"id": "live_seat", "yaml": "simops_week9_live_seat.yaml", "kind": "optional", "needs": "OPENAI_API_KEY",
     "tests": "Electrical decided by an OpenAI model (gpt-6-luna) through the same desk; a mutable dependency.",
     "expect": {"verdict": "passed", "stages": {"referee_discovered": "passed", "week_committed": "passed",
                "simops_verified": "passed"}}},
]
DOOR = ("unauthorized", "referee_unreachable")  # refused before reaching a run: never in SimOps's log


# -- running --------------------------------------------------------------------------------------------


def town_commit():
    try:
        sha = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True,
                             check=True).stdout.strip()
        base = subprocess.run(["git", "-C", REPO, "merge-base", "HEAD", "upstream/main"], capture_output=True,
                              text=True).stdout.strip()
        return {"head": sha, "base": base or None}
    except (OSError, subprocess.CalledProcessError):
        return {"head": None, "base": None}


def _relative_plugins():
    """Record plugin files relative to the repo, not as this machine's absolute paths."""
    from nandatown.sim import runner

    original = runner.load_scenario_file

    def load(path):
        spec = original(path)
        spec.plugin_files = [os.path.relpath(p, REPO) for p in spec.plugin_files]
        return spec

    runner.load_scenario_file = load


def play(case, out):
    from nandatown.sim.runner import run_lab

    import simops_link

    old = os.environ.get("SIMOPS_URL")
    if case.get("url"):
        os.environ["SIMOPS_URL"] = case["url"]
    try:
        bundle, result = run_lab(os.path.join("examples", "simops", case["yaml"]), out)
    finally:
        if case.get("url"):
            os.environ.pop("SIMOPS_URL") if old is None else os.environ.__setitem__("SIMOPS_URL", old)
    site = simops_link.SITES.get(result.run_id)
    return bundle, result, site


def load_events(bundle):
    with open(os.path.join(bundle, "events.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# -- checking -------------------------------------------------------------------------------------------


def observe(events, result):
    """The facts a case's expectation is about, read from Town's own record."""
    calls = [e for e in events if e["kind"] == "simops_call"]
    forged = {e["subject"] for e in events if e["kind"] == "forged_message"}
    bounced = {e["subject"] for e in events if e["kind"] == "delivery_failed" and e["subject"] in forged}
    reached = [e for e in calls if e["subject"] in forged]
    dup = {e["subject"] for e in events if e["kind"] == "message_duplicated"
           and str(e["detail"].get("kind", "")).startswith("call.")}
    repeated = sum(1 for i, e in enumerate(calls) if e["subject"] in dup
                   and any(x["subject"] == e["subject"] for x in calls[:i]))
    model = next((e["detail"] for e in reversed(events) if e["kind"] == "model_turn"), None)
    board = next((e["detail"] for e in reversed(events) if e["kind"] == "week_board"), None)
    audit = next((e["detail"] for e in reversed(events) if e["kind"] == "simops_verified"), None)
    return {
        "verdict": result["verdict"],
        "stages": {s["name"]: s["status"] for s in result["stages"]},
        "simops_calls": len(calls),
        "refused": sorted({e["detail"].get("reason") or e["detail"].get("status") for e in calls
                           if not e["detail"].get("ok")} - {None}),
        "repeated_calls": repeated,
        "forged": {"sent": len(forged), "stopped_by_town": len(bounced),
                   "reached_simops": len(reached),
                   "refused_by_simops": sum(1 for e in reached if not e["detail"].get("ok"))},
        "model": model,
        "unscheduled": board["unscheduled"] if board else None,
        "board": board["board"] if board else None,
        "simops_audit": None if audit is None else {k: audit.get(k) for k in
                                                    ("ok", "calls", "invariants", "log_digest", "state_hash",
                                                     "simops")},
    }


def meets(expect, seen):
    """[(what, expected, observed, ok)] for each thing the case expects."""
    rows = [("verdict", expect["verdict"], seen["verdict"], seen["verdict"] == expect["verdict"])]
    for name, want in expect.get("stages", {}).items():
        got = seen["stages"].get(name)
        rows.append((f"stage {name}", want, got, got == want))
    if "repeated_calls_min" in expect:
        rows.append(("repeated deliveries", f">= {expect['repeated_calls_min']}", seen["repeated_calls"],
                     seen["repeated_calls"] >= expect["repeated_calls_min"]))
    if "unscheduled" in expect:
        rows.append(("unscheduled at the end", expect["unscheduled"], seen["unscheduled"],
                     seen["unscheduled"] == expect["unscheduled"]))
    if "forged_stopped_by" in expect:
        f = seen["forged"]
        who = ("town" if f["sent"] and f["stopped_by_town"] == f["sent"] else
               "simops" if f["sent"] and f["reached_simops"] == f["sent"] == f["refused_by_simops"] else "mixed")
        rows.append(("forged calls stopped by", expect["forged_stopped_by"], who, who == expect["forged_stopped_by"]))
    for task, days in expect.get("placed", {}).items():
        got = (seen["board"] or {}).get(task, {}).get("days")
        rows.append((f"{task} placed on days", days, got, got == days))
    return [{"what": w, "expected": e, "observed": o, "ok": ok} for w, e, o, ok in rows]


def cross_check(events, simops_log):
    """Town's record of SimOps calls against SimOps's own log of the same run."""
    if simops_log is None:
        return {"ok": None, "note": "no SimOps run: nothing to cross-check"}
    calls = [e["detail"] for e in events if e["kind"] == "simops_call"]
    reached = [(d["from"], d["tool"], d["status"]) for d in calls if d.get("reason") not in DOOR]
    door = [d for d in calls if d.get("reason") in DOOR]
    logged = [(e["actor"], e["tool"], e["status"]) for e in simops_log["events"]
              if e["actor"] not in ("engine", "gc")]
    engine = sorted({e["tool"] for e in simops_log["events"] if e["actor"] in ("engine", "gc")})
    audit = next((e["detail"] for e in reversed(events) if e["kind"] == "simops_verified"), {})
    problems = []
    if reached != logged:
        first = next((i for i, (a, b) in enumerate(zip(reached, logged)) if a != b), min(len(reached), len(logged)))
        problems.append(f"call {first}: Town recorded {reached[first:first + 1]}, SimOps logged "
                        f"{logged[first:first + 1]} ({len(reached)} vs {len(logged)} calls)")
    if audit.get("log_digest") != simops_log.get("log_digest"):
        problems.append("the log digest Town recorded is not the one SimOps reports")
    if audit.get("state_hash") != simops_log.get("state_hash"):
        problems.append("the state hash Town recorded is not the one SimOps reports")
    if not set(engine) <= {"start", "freeze"}:
        problems.append(f"unexpected engine events in SimOps's log: {engine}")
    return {"ok": not problems, "problems": problems, "calls_matched": len(logged),
            "refused_at_the_door": len(door), "simops_engine_events": engine,
            "log_digest": simops_log.get("log_digest")}


# -- writing --------------------------------------------------------------------------------------------


def save(case, bundle, result, site, out, versions):
    folder = os.path.join(out, case["id"])
    shutil.rmtree(folder, ignore_errors=True)
    shutil.copytree(bundle, os.path.join(folder, "town"))
    report = os.path.join(folder, "town", "report.md")
    if os.path.exists(report):  # not a hashed record; Town's renderer leaves trailing spaces on empty cells
        with open(report, encoding="utf-8") as f:
            text = "\n".join(line.rstrip() for line in f.read().splitlines()) + "\n"
        with open(report, "w", encoding="utf-8") as f:
            f.write(text)
    events = load_events(bundle)
    simops_log = None
    if site is not None and site.audit is not None:
        a = site.audit
        simops_log = {k: a[k] for k in ("run", "case", "simops", "week", "frozen", "board_version", "unscheduled",
                                        "commits", "state_hash", "log_digest", "verify", "events")}
        with open(os.path.join(folder, "simops_log.json"), "w", encoding="utf-8") as f:
            json.dump(simops_log, f, indent=1, sort_keys=True)
    seen = observe(events, json.loads(json.dumps(result.model_dump())))
    rows = meets(case["expect"], seen)
    cross = cross_check(events, simops_log)
    check = {"case": case["id"], "kind": case["kind"], "tests": case["tests"], "scenario": case["yaml"],
             "town_run": result.run_id, "simops_run": site.run if site else None,
             "versions": versions, "expected": case["expect"], "observed": seen, "rows": rows,
             "as_expected": all(r["ok"] for r in rows), "cross_check": cross}
    with open(os.path.join(folder, "check.json"), "w", encoding="utf-8") as f:
        json.dump(check, f, indent=1, sort_keys=True)
    return check


def versions_of(sites):
    import nandatown
    from nandatown.sim.validators import LAB_EVALUATOR_VERSION

    simops = next((s.opened.get("simops") for s in sites if s is not None and s.ok), None)
    return {"nandatown": nandatown.__version__, "evaluator": LAB_EVALUATOR_VERSION, "town_commit": town_commit(),
            "python": sys.version.split()[0], "simops": simops}


def summarize(out, checks, versions):
    summary = {"versions": versions, "cases": [{k: c[k] for k in ("case", "kind", "tests", "as_expected",
                                                                   "town_run", "simops_run")}
                                               | {"verdict": c["observed"]["verdict"],
                                                  "cross_check": c["cross_check"]["ok"]} for c in checks],
               "all_as_expected": all(c["as_expected"] for c in checks),
               "cross_checks_ok": all(c["cross_check"]["ok"] in (True, None) for c in checks)}
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    s = versions.get("simops") or {}
    lines = ["# SimOps in Nanda Town: recorded evidence", "",
             f"Town {str(versions['town_commit'].get('base') or versions['town_commit'].get('head'))[:7]} · "
             f"nandatown {versions['nandatown']} · evaluator {versions['evaluator']} · "
             f"SimOps {s.get('version')} ({s.get('build')})", "",
             "| Case | Kind | What it tests | Expected | Observed | As expected | Logs agree |",
             "|---|---|---|---|---|---|---|"]
    for c in checks:
        lines.append(f"| {c['case']} | {c['kind']} | {c['tests']} | {c['expected']['verdict']} | "
                     f"{c['observed']['verdict']} | {'yes' if c['as_expected'] else 'NO'} | "
                     f"{ {True: 'yes', False: 'NO', None: 'n/a'}[c['cross_check']['ok']] } |")
    lines += ["", "Each case folder has the Town bundle (`town/`), SimOps's own log of that run without result "
              "bodies (`simops_log.json`), and `check.json` with every expected and observed value."]
    with open(os.path.join(out, "SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return summary


# -- offline verification -------------------------------------------------------------------------------


def verify_evidence(folder):
    from nandatown.bundle import verify_bundle
    from nandatown.sim.runner import load_plugin_files

    load_plugin_files([os.path.join(HERE, "simops_plugin.py")])
    ok = True
    for name in sorted(os.listdir(folder)):
        case = os.path.join(folder, name)
        if not os.path.isdir(os.path.join(case, "town")):
            continue
        problems = verify_bundle(os.path.join(case, "town"))
        with open(os.path.join(case, "check.json"), encoding="utf-8") as f:
            check = json.load(f)
        log = None
        if os.path.exists(os.path.join(case, "simops_log.json")):
            with open(os.path.join(case, "simops_log.json"), encoding="utf-8") as f:
                log = json.load(f)
        events = load_events(os.path.join(case, "town"))
        cross = cross_check(events, log)
        with open(os.path.join(case, "town", "result.json"), encoding="utf-8") as f:
            seen = observe(events, json.load(f))
        spec = next(c for c in CASES if c["id"] == check["case"])
        rows = meets(spec["expect"], seen)
        good = not problems and cross["ok"] in (True, None) and all(r["ok"] for r in rows)
        ok &= good
        print(f"{name:18} bundle {'verified' if not problems else 'PROBLEMS'}  "
              f"logs {'agree' if cross['ok'] else 'n/a' if cross['ok'] is None else 'DISAGREE'}  "
              f"verdict {seen['verdict']:10} {'as expected' if all(r['ok'] for r in rows) else 'NOT AS EXPECTED'}")
        for p in problems + cross.get("problems", []):
            print(f"  problem: {p}")
    print("all evidence verified" if ok else "SOME EVIDENCE FAILED")
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--only", default="", help="comma-separated case ids")
    p.add_argument("--out", default=os.path.join(HERE, "evidence"), help="evidence folder")
    p.add_argument("--verify", metavar="DIR", help="verify recorded evidence offline and exit")
    a = p.parse_args(argv)
    if a.verify:
        return verify_evidence(a.verify)
    if not os.environ.get("SIMOPS_URL"):
        sys.exit("Set SIMOPS_URL to the SimOps server (see README.md).")
    os.chdir(REPO)
    os.environ.setdefault("NANDATOWN_HOME", tempfile.mkdtemp(prefix="town-home-"))
    _relative_plugins()
    pick = [c for c in CASES if not a.only or c["id"] in a.only.split(",")]
    for c in [c for c in pick if c.get("needs") and not os.environ.get(c["needs"])]:
        print(f"{c['id']:18} skipped: needs {c['needs']}")
    pick = [c for c in pick if not c.get("needs") or os.environ.get(c["needs"])]
    os.makedirs(a.out, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="simops-town-")
    played = []
    for case in pick:
        bundle, result, site = play(case, scratch)
        played.append((case, bundle, result, site))
        print(f"{case['id']:18} {result.verdict:10} town {result.run_id}  simops {site.run if site and site.ok else '-'}")
    versions = versions_of([s for *_, s in played])
    checks = [save(case, bundle, result, site, a.out, versions) for case, bundle, result, site in played]
    for c in checks:
        print(f"{c['case']:18} {'as expected' if c['as_expected'] else 'NOT AS EXPECTED'}  "
              f"logs {'agree' if c['cross_check']['ok'] else 'n/a' if c['cross_check']['ok'] is None else 'DISAGREE'}")
        for r in c["rows"]:
            if not r["ok"]:
                print(f"  {r['what']}: expected {r['expected']}, observed {r['observed']}")
        for problem in c["cross_check"].get("problems", []):
            print(f"  cross-check: {problem}")
    s = summarize(a.out, checks, versions)
    print(f"\nevidence in {os.path.relpath(a.out)}: {'every case as expected' if s['all_as_expected'] else 'SOME CASES DIFFER'}"
          f", {'logs agree' if s['cross_checks_ok'] else 'LOGS DISAGREE'}")
    return 0 if s["all_as_expected"] and s["cross_checks_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
