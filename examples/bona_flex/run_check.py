"""One command: run the VeriFlex (Bona) Lab scenarios live on fresh synthetic worlds, with a readable timeline.

    python examples/bona_flex/run_check.py --out <new dir> [--repeat 3] [--cases flex_clean,...]

Needs (live route): BONA_HOME (the Bona runtime checkout) and BONA_PYTHON (its Python 3.13); run with NandaTown's
Python. For every run: a NEW world (`$BONA_PYTHON -m bench.hw8.fresh_world`), the real Bona MCP server spawned by
the desk, Town's Lab engine with the scenario's seeded faults, Town's own bundle (+ attestation with a keystore
inside the world folder), and the hash-bound Bona sidecar beside it.

Timeline: lines marked LIVE are printed by the agents while the run executes; lines marked RENDERED are rebuilt
afterwards from the recorded events.jsonl only. The two are kept apart on purpose.

Determinism: Town's run_id is fixed per (scenario, seed) here (Town's runner draws a random one), so reruns can be
compared. Raw events.jsonl bytes differ between reruns only in the declared binding fields (receipt, request,
offer and sidecar-manifest hashes, which cover wall-clock-signed Bona objects); the check reports raw identity and
identity with exactly those fields masked, and lists every differing field.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
MASKED_FIELDS = ("receipt_sha256", "request_sha256", "manifest_sha256")
CASES = ("flex_clean", "flex_race_lost_response", "flex_race_naive_desk_control")


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop("PYTHONIOENCODING", None)
    return env


def fresh_world(dest: pathlib.Path) -> dict:
    env = _clean_env() | {"PYTHONPATH": os.environ["BONA_HOME"]}
    for k in ("BONA_PATHS_TOML", "BONA_KEY_STORE", "BONA_DB_PATH", "BONA_ENV"):
        env.pop(k, None)
    p = subprocess.run([os.environ["BONA_PYTHON"], "-m", "bench.hw8.fresh_world", "--out", str(dest)],
                       cwd=os.environ["BONA_HOME"], env=env, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise SystemExit(f"fresh_world failed:\n{p.stdout}\n{p.stderr}")
    return json.loads((dest / "world.json").read_text(encoding="utf-8"))


def provenance() -> dict:
    import importlib.metadata as md

    import nandatown
    from nandatown.sim.validators import LAB_EVALUATOR_VERSION

    def git(d, *a):
        return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, env=_clean_env(),
                              check=False).stdout.strip()
    town = HERE.parents[1]
    return {"town_sha": git(town, "rev-parse", "HEAD"), "nandatown_version": nandatown.__version__,
            "lab_evaluator_version": LAB_EVALUATOR_VERSION,
            "bona_sha": git(os.environ["BONA_HOME"], "rev-parse", "HEAD"),
            "bona_dirty": bool(git(os.environ["BONA_HOME"], "status", "--porcelain", "--", "bona", "bench")),
            "python_town": sys.version.split()[0], "os": platform.platform(),
            "cryptography": md.version("cryptography")}


def run_case(case: str, out: pathlib.Path, worlds: pathlib.Path, tag: str, prov: dict) -> dict:
    from nandatown import __version__
    from nandatown.bundle import attest_bundle, write_bundle
    from nandatown.identity_portable import Keystore
    from nandatown.records import RunRecord, TownEvent, fingerprint
    from nandatown.sim.agents import ROLES
    from nandatown.sim.api import TownAPI
    from nandatown.sim.engine import Engine
    from nandatown.sim.runner import resolve_spec
    from nandatown.sim.scenario import ScenarioSpec
    from nandatown.sim.validators import LAB_EVALUATOR_VERSION, evaluate_scenario

    case_dir = out / case if tag == "" else out / "reruns" / f"{case}{tag}"
    case_dir.mkdir(parents=True)
    world = worlds / f"{case}{tag}"
    info = fresh_world(world)
    os.environ.update({"BONA_FLEX_WORLD": str(world), "BONA_FLEX_SIDECAR": str(case_dir / "bona_sidecar"),
                       "BONA_FLEX_TIMELINE": str(case_dir / "timeline_live.txt"),
                       "BONA_FLEX_PROVENANCE": json.dumps(prov)})
    spec = resolve_spec(str(HERE / "scenarios" / f"{case}.yaml"))
    import yaml
    # record plugin_files as the YAML names them (relative), never this machine's absolute paths
    spec.plugin_files = list(yaml.safe_load((HERE / "scenarios" / f"{case}.yaml").read_text(encoding="utf-8"))["plugin_files"])
    run_id = f"bona-flex-{spec.name}-s{spec.seed}"
    print(f"\n=== {case}{tag}: world {world.name}, Bona did {info['bona_did'][:24]}..., run_id {run_id}")
    engine = Engine(spec, run_id=run_id)
    payments = engine.layers["payments"]
    for a in spec.agents:
        engine.add_agent(ROLES[a.role](a.name, dict(a.config), TownAPI(engine, a.name)))
        payments.open_account(a.name, a.config.get("balance_cents", 0))
    try:
        engine.run()
    finally:
        ROLES["flex_seller"].on_start.__globals__["Ctx"].reset()   # Town's plugin loader keeps no sys.modules entry
    privacy = engine.layers["privacy"]
    events = [TownEvent.model_validate(privacy.redact(e.model_dump())) for e in engine.events]
    intents = [privacy.redact(i) for i in engine.intents]
    public_spec = ScenarioSpec.model_validate(privacy.redact(spec.model_dump()))
    result = evaluate_scenario(public_spec, engine.run_id, events)
    rr = RunRecord(run_id=engine.run_id, profile_name=spec.name,
                   profile_fingerprint=fingerprint(public_spec.model_dump()), created_at=0.0,
                   participants=[{"name": a.name, "role": a.role} for a in spec.agents],
                   releases={"nandatown": __version__, "evaluator": LAB_EVALUATOR_VERSION,
                             "python": sys.version.split()[0]},
                   config={"mode": "lab", "seed": spec.seed, "layers": spec.layers, "logical_time": engine.now,
                           "rerun_command": f"python examples/bona_flex/run_check.py --cases {case}"})
    bundle = case_dir / "bundle"
    write_bundle(str(bundle), public_spec, rr, intents, events, result, mode="lab")
    attest_bundle(str(bundle), keystore=Keystore(str(world / "town_keystore")))
    stages = {s.name: s.status for s in result.stages}
    print(f"--- verdict {result.verdict}; stages {stages}")
    render_timeline(bundle / "events.jsonl", case_dir / "timeline_rendered.txt")
    return {"case": case, "tag": tag, "verdict": result.verdict, "stages": stages,
            "dir": case_dir.relative_to(out).as_posix()}


def _line(e: dict) -> str | None:
    d, k = e.get("detail") or {}, e["kind"]
    t = f"RENDERED t={e['at']:6.2f}  {e['observer']:<8}"
    if k == "message_dropped":
        return f"{t} FAULT: Town dropped {d.get('kind')} to {d.get('to')}"
    if k == "message_duplicated":
        return f"{t} FAULT: Town duplicated {d.get('kind')} to {d.get('to')}"
    if k == "flex_assessed":
        return f"{t} assessed {e['subject']} ({d.get('offer')}, {d.get('buyer')}): {d.get('status')}, feasible " \
               f"{(d.get('feasible_w') or 0) // 1000} kW  [{d.get('source')}]"
    if k == "flex_commit":
        return f"{t} {d.get('action')} {e['subject']} attempt {d.get('attempt')}: {d.get('outcome')} " \
               f"{(d.get('w_granted') or 0) // 1000} kW {d.get('flags') or ''} seq {d.get('seq')} " \
               f"verified={d.get('verified')}  [{d.get('source')}]"
    if k == "escrow_held":
        return f"{t} escrow held {e['subject']} {d.get('cents')} cents from {d.get('from')}"
    if k in ("escrow_released", "escrow_refunded"):
        return f"{t} {k.replace('_', ' ')} {e['subject']} {d.get('cents')} cents"
    if k == "flex_metered":
        return f"{t} meter: executed {d.get('executed_w')} W, pushed {d.get('pushed')} reading(s)"
    if k == "flex_delivery":
        return f"{t} delivery {e['subject']} attempt {d.get('attempt')}: {d.get('verdict')} " \
               f"{d.get('delivered_fraction_bp')} bp seq {d.get('seq')}  [{d.get('source')}]"
    if k == "flex_trust":
        return f"{t} trust {e['subject']} [{d.get('phase')}]: {d.get('deliveries_verified')} verified  " \
               f"[{d.get('source')}]"
    if k == "flex_capacity":
        return f"{t} capacity {e['subject']}: live {d.get('live_w')} W of assessed {d.get('assessed_w')} W"
    if k == "flex_clock":
        return f"{t} clock checks {d.get('checks')}"
    if k == "bona_sidecar_bound":
        return f"{t} sidecar bound ({d.get('files')} files)"
    return None


def render_timeline(events_path: pathlib.Path, dest: pathlib.Path) -> None:
    lines = [ln for ln in (_line(json.loads(x)) for x in events_path.read_text(encoding="utf-8").splitlines()
                           if x.strip()) if ln]
    dest.write_bytes(("\n".join(lines) + "\n").encode())
    print("\n".join(lines))


def _mask(o):
    if isinstance(o, dict):
        return {k: ("<masked>" if k in MASKED_FIELDS else _mask(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_mask(v) for v in o]
    return o


def compare(a: pathlib.Path, b: pathlib.Path) -> dict:
    ra, rb = a.read_bytes(), b.read_bytes()
    la = [json.loads(x) for x in ra.decode().splitlines() if x.strip()]
    lb = [json.loads(x) for x in rb.decode().splitlines() if x.strip()]
    diffs = set()

    def walk(x, y, path):
        if isinstance(x, dict) and isinstance(y, dict) and x.keys() == y.keys():
            for k in x:
                walk(x[k], y[k], path + (k,))
        elif isinstance(x, list) and isinstance(y, list) and len(x) == len(y):
            for i, (p, q) in enumerate(zip(x, y)):
                walk(p, q, path + ("[]",))
        elif x != y:
            diffs.add(".".join(path))
    if len(la) == len(lb):
        for x, y in zip(la, lb):
            walk(x, y, ())
    masked_same = len(la) == len(lb) and all(json.dumps(_mask(x), sort_keys=True) == json.dumps(_mask(y), sort_keys=True)
                                             for x, y in zip(la, lb))
    only_masked = all(p.split(".")[-1] in MASKED_FIELDS for p in diffs)
    return {"raw_identical": ra == rb, "masked_identical": masked_same, "differing_fields": sorted(diffs),
            "only_masked_fields_differ": only_masked and len(la) == len(lb), "lines": [len(la), len(lb)]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--worlds", type=pathlib.Path, default=None, help="default: <out>/../<out name>_worlds")
    ap.add_argument("--repeat", type=int, default=3, help="race runs in total (>= 2 for the rerun check)")
    ap.add_argument("--cases", default=",".join(CASES))
    a = ap.parse_args()
    for v in ("BONA_HOME", "BONA_PYTHON"):
        if not os.environ.get(v):
            raise SystemExit(f"{v} is not set (live route: see README)")
    out = a.out.resolve()
    if out.exists():
        raise SystemExit(f"{out} exists; use a new folder")
    worlds = (a.worlds or out.parent / f"{out.name}_worlds").resolve()
    out.mkdir(parents=True)
    prov = provenance()
    summary = {"provenance": prov, "runs": [], "reruns": {}}
    for case in a.cases.split(","):
        summary["runs"].append(run_case(case, out, worlds, "", prov))
    if "flex_race_lost_response" in a.cases and a.repeat >= 2:
        base = out / "flex_race_lost_response" / "bundle" / "events.jsonl"
        for i in range(2, a.repeat + 1):
            r = run_case("flex_race_lost_response", out, worlds, f"_rep{i}", prov)
            summary["runs"].append(r)
            summary["reruns"][f"rep{i}"] = compare(base, out / r["dir"] / "bundle" / "events.jsonl")
    exp = json.loads((HERE / "scenarios" / "expected_control_stages.json").read_text(encoding="utf-8"))
    ctl = next((r for r in summary["runs"] if r["case"] == exp["scenario"] and r["tag"] == ""), None)
    if ctl:
        failed = sorted(k for k, v in ctl["stages"].items() if v == "failed")
        summary["control"] = {"failed_stages": failed, "expected": exp["expected_failed_stages"],
                              "exactly_as_frozen": failed == sorted(exp["expected_failed_stages"])
                              and ctl["verdict"] == exp["expected_verdict"]}
    (out / "summary.json").write_bytes((json.dumps(summary, indent=1, sort_keys=True) + "\n").encode())
    print("\n=== SUMMARY"); print(json.dumps({k: summary[k] for k in ("reruns", "control") if k in summary}, indent=1))
    for r in summary["runs"]:
        print(f"{r['case']}{r['tag']}: {r['verdict']}")
    ok = all(r["verdict"] == ("failed" if r["case"] == exp["scenario"] else "passed") for r in summary["runs"])
    ok = ok and summary.get("control", {}).get("exactly_as_frozen", True)
    ok = ok and all(v["only_masked_fields_differ"] and v["masked_identical"] for v in summary["reruns"].values())
    print("RUN CHECK:", "OK" if ok else "NOT OK")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
