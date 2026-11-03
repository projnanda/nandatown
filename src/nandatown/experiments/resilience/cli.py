# SPDX-License-Identifier: MIT
# Copyright (c) 2026 nanda-resilience contributors
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from nandatown.bundle import load_bundle, verify_bundle

from .campaign import run_campaign
from .evaluator import analyze, metrics
from .experiment import run_experiment
from .model import CONDITIONS, POLICIES, Settings, build_spec


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Many Agents, One Point of Failure: Nanda Town Lab extension")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "scenario"):
        command = commands.add_parser(name)
        command.add_argument("--condition", choices=CONDITIONS, default="shared")
        command.add_argument("--policy", choices=POLICIES, default="dependency")
        command.add_argument("--seed", type=int, default=7)
        command.add_argument("--disclosure", type=float, default=1.0)
        command.add_argument("--buyers", type=int, default=8)
        command.add_argument("--rounds", type=int, default=20)
        command.add_argument("--attempts", type=int, default=3)
        command.add_argument("--interval", type=float, default=2.0)
        command.add_argument("--service-time", type=float, default=0.35)
        command.add_argument("--dependency-capacity", type=int, default=12)
        command.add_argument("--out", type=Path, required=True)
    campaign = commands.add_parser("campaign")
    campaign.add_argument("--out", type=Path, required=True)
    campaign.add_argument("--seeds", type=int, nargs="+", default=list(range(10)))
    campaign.add_argument("--disclosures", type=float, nargs="+", default=[0.0, 0.5, 1.0])
    campaign.add_argument("--keep-all", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "campaign":
            summary = run_campaign(args.out, args.seeds, args.disclosures, args.keep_all)
            print(f"Verified {len(summary['runs'])} runs. Open {args.out / 'report.html'}")
        elif args.command == "verify":
            problems = verify_bundle(str(args.bundle))
            if problems:
                raise ValueError("\n".join(str(problem) for problem in problems))
            bundle = load_bundle(str(args.bundle))
            row = metrics(analyze(bundle["profile"], bundle["events"], bundle["run"].run_id))
            print(json.dumps({"verified": True, "metrics": row}, indent=2, allow_nan=False))
        else:
            settings = Settings(seed=args.seed, policy=args.policy,
                                condition=args.condition, disclosure=args.disclosure,
                                buyers=args.buyers, rounds=args.rounds,
                                attempts=args.attempts, interval=args.interval,
                                service_time=args.service_time,
                                dependency_capacity=args.dependency_capacity)
            if args.command == "scenario":
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("x", encoding="utf-8") as handle:
                    yaml.safe_dump(build_spec(settings).model_dump(mode="json"),
                                   handle, sort_keys=False)
                plugin = Path(__file__).with_name("plugin.py")
                print(f'Wrote {args.out}; run with nandatown run "{args.out}"'
                      f' --plugin "{plugin}" --out NEW_DIRECTORY')
            else:
                print(json.dumps(run_experiment(settings, args.out), indent=2, allow_nan=False))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
