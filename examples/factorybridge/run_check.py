"""Exercise FactoryBridge through Town's SQLite mailbox (no HTTP transport)."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

from nandatown.db import TownDB
from nandatown.profiles import PROFILES
from nandatown.records import fingerprint

ROOT = Path(__file__).resolve().parent
from fixture import ORDER

def check(condition, message):
    if not condition:
        raise AssertionError(message)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factory-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("artifacts/factorybridge"))
    args = parser.parse_args()
    factory = args.factory_root.resolve()
    check((factory / "simulator.py").is_file(), "FactoryBridge checkout missing simulator.py")
    with tempfile.TemporaryDirectory(prefix="factorybridge-town-") as tmp:
        db = TownDB(str(Path(tmp) / "town.db"))
        # Quote-shaped profile is required by this version's schema; the
        # manufacturing payload itself passes through the mailbox.
        run_id = db.create_run(PROFILES["quote-clean"].model_dump_json(), now=time.time())
        for name in ("buyer", "seller"):
            token = secrets.token_urlsafe(16)
            db.add_participant(run_id, name, name, ["manufacturing"] if name == "seller" else [], token)
            session = db.authenticate(run_id, name, token, now=time.time())
            check(session is not None and db.session_owner(run_id, session) == name, "join failed")
        check(any(p["name"] == "seller" for p in db.directory(run_id)), "seller missing")
        results = []
        for name, order in (("success", ORDER), ("invalid_quantity", deepcopy(ORDER))):
            if name == "invalid_quantity":
                order["parts"][0]["qty"] = 0
            request_id = "factorybridge-" + name
            request = {"request_id": request_id, "order": order}
            db.accept_message(run_id, "buyer", request_id, "seller",
                              "manufacturing.request", request, fingerprint(request), time.time())
            claim = db.claim_next(run_id, "seller", lease_seconds=60, now=time.time())
            check(claim is not None and claim["message_id"] == request_id, "request not received")
            worker_env = dict(os.environ)
            worker_env["PYTHONPATH"] = str(factory)
            worker = subprocess.run(
                [sys.executable, str(ROOT / "worker.py")], input=json.dumps(claim["body"]),
                text=True, capture_output=True, cwd=factory, env=worker_env, timeout=60)
            check(worker.returncode == 0, "FactoryBridge worker failed: " + worker.stderr)
            payload = json.loads(worker.stdout)
            response_id = "response-" + request_id
            db.accept_message(run_id, "seller", response_id, "buyer", "manufacturing.response",
                              payload, fingerprint(payload), time.time())
            db.ack(run_id, "seller", request_id, claim["fence"], "processed",
                   {"result_status": payload["status"]}, now=time.time())
            reply = db.claim_next(run_id, "buyer", lease_seconds=60, now=time.time())
            check(reply is not None and reply["message_id"] == response_id, "response missing")
            check(reply["body"]["request_id"] == request_id, "request correlation lost")
            db.ack(run_id, "buyer", response_id, reply["fence"], "processed", {}, now=time.time())
            results.append({"case": name, "response": reply["body"]})
        success, rejected = [r["response"] for r in results]
        check(success["status"] == "completed" and success["verdict"]["success"],
              "expected successful simulated fulfillment")
        check(success["truth"]["final_delivery_day"] == 8, "expected delivery on day 8")
        check(success["truth"]["total_cost"] == 42.5, "unexpected simulated total")
        check(any(e["status"] == "delayed" for e in success["truth"]["events"]),
              "supplier delay was not observed")
        check(any(e["status"] == "cancelled" for e in success["truth"]["events"]),
              "shipping replan was not observed")
        check(rejected["status"] == "rejected" and not rejected["simulation_started"],
              "invalid quantity was not rejected before simulation")
        db.finish_run(run_id, now=time.time())
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        (args.out / "town-events.json").write_text(json.dumps(db.events(run_id), indent=2) + "\n")
        print(json.dumps({"result": "PASS", "transport": "direct TownDB; no HTTP",
            "success": {"delivery_day": success["truth"]["final_delivery_day"],
                        "cost_usd_simulated": success["truth"]["total_cost"],
                        "tool_calls": success["tool_usage"]["tool_calls"],
                        "delay_and_shipping_replan": True},
            "boundary": {"invalid_quantity": rejected["status"],
                         "simulation_started": rejected["simulation_started"]},
            "evidence_dir": str(args.out)}, indent=2))

if __name__ == "__main__":
    main()

