#!/usr/bin/env python3
"""One FlexGrid process on a Town run: the coordinator, a buyer, or nodes.

FlexGrid itself is not in this repository. run_check.py stages a copy of a
FlexGrid checkout, writes it a throw-away config folder and starts this file
once per process, with the staged code on PYTHONPATH and FLEXGRID_CONFIG_DIR
set. Everything below is an adapter around FlexGrid's own entry points; the
market, the nodes, the buyers and their Town client are FlexGrid's.

  python flexgrid_node.py coordinator
  python flexgrid_node.py buyer utility --port 41411
  python flexgrid_node.py nodes             # the homes in FLEXGRID_NODES
  python flexgrid_node.py nodes --buildings
  python flexgrid_node.py reference OUT     # the same day with no Town at all
  python flexgrid_node.py answers OUT       # what a model answered this folder's nodes

What the adapter adds, and nothing else:

  * a join through a Town Run Grant when the run pins this name to a portable
    identity (FlexGrid's TownSession only knows join tokens)
  * the coordinator's answer to Town's quote_request: the closing statement
    (see run_check.py), the one exchange Town's stage evaluator can judge
  * the same day through FlexGrid's own offline simulator, the reference a
    rules day is held to
  * a reader for each node's decision log: the model's answers, each
    decision's receipt, what each person was told and each guard verdict
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from pathlib import Path


def join_with_grants() -> None:
    """Teach TownSession to join through a Run Grant, if this folder has any.

    A role pinned to a portable identity cannot join with a bare token. The
    session proof is plain Ed25519 over Town's canonical JSON, so this needs
    no nandatown import: FlexGrid may run on its own interpreter."""
    from flexgrid_core import config, town

    path = config.CONFIG_DIR / "secrets" / "town_grants.json"
    if not path.exists():
        return
    grants = json.loads(path.read_text())

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    def proof(private_hex: str, run_id: str, name: str) -> str:
        text = json.dumps({"purpose": "join", "run_id": run_id, "name": name},
                          sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
        return key.sign(text.encode()).hex()

    token_join = town.TownSession._join

    def _join(self):
        held = grants.get(self.name)
        if held is None:
            return token_join(self)
        r = self.http.post(
            f"/runs/{self.run_id}/join",
            json={"name": self.name, "grant": held["grant"],
                  "grant_signature": held["grant_signature"],
                  "session_proof": proof(held["session_private"], self.run_id,
                                         self.name)})
        r.raise_for_status()
        data = r.json()
        self.session = data["session"]
        return data

    town.TownSession._join = _join


def closed_nodes(market) -> int:
    """The nodes whose day closed on their own word: each one answered the call
    for offers, and each one under contract had every half hour's reading reach
    this coordinator and was settled on them, paid or told why not. A node its
    agent kept out of the sale has nothing more to close. So the count doesn't
    hang on what an agent chose, and a reading Town refused still shows."""
    closed = set()
    for ev in market.events.values():
        res = ev.get("result") or {}
        slots = len((ev.get("need") or {}).get("slots") or [])
        settled = set(((res.get("split") or {}).get("per_home") or {})) \
            | set(res.get("not_counted_homes") or [])
        readings = ev.get("readings") or {}
        for node in ev.get("offers") or {}:
            c = (ev.get("commitments") or {}).get(node) or {}
            if c.get("status") != "ACCEPTED":
                closed.add(node)
            elif node in settled and len(readings.get(node) or {}) == slots:
                closed.add(node)
    return len(closed)


def statement(market, sku: str) -> int:
    """The closing statement, from this coordinator's own ledger: the cents it
    paid its sellers, or for flexgrid-nodes-closed how many nodes' days closed
    (closed_nodes)."""
    with market.lock:
        if sku == "flexgrid-nodes-closed":
            return closed_nodes(market)
        paid: dict = {}
        for ev in market.events.values():
            paid.update(((ev.get("result") or {}).get("split") or {})
                        .get("per_home") or {})
    return len(paid) if sku == "flexgrid-sellers-settled" \
        else round(100 * sum(paid.values()))


def run_coordinator() -> None:
    from flexgrid_core import coordinator

    class Coordinator(coordinator.Coordinator):
        """FlexGrid's coordinator, plus Town's quote_request."""

        answered: dict = {}

        def handle(self, msg):
            if msg.get("kind") != "quote_request":
                return super().handle(msg)
            mid = msg["message_id"]
            first = mid not in self.answered
            if first:
                body = msg.get("body") or {}
                self.answered[mid] = {
                    "message_id": "r-" + mid.removeprefix("q-"),
                    "to": msg["from"], "kind": "quote_response",
                    "body": {"request_id": mid, "sku": body.get("sku"),
                             "quantity": body.get("quantity"),
                             "total_cents": statement(self.market,
                                                      body.get("sku"))}}
            reply = self.answered[mid]
            self.town.send(reply["to"], reply["kind"], reply["body"],
                           reply["message_id"])
            note = ({"applied": True,
                     "total_cents": reply["body"]["total_cents"]}
                    if first else {"duplicate": True})
            self.town.ack(mid, msg["fence"], "processed", note)

    coordinator.Coordinator = Coordinator
    sys.argv = ["flexgrid-coordinator", "run"]
    coordinator.main()


def run_buyer(name: str, port: int) -> None:
    """A stand-in buyer as a live process: FlexGrid's utility main() around
    StandInBuyer, as FlexGrid's own deployment does it."""
    import uvicorn

    from flexgrid_core import config, wire
    from flexgrid_core.stand_in import StandInBuyer
    from flexgrid_core.town import TownSession
    from flexgrid_core.utility import build_app
    try:
        from flexgrid_core import keep
    except ImportError:             # the code of 6 Nov has no state to keep
        keep = None

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    buyer = StandInBuyer(name)
    if name != "utility":
        buyer.town = TownSession(buyer.cfg["town_url"],
                                 buyer.cfg["town_run_id"], name,
                                 config.secret(f"{name}_town_token"))
    buyer.cfg.setdefault("utility_settle_quiet_s", 30.0)
    logs = config.REPO_DIR / "logs"
    logs.mkdir(exist_ok=True)
    wire.open_log(str(logs / f"town_wire_{name}.jsonl"))
    keeper = keep.for_buyer(buyer) if keep else None
    buyer.town.join_until_up(
        then=lambda: threading.Thread(target=buyer.inbox_loop,
                                      daemon=True).start(),
        log=lambda m: print(m, flush=True))
    threading.Thread(target=buyer.tick_loop, daemon=True).start()
    try:
        uvicorn.run(build_app(buyer), host="127.0.0.1", port=port,
                    log_level="warning")
    finally:
        if keeper:
            keeper.close()


def run_nodes(buildings: bool) -> None:
    from flexgrid_core import fleet

    wanted = {n for n in os.environ.get("FLEXGRID_NODES", "").split(",") if n}
    base = fleet.BuildingFleet if buildings else fleet.Fleet

    class Nodes(base):
        """FlexGrid's fleet, with the roster this process was given."""

        def roster(self):
            rows = super().roster()
            return [r for r in rows if not wanted or r["name"] in wanted]

    setattr(fleet, base.__name__, Nodes)
    sys.argv = ["flexgrid-fleet", "run"] + (["--buildings"] if buildings
                                            else [])
    fleet.main()


def run_reference(out: str) -> None:
    """The same day through FlexGrid's own offline simulator: one process, an
    in-memory bus, a virtual clock, no Town. What it pays is what the day
    should pay, and run_check.py freezes that in the Town profile."""
    sys.path.insert(0, str(Path(os.environ["PYTHONPATH"]) / "tools"))
    import market_day
    import sim_event

    day = json.loads(os.environ["FLEXGRID_REFERENCE"])
    r = sim_event.run_event(
        day["day"], quiet=True, forecast="temp_regression",
        only=set(day["homes"]), control_mode="rolling",
        surprises=[dict(x) for x in day.get("surprises") or []],
        on_homes=market_day.set_owners, buildings=day["buildings"],
        orders=day["orders"], report_rule="exception")
    if "error" in r:
        sys.exit(f"the offline day did not settle: {r['error']}")
    ev = next(iter(sim_event.LAST["market"].events.values()))
    split = ev["result"]["split"]
    start, end = r["window"]
    half: dict = {}                 # true relief, every node's meter against its twin
    for row in r["series"]:
        if start <= row["ts"] < end:
            k = row["ts"][:14] + ("00" if row["ts"][14:16] < "30" else "30")
            half.setdefault(k, []).append(row["import_cf_kw"] - row["import_kw"])
    told = {}                       # each node's own payment notice: kWh and $
    for name, h in sim_event.LAST["homes"].items():
        told[name] = [p for p in getattr(h, "payments", []) or []]
    Path(out).write_text(json.dumps({
        "sold_kwh": r["promised_kwh"],
        "delivered_kwh_measured": r["delivered_kwh_measured"],
        "delivered_kwh_true": r["delivered_kwh_true"],
        "delivered_kw_by_half_hour": {k: round(sum(v) / len(v), 3)
                                      for k, v in sorted(half.items())},
        "buyers_usd": {b: x["paid_usd"]
                       for b, x in ev["result"]["by_buyer"].items()},
        "pool_usd": split["pool"], "fee_usd": split["fee"],
        "paid_usd": split["per_home"],
        "delivered_kwh_by_node": {
            n: round(sum(float(p.get("delivered_kwh") or 0) for p in ps), 3)
            for n, ps in told.items()},
        "surprises": r.get("surprises"),
        "messages_by_kind": r["messages"]["by_kind"]}, indent=1))


def run_answers(out: str) -> None:
    """Each answer the model gave this folder's nodes, read through FlexGrid's
    own decision log: which node, at which step, what it said, how long it took,
    and which device calls followed. An answer can ask for what code then doesn't
    do. The rows hold the whole question too, and each log is a hash chain."""
    from flexgrid_core import config
    from flexgrid_core.node import record

    steps = {"offer": "5 offer", "raise": "5 second round",
             "accept": "6 commitment"}
    rows, chains, receipts, told, verdicts = [], {}, [], [], []
    for path in sorted((config.state_dir() / "decisions").glob("*.jsonl")):
        log = record.Log(path.stem, path)
        chains[path.stem] = log.verify()
        step = sim = last = None
        for r in log.rows:
            if r["kind"] == "receipt":      # every node an agent: one decision
                receipts.append({"node": path.stem, **{k: r.get(k) for k in (
                    "point", "ts", "asked", "rule", "chose", "less_than_rule",
                    "by", "reason", "facts", "flags", "told")}})
            elif r["kind"] == "told":       # what the owner or manager heard
                told.append({"node": path.stem, **{k: r.get(k) for k in (
                    "ts", "point", "to", "text")}})
            if r["kind"] == "act":          # each device call, as the guard answered it
                ans = r.get("answer") or {}
                verdicts.append({"node": path.stem, "ts": r.get("ts"),
                                 "tool": r.get("tool"),
                                 "target": (r.get("args") or {}).get("target"),
                                 "status": ans.get("status"),
                                 "why": str(ans.get("why") or "")[:200]})
            if r["kind"] == "step" and r.get("state") == "entered":
                step, sim, last = r["step"], r["ts"], None
            elif r["kind"] == "act" and last is not None:
                last["did"].append({"tool": r.get("tool"),
                                    "target": (r.get("args") or {}).get("target"),
                                    "status": (r.get("answer") or {}).get("status")})
            elif r["kind"] == "decide":
                meta, facts = r.get("meta") or {}, r.get("facts") or {}
                what = str(r.get("session")).removeprefix(path.stem + "-")
                safe = (facts.get("safe_offer") or {}).get("per_half_hour") or []
                rows.append({"node": path.stem,
                             "step": steps.get(what.split("-")[0], what),
                             "sim": sim, "decided": r.get("answer"),
                             "ms": meta.get("ms"), "error": meta.get("error"),
                             "grounds": facts.get("grounds") or [],
                             "safe_kw": [x.get("total_kw") for x in safe]})
            elif r["kind"] == "model":
                a = r.get("answer") or {}
                names = {t.get("function", {}).get("name") for t in r.get("tools") or []}
                said = "owner's note" if names & {"set_owner_wish", "no_wish"} else None
                last = {"node": path.stem, "step": step or said or "outside a step", "sim": sim,
                        "decided": {"calls": a.get("calls"),
                                    "said": a.get("text")},
                        "ms": a.get("ms"), "error": a.get("error"), "did": []}
                rows.append(last)
    Path(out).write_text(json.dumps({"answers": rows, "chains": chains,
                                     "receipts": receipts, "told": told,
                                     "verdicts": verdicts}))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what", choices=["coordinator", "buyer", "nodes",
                                     "reference", "answers"])
    ap.add_argument("name", nargs="?", default="utility")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--buildings", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("FLEXGRID_CONFIG_DIR"):
        sys.exit("FLEXGRID_CONFIG_DIR is not set: FlexGrid would use a real"
                 " config folder")
    if not Path(os.environ["FLEXGRID_CONFIG_DIR"]).is_dir():
        sys.exit("FLEXGRID_CONFIG_DIR does not exist")
    if a.what == "reference":
        return run_reference(a.name)
    if a.what == "answers":
        return run_answers(a.name)
    join_with_grants()
    if a.what == "coordinator":
        run_coordinator()
    elif a.what == "buyer":
        run_buyer(a.name, a.port)
    else:
        run_nodes(a.buildings)


if __name__ == "__main__":
    main()
