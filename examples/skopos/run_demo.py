"""Run the SKOPOS Golden Dome scenario and print what happened.

    python examples/skopos/run_demo.py                 # quick, in memory
    python examples/skopos/run_demo.py --town          # agents talk through Nanda Town
    python examples/skopos/run_demo.py --trials 1000   # many random attacks
    python examples/skopos/run_demo.py --out /tmp/skopos-result.json

--town starts a local Nanda Town coordinator in this process and gives
every agent its own Town identity. Each report, alert, plan and
approval is a real Town mailbox message that is sent, claimed and
acknowledged. Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenario  # noqa: E402

AGENTS = {
    **{sat: "watch" for sat in scenario.COVERAGE},
    "guardian": "trust_check",
    "aim": "aim",
    "battle": "battle_manager",
    "commander": "human_gate",
}


class TownBus:
    """send / drain over real Nanda Town mailbox messages."""

    def __init__(self, workdir: Path) -> None:
        from fastapi.testclient import TestClient
        from nandatown.client import TownClient
        from nandatown.coordinator import build_app
        from nandatown.records import TestProfile

        admin_token = secrets.token_urlsafe(24)
        app = build_app(str(workdir / "town.db"), admin_token=admin_token)
        admin = TestClient(app, headers={"X-Town-Admin": admin_token})
        profile = TestProfile(
            name="skopos-golden-dome-scenario",
            # Town's current schema needs a quote task; this is a
            # zero-value placeholder. The scenario uses custom messages.
            task={"kind": "quote", "sku": "synthetic-scenario",
                  "quantity": 1, "unit_price_cents": 0,
                  "expected_total_cents": 0},
            roles=dict(AGENTS),
            capabilities={name: [f"skopos.{role}"]
                          for name, role in AGENTS.items()},
            fault="none", lease_seconds=30,
            evaluator="skopos-golden-dome-v1")
        created = admin.post("/runs", json={"profile": profile.model_dump()})
        created.raise_for_status()
        created = created.json()
        self.run_id = created["run_id"]
        self.admin = admin
        self.clients = {}
        for name in AGENTS:
            c = TownClient("http://testserver", self.run_id,
                           http=TestClient(app))
            c.join(name, created["join_tokens"][name])
            self.clients[name] = c
        self.count = 0

    def send(self, frm, to, kind, body):
        self.count += 1
        self.clients[frm].send(f"{frm}-{self.count}", to, kind, body)

    def drain(self, name):
        out = []
        client = self.clients[name]
        while (claim := client.claim()) is not None:
            out.append({"from": claim["from"], "kind": claim["kind"],
                        "body": claim["body"]})
            client.ack(claim["message_id"], claim["fence"], "processed")
        return out

    def participants(self):
        return self.clients["commander"].participants()

    def events(self):
        r = self.admin.get(f"/runs/{self.run_id}/events")
        r.raise_for_status()
        return r.json()["events"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--town", action="store_true")
    ap.add_argument("--trials", type=int, default=1000)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    report: dict = {"project": "SKOPOS", "scenario": "Golden Dome, made-up data",
                    "story_run": {}, "limitations": LIMITATIONS}
    params = scenario.Params()
    with tempfile.TemporaryDirectory(prefix="skopos-") as tmp:
        for mode in scenario.MODES:
            bus = None
            if args.town:
                (Path(tmp) / mode).mkdir()
                bus = TownBus(Path(tmp) / mode)
            r = scenario.simulate(mode, params, seed=0, bus=bus)
            report["story_run"][mode] = r.__dict__
            if bus:
                report["story_run"][mode]["town_run_id"] = bus.run_id
                report["story_run"][mode]["town_events"] = len(bus.events())
    report["transport"] = "nanda_town_mailbox" if args.town else "in_memory"
    if args.trials:
        report["random_trials"] = scenario.run_trials(args.trials)

    for mode, r in report["story_run"].items():
        print(f"\n== {mode}")
        for line in r["timeline"]:
            print("  " + line)
    if args.trials:
        print("\n== random trials")
        for mode, s in report["random_trials"].items():
            print(f"  {mode:9s} all 5 tracked: "
                  f"{s['all_5_tracked_correctly_pct']:5.1f}%   "
                  f"halted: {s['engagement_halted_pct']:5.1f}%")
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


LIMITATIONS = [
    "All data are made up. Flat 2-D toy geometry, straight-line flight.",
    "The trust check is a simple plausibility rule written for this demo."
    " It is not Guardian Veritas and does not show how Guardian Veritas"
    " works.",
    "The rule catches a satellite that suddenly reports an impossible"
    " jump. A slow, gradual spoof was not tested and may not be caught.",
    "The commander is a scripted stand-in for a human approval click.",
    "No message can fire anything. There is no weapon or interceptor"
    " model. 'At risk' only means the aim point is off by more than 5 km.",
    "Results show the workflow in simulation. They are not evidence of"
    " real-world performance.",
]


if __name__ == "__main__":
    raise SystemExit(main())
