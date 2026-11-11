"""VeriFlex (Bona) energy-flexibility Lab roles. LIVE ONLY: needs a fresh HW8 world and the private Bona runtime.

Environment (set by run_check.py): BONA_HOME (the Bona runtime checkout), BONA_PYTHON (its Python),
BONA_FLEX_WORLD (a world made by `python -m bench.hw8.fresh_world`), BONA_FLEX_SIDECAR (this run's sidecar folder),
BONA_FLEX_TIMELINE (the live timeline file; lines are also printed as they happen).

Process layout (no Town src/ change; Town runs in its own venv):
- `flex_desk` is the only agent that talks to Bona. It spawns `$BONA_PYTHON -m bona.server` and calls the four
  MCP tools through bona_link.py (stdlib JSON-RPC over stdio).
- The seller behaviours and the SyntheticWorld run UNCHANGED inside `$BONA_PYTHON -m bench.hw8.opserver` (bench
  cannot be imported in Town's venv); `flex_seller` asks it for its behaviour's signed offers, `flex_meter` asks it
  to execute the committed W, step the world and push the meter readings (operator-side feed, as BonaArm does).
- `flex_desk_naive` (the control) grants every hold in full without Bona and keeps its own model ("control_model").

Town events carry aliases and integers only (o-N offer, a-N assessment, r-N request, h-N hold). Full Bona objects
go to the sidecar; hashes in events are the declared binding fields (see run_check.MASKED_FIELDS).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bona_flex_offline import dump_bytes, receipt_sha256, sha256_bytes, verify_receipt  # noqa: E402
from bona_link import BonaLink  # noqa: E402

from nandatown.sim.agents import SimAgent, role  # noqa: E402

FRESHNESS_S = 30          # Bona's commit tool contract: "created_at must be within 30 s of server time"
VALID_FOR_S = 900         # Bona default assessment validity (assess_offers valid_until - now)
HOLD_TTL_S = 300          # Bona max hold TTL; the buyer asks for 300 s
RUN_BUDGET_S = min(FRESHNESS_S, VALID_FOR_S, HOLD_TTL_S) - 5


class _Op:
    """JSON-lines client of bench.hw8.opserver (Bona venv)."""

    def __init__(self, world: str):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.pop("PYTHONIOENCODING", None)
        env["PYTHONPATH"] = os.environ["BONA_HOME"]
        self.p = subprocess.Popen([os.environ["BONA_PYTHON"], "-m", "bench.hw8.opserver", "--world", world],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=os.environ["BONA_HOME"], env=env, text=True, encoding="utf-8")
        self._err: list[str] = []
        threading.Thread(target=lambda: self._err.extend(self.p.stderr), daemon=True).start()
        first = json.loads(self.p.stdout.readline() or '{"ok": false, "error": "no ready line"}')
        if not first.get("ok"):
            raise RuntimeError(f"opserver failed to start: {first} {''.join(self._err[-20:])}")
        self.bona_did = first["bona_did"]

    def call(self, op: str, **kw) -> dict:
        self.p.stdin.write(json.dumps({"op": op, **kw}) + "\n")
        self.p.stdin.flush()
        r = json.loads(self.p.stdout.readline() or '{"ok": false, "error": "opserver closed"}')
        if not r.get("ok"):
            raise RuntimeError(f"opserver {op}: {r.get('error')} {''.join(self._err[-20:])}")
        return r

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.p.kill()


class Ctx:
    """Per-run shared state (all roles live in one Town process)."""
    _inst: Ctx | None = None

    @classmethod
    def get(cls) -> Ctx:
        if cls._inst is None:
            cls._inst = Ctx()
        return cls._inst

    @classmethod
    def reset(cls) -> None:
        if cls._inst is not None:
            cls._inst.close()
        cls._inst = None

    def __init__(self):
        self.world = pathlib.Path(os.environ["BONA_FLEX_WORLD"])
        self.sidecar = pathlib.Path(os.environ["BONA_FLEX_SIDECAR"])
        self.timeline_path = pathlib.Path(os.environ["BONA_FLEX_TIMELINE"])
        self.info = json.loads((self.world / "world.json").read_text(encoding="utf-8"))
        self.op = _Op(str(self.world))
        self.files: dict[str, bytes] = {}
        self.offers: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.n = {"o": 0, "a": 0, "r": 0, "h": 0}
        self.link = None                                  # the desk's BonaLink, closed by close()
        self.tl = open(self.timeline_path, "a", encoding="utf-8", newline="\n")  # noqa: SIM115

    def alias(self, k: str) -> str:
        self.n[k] += 1
        return f"{k}-{self.n[k]}"

    def put(self, rel: str, obj) -> bytes:
        b = dump_bytes(obj)
        if rel in self.files and self.files[rel] != b:
            raise RuntimeError(f"sidecar {rel} would change")
        self.files[rel] = b
        return b

    def live(self, t: float, who: str, text: str) -> None:
        line = f"LIVE  t={t:6.2f}  {who:<8} {text}"
        print(line, flush=True)
        self.tl.write(line + "\n")
        self.tl.flush()

    def write_sidecar(self) -> str:
        self.sidecar.mkdir(parents=True, exist_ok=False)
        for rel, b in self.files.items():
            p = self.sidecar / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b)
        man = {rel: sha256_bytes(b) for rel, b in sorted(self.files.items())}
        mb = dump_bytes({"files": man})
        (self.sidecar / "sidecar_manifest.json").write_bytes(mb)
        return sha256_bytes(mb)

    def close(self):
        try:
            if self.link is not None:
                self.link.close()
            self.op.close()
        finally:
            self.tl.close()


def _idem(buyer: str, rnd: int, seller: str, action: str) -> str:
    return hashlib.sha256(f"hw8|{buyer}|{rnd}|{seller}|{action}".encode()).hexdigest()[:32]


# ---- sellers and meter (wrap the bench behaviours and the world through opserver) -----------------------------
@role("flex_seller")
class FlexSeller(SimAgent):
    def on_start(self):
        self.ctx = Ctx.get()
        self.api.register(["sell.flex"], {"seller_id": self.config["seller_id"]})

    def handle_flex_quote_request(self, msg):
        r = self.ctx.op.call("offers", seller=self.config["seller_id"], step=int(msg["body"]["step"]),
                             price_musd_per_mwh=self.config.get("price_musd_per_mwh"))
        aliases = []
        for o in r["offers"]:
            a = self.ctx.alias("o")
            self.ctx.offers[a] = o
            self.ctx.put(f"offers/{a}.json", o)
            aliases.append(a)
        w = r["offers"][0]["w"] if r["offers"] else 0
        self.ctx.live(self.api.now, self.name, f"offers {aliases} ({w // 1000} kW each, behaviour "
                      f"{r['behaviour']['kind']}) to {msg['sender']}")
        self.api.reply(msg, "flex_quote", {"offers": aliases, "w": w, "seller_id": self.config["seller_id"]})


@role("flex_meter")
class FlexMeter(SimAgent):
    def on_start(self):
        self.ctx = Ctx.get()
        self.api.register(["meter.flex"])

    def handle_flex_dispatch(self, msg):
        b = msg["body"]
        r = self.ctx.op.call("step", step=int(b["step"]), committed=b["committed_w"])
        rows = [{"meter": x["meter"], "ts": x["ts"], "p_net_w": x["p_net_w"]} for x in r["pushed"]]
        prev = self.ctx.files.get("readings.jsonl", b"")
        self.ctx.files["readings.jsonl"] = prev + b"".join(
            (json.dumps(x, sort_keys=True) + "\n").encode() for x in rows)
        self.ctx.live(self.api.now, self.name, f"step {b['step']}: executed {r['executed_w']} W; pushed "
                      f"{len(rows)} reading(s) to Bona's feed")
        self.api.observe("flex_metered", f"step-{b['step']}",
                         {"pushed": len(rows), "executed_w": r["executed_w"]})
        self.api.reply(msg, "flex_readings", {"step": b["step"], "pushed": len(rows)})


# ---- buyer -----------------------------------------------------------------------------------------------------
@role("flex_buyer")
class FlexBuyer(SimAgent):
    def on_start(self):
        self.ctx = Ctx.get()
        self.api.register(["buy.flex"])
        self.pending: dict[str, dict] = {}
        self.done: set[str] = set()
        self.seller_of: dict[str, str] = {}
        self.api.later(float(self.config.get("start_at", 0.5)), self.start)

    def start(self):
        cards = self.api.lookup("sell.flex")
        target = next(c["name"] for c in cards if c["name"] == self.config["seller"])
        self.ctx.live(self.api.now, self.name, f"looks up sell.flex, asks {target} for step 0 "
                      f"(wants {self.config['want_w'] // 1000} kW)")
        self.api.send(target, "flex_quote_request", {"step": 0})

    def handle_flex_quote(self, msg):
        self.quote_from = msg["sender"]
        self.seller_id = msg["body"]["seller_id"]
        self.api.send(self.config["desk"], "flex_assess", {"buyer": self.config["buyer_id"],
                                                            "offers": msg["body"]["offers"][:1]})

    def _request(self, action, w, assessment=None, hold=None) -> str:
        now = int(time.time())
        r = self.ctx.alias("r")
        self.ctx.requests[r] = {
            "action": action, "assessment_id": assessment, "hold_id": hold, "w": int(w),
            "expires_at": now + HOLD_TTL_S if action == "hold" else None,
            "idempotency_key": _idem(self.config["buyer_id"], 1, self.seller_id, action),
            "on_behalf_of": self.config["legal_entity"], "capability": None, "created_at": now}
        return r

    def handle_flex_assess_result(self, msg):
        a = next((x for x in msg["body"]["assessments"] if x["status"] == "ok" and x["feasible_w"] > 0), None)
        if a is None:
            self.ctx.live(self.api.now, self.name, "no feasible assessment; stops")
            return
        w = min(a["feasible_w"], int(self.config["want_w"]))
        r = self._request("hold", w, assessment=a["assessment"])
        self.pending[r] = {"kind": "hold", "tries": 1}
        self.ctx.live(self.api.now, self.name, f"holds {w // 1000} kW on {a['assessment']} (request {r})")
        self.api.send(self.config["desk"], "flex_hold", self._body(r))
        self.api.later(float(self.config.get("retry_after", 2.0)), lambda r=r: self._retry(r))

    def _retry(self, r):
        if r in self.done or self.pending[r]["tries"] >= 2:
            return
        self.pending[r]["tries"] += 1
        self.ctx.live(self.api.now, self.name, f"no answer for {r}: retries the IDENTICAL request")
        self.api.send(self.config["desk"], "flex_hold", self._body(r))

    def _body(self, r) -> dict:
        """What the buyer sends: the request alias and the sha256 of the exact bytes it is (re)sending."""
        return {"request": r, "request_sha256": sha256_bytes(dump_bytes(self.ctx.requests[r]))}

    def handle_flex_hold_result(self, msg):
        b = msg["body"]
        if b["request"] in self.done:
            self.ctx.live(self.api.now, self.name, f"ignores a repeated answer for {b['request']}")
            return
        self.done.add(b["request"])
        if b["outcome"] != "hold":
            self.ctx.live(self.api.now, self.name, f"hold refused ({b.get('reason_code')})")
            return
        h = b["hold"]
        self.seller_of[h] = self.quote_from
        self.api.escrow_hold(b["w_granted"] // 1000, h)
        r = self._request("confirm", b["w_granted"], hold=h)
        self.ctx.live(self.api.now, self.name, f"escrows {b['w_granted'] // 1000} cents and confirms {h}")
        self.api.send(self.config["desk"], "flex_confirm", {**self._body(r), "hold": h})

    def handle_flex_confirm_result(self, msg):
        b = msg["body"]
        self.ctx.live(self.api.now, self.name, f"{b['hold']} {b['outcome']}")
        if b["outcome"] != "confirm":              # QA 5199c77 L5: a refused confirm returns the escrow
            self.api.escrow_refund(b["hold"])
            self.ctx.live(self.api.now, self.name, f"confirm refused: escrow for {b['hold']} refunded")

    def handle_flex_settled(self, msg):
        b = msg["body"]
        if b["verdict"] == "delivered":
            self.api.escrow_release(b["hold"], self.seller_of[b["hold"]])
            self.ctx.live(self.api.now, self.name, f"{b['hold']} delivered: escrow released to "
                          f"{self.seller_of[b['hold']]}")
        else:
            self.api.escrow_refund(b["hold"])
            self.ctx.live(self.api.now, self.name, f"{b['hold']} {b['verdict']}: escrow refunded")


# ---- desks -----------------------------------------------------------------------------------------------------
class _DeskBase(SimAgent):
    source = "bona"

    def on_start(self):
        self.ctx = Ctx.get()
        self.api.register(["flex.desk"])
        self.sellers = list(self.config["sellers"])
        self.assess: dict[str, dict] = {}       # a-N -> {buyer, seller, meter, step, offer, ...}
        self.hold_alias: dict[str, str] = {}    # real hold id -> h-N
        self.holds: dict[str, dict] = {}        # h-N -> {seller, meter_step, w, buyer, real}
        self.confirmed: list[str] = []
        self.attempts: dict[str, int] = {}
        self.clock = {"assess_calls": [], "commits": [], "first_wall": None, "last_wall": None,
                      "interval_end_s": []}
        self._start()
        self.api.later(float(self.config.get("settle_at", 20.0)), self.dispatch)

    def _hold_of(self, real: str | None) -> str | None:
        if real is None:
            return None
        if real not in self.hold_alias:
            self.hold_alias[real] = self.ctx.alias("h")
        return self.hold_alias[real]

    def dispatch(self):
        committed: dict[str, int] = {}
        for h in self.confirmed:
            committed[self.holds[h]["seller"]] = committed.get(self.holds[h]["seller"], 0) + self.holds[h]["w"]
        self.ctx.live(self.api.now, self.name, f"interval over: dispatches committed W {committed} to the meter")
        self.api.send(self.config["meter"], "flex_dispatch", {"step": 0, "committed_w": committed})


@role("flex_desk")
class FlexDesk(_DeskBase):
    source = "bona"

    def _start(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.pop("PYTHONIOENCODING", None)
        env.update(self.ctx.info["env"])
        env["PYTHONPATH"] = os.environ["BONA_HOME"]
        self.link = BonaLink([os.environ["BONA_PYTHON"], "-m", "bona.server"], env=env, cwd=os.environ["BONA_HOME"])
        self.ctx.link = self.link
        self.did = self.ctx.info["bona_did"]
        tools = sorted(self.link.list_tools())
        self.ctx.live(self.api.now, self.name, f"spawned the real Bona MCP server; tools {tools}")
        self.ctx.put("tools.json", {"tools": tools, "server_info": self.link.server_info})
        for s in self.sellers:
            self._trust(s, "pre")

    def _wall(self):
        t = time.time()
        if self.clock["first_wall"] is None:
            self.clock["first_wall"] = t
        self.clock["last_wall"] = t
        return t

    def _trust(self, seller, phase):
        self._wall()
        t = self.link.call("get_trust", {"seller_id": seller})
        self.ctx.put(f"trust/{seller}_{phase}.json", t)
        self.api.observe("flex_trust", seller, {"source": self.source, "phase": phase,
                                                "deliveries_verified": t.get("deliveries_verified"),
                                                "committed_wh": t.get("committed_wh"), "tier": t.get("tier")})
        self.ctx.live(self.api.now, self.name, f"get_trust({seller}) [{phase}]: tier {t.get('tier')}, "
                      f"{t.get('deliveries_verified')} verified deliveries")

    def handle_flex_assess(self, msg):
        b = msg["body"]
        offers = [self.ctx.offers[o] for o in b["offers"]]
        w0 = self._wall()
        res = self.link.call("assess_offers", {"buyer_id": b["buyer"], "offers": offers})["assessments"]
        out = []
        for o, a in zip(b["offers"], res, strict=True):
            al = self.ctx.alias("a")
            off = self.ctx.offers[o]
            self.assess[al] = {"real": a["assessment_id"], "buyer": b["buyer"], "seller": a["seller"],
                               "meter": a["meter"], "start_s": a["interval"][0], "valid_until": a["valid_until"]}
            self.clock["assess_calls"].append(w0)
            self.clock["interval_end_s"].append(a["interval"][1])
            self.ctx.put(f"assessments/{al}.json", a)
            self.api.observe("flex_assessed", al, {
                "source": self.source, "buyer": b["buyer"], "seller": a["seller"], "meter": a["meter"],
                "step": a["interval"][0], "offer": o, "offered_w": off["w"], "feasible_w": a["feasible_w"],
                "status": a["status"]})
            self.ctx.live(self.api.now, self.name, f"assess_offers({b['buyer']}, {o}) -> {al}: {a['status']}, "
                          f"feasible {a['feasible_w'] // 1000} kW")
            out.append({"assessment": al, "offer": o, "status": a["status"], "feasible_w": a["feasible_w"]})
        self.api.reply(msg, "flex_assess_result", {"assessments": out})

    def _commit(self, msg, action):
        r = msg["body"]["request"]
        req = dict(self.ctx.requests[r])
        if action == "hold":
            a = self.assess[req["assessment_id"]]
            req["assessment_id"] = a["real"]
            meter_step, seller = f"{a['meter']}@{a['start_s']}", a["seller"]
        else:
            h = self.holds[req["hold_id"]]
            req["hold_id"] = h["real"]
            meter_step, seller = h["meter_step"], h["seller"]
        self.attempts[r] = self.attempts.get(r, 0) + 1
        attempt = self.attempts[r]
        if sha256_bytes(dump_bytes(self.ctx.requests[r])) != msg["body"].get("request_sha256"):
            raise RuntimeError(f"{r}: the stored request is not the one the buyer sent")
        self.ctx.put(f"requests/{r}.json", self.ctx.requests[r])
        sent = self._wall()
        rc = self.link.call("commit", {"request": req})
        ok = verify_receipt(rc, self.did)
        if rc.get("seq") is not None:
            self.ctx.put(f"receipts/{rc['seq']}.json", rc)
        hold = self._hold_of(rc.get("hold_id"))
        if rc["type"] == "hold" and hold not in self.holds:
            self.holds[hold] = {"real": rc["hold_id"], "seller": seller, "meter_step": meter_step, "w": rc["w"],
                                "buyer": msg["sender"]}
        if rc["type"] == "confirm" and hold not in self.confirmed:
            self.confirmed.append(hold)
        self.clock["commits"].append({"request": r, "attempt": attempt, "sent": sent, "created_at": req["created_at"],
                                      "issued_at": rc.get("issued_at"), "seq": rc.get("seq"), "type": rc["type"],
                                      "expires_at": rc.get("expires_at"), "action": action,
                                      "valid_until": self.assess[self.ctx.requests[r]["assessment_id"]]["valid_until"]
                                      if action == "hold" else None,
                                      "hold_expires_at": self.holds[self.ctx.requests[r]["hold_id"]].get("expires_at")
                                      if action == "confirm" else None})
        if rc["type"] == "hold":
            self.holds[hold]["expires_at"] = rc.get("expires_at")
        granted = int(rc.get("w") or 0) if rc["type"] != "refusal" else 0
        flags = list(rc.get("flags") or [])
        self.api.observe("flex_commit", r, {
            "source": self.source, "action": action, "buyer": msg["sender"],
            "assessment": self.ctx.requests[r].get("assessment_id"), "hold": hold, "meter_step": meter_step,
            "w_requested": req["w"], "outcome": rc["type"], "w_granted": granted, "flags": flags,
            "reason_code": rc.get("reason_code"), "seq": rc.get("seq"), "receipt_sha256": receipt_sha256(rc),
            "request_sha256": sha256_bytes(dump_bytes(self.ctx.requests[r])), "verified": ok, "attempt": attempt})
        self.ctx.live(self.api.now, self.name, f"commit {action} {r} (attempt {attempt}) -> {rc['type']} "
                      f"{granted // 1000} kW seq {rc.get('seq')} {flags or ''}"
                      + (" [replayed the stored receipt]" if attempt > 1 else ""))
        return r, rc, hold, granted, flags

    def handle_flex_hold(self, msg):
        r, rc, hold, granted, flags = self._commit(msg, "hold")
        self.api.reply(msg, "flex_hold_result", {"request": r, "outcome": rc["type"], "hold": hold,
                                                 "w_granted": granted, "flags": flags,
                                                 "reason_code": rc.get("reason_code")})

    def handle_flex_confirm(self, msg):
        r, rc, hold, granted, _ = self._commit(msg, "confirm")
        self.api.reply(msg, "flex_confirm_result", {"request": r, "outcome": rc["type"], "hold": hold})

    def handle_flex_readings(self, msg):
        for h in self.confirmed:
            for attempt in (1, 2):              # 2 = a repeated settle, must return the same receipt
                self._wall()
                d = self.link.call("assess_delivery", {"commitment_id": self.holds[h]["real"]})
                self.ctx.put(f"receipts/{d['seq']}.json", d)
                self.api.observe("flex_delivery", h, {
                    "source": self.source, "seller": self.holds[h]["seller"], "seq": d.get("seq"),
                    "receipt_sha256": receipt_sha256(d), "verdict": d.get("verdict"),
                    "delivered_fraction_bp": d.get("delivered_fraction_bp"),
                    "verified": verify_receipt(d, self.did), "attempt": attempt})
                self.ctx.live(self.api.now, self.name, f"assess_delivery({h}) [{'settle' if attempt == 1 else 'repeat'}]"
                              f" -> {d.get('verdict')} {d.get('delivered_fraction_bp')} bp seq {d.get('seq')}")
            self.api.send(self.holds[h]["buyer"], "flex_settled", {"hold": h, "verdict": d.get("verdict")})
        for s in self.sellers:
            self._trust(s, "post")
            self._trust(s, "repeat")
        self.finish()

    def finish(self):
        snap = self.ctx.op.call("snapshot")
        self.ctx.put("ledger_snapshot.json", snap)
        for row in snap["capacity"]:
            k = f"{row['meter_id']}@{row['start_s']}"
            self.api.observe("flex_capacity", k, {"source": self.source, "assessed_w": row["cap_w"],
                                                  "live_w": int(snap["live_w"].get(k, 0))})
        checks, detail = self._clock_checks()
        self.ctx.put("clock_checks.json", {"checks": checks, "detail": detail})
        self.api.observe("flex_clock", "run", {"source": self.source, "checks": checks})
        self.ctx.live(self.api.now, self.name, f"clock checks {checks}")
        self.link.close()
        self._bind()

    def _clock_checks(self):
        c = self.clock
        fresh_assess = [x for x in c["commits"] if x["attempt"] == 1]
        detail = {"first_assess_wall": c["assess_calls"][0] if c["assess_calls"] else None,
                  "max_interval_end_s": max(c["interval_end_s"]) if c["interval_end_s"] else None,
                  "wall_s": (c["last_wall"] - c["first_wall"]) if c["first_wall"] else None,
                  "budget_s": RUN_BUDGET_S, "freshness_s": FRESHNESS_S,
                  "commits": c["commits"]}
        seqs = [x["seq"] for x in fresh_assess if x["seq"] is not None]
        checks = {
            "delivery_due": bool(c["assess_calls"]) and max(c["interval_end_s"]) <= min(c["assess_calls"]),
            "freshness": all(abs(x["created_at"] - x["issued_at"]) <= FRESHNESS_S
                             and x["sent"] - x["created_at"] <= FRESHNESS_S for x in c["commits"]
                             if x["issued_at"] is not None),
            "assessment_valid": all(x["issued_at"] < x["valid_until"] for x in c["commits"]
                                    if x["action"] == "hold" and x["type"] == "hold"),
            "hold_valid": all(x["issued_at"] < x["hold_expires_at"] for x in c["commits"]
                              if x["action"] == "confirm" and x["type"] == "confirm"),
            "run_budget": detail["wall_s"] is not None and detail["wall_s"] < RUN_BUDGET_S,
            "monotonic_seq": seqs == sorted(seqs) and len(set(seqs)) == len(seqs),
        }
        return checks, detail

    def _bind(self):
        self.ctx.put("mapping.json", {"assessments": {k: v["real"] for k, v in self.assess.items()},
                                      "holds": {v: k for k, v in self.hold_alias.items()},
                                      "offers": {k: v["offer_id"] for k, v in self.ctx.offers.items()}})
        self.ctx.put("provenance.json", json.loads(os.environ.get("BONA_FLEX_PROVENANCE", "{}"))
                     | {"bona_did": self.ctx.info["bona_did"], "source": self.source,
                        "fixture_manifest_sha256": self.ctx.info["fixture_manifest_sha256"]})
        sha = self.ctx.write_sidecar()
        self.api.observe("bona_sidecar_bound", "run", {"source": self.source, "manifest_sha256": sha,
                                                       "files": len(self.ctx.files)})
        self.ctx.live(self.api.now, self.name, f"sidecar written ({len(self.ctx.files)} files), manifest "
                      f"bound in the trace")


@role("flex_desk_naive")
class FlexDeskNaive(_DeskBase):
    """CONTROL: grants every hold in full, no Bona, no signatures; idempotent by request alias only."""
    source = "control_model"

    def _start(self):
        self.seq = 0
        self.by_req: dict[str, dict] = {}
        self.verified = {s: 0 for s in self.sellers}
        self.offered: dict[str, int] = {}
        for s in self.sellers:
            self.api.observe("flex_trust", s, {"source": self.source, "phase": "pre", "deliveries_verified": 0,
                                               "committed_wh": 0, "tier": None})
        self.ctx.live(self.api.now, self.name, "CONTROL desk (no Bona): grants every hold in full")

    def handle_flex_assess(self, msg):
        out = []
        for o in msg["body"]["offers"]:
            off = self.ctx.offers[o]
            al = self.ctx.alias("a")
            k = f"{off['meter']}@{off['interval'][0]}"
            self.offered[k] = max(self.offered.get(k, 0), off["w"])
            self.assess[al] = {"seller": off["seller_id"], "meter": off["meter"], "start_s": off["interval"][0]}
            self.api.observe("flex_assessed", al, {
                "source": self.source, "buyer": msg["body"]["buyer"], "seller": off["seller_id"],
                "meter": off["meter"], "step": off["interval"][0], "offer": o, "offered_w": off["w"],
                "feasible_w": off["w"], "status": "ok"})
            out.append({"assessment": al, "offer": o, "status": "ok", "feasible_w": off["w"]})
        self.api.reply(msg, "flex_assess_result", {"assessments": out})

    def _grant(self, msg, action):
        r = msg["body"]["request"]
        req = self.ctx.requests[r]
        self.attempts[r] = self.attempts.get(r, 0) + 1
        if sha256_bytes(dump_bytes(req)) != msg["body"].get("request_sha256"):
            raise RuntimeError(f"{r}: the stored request is not the one the buyer sent")
        if r not in self.by_req:
            self.seq += 1
            if action == "hold":
                a = self.assess[req["assessment_id"]]
                h = self.ctx.alias("h")
                self.holds[h] = {"seller": a["seller"], "meter_step": f"{a['meter']}@{a['start_s']}",
                                 "w": req["w"], "buyer": msg["sender"]}
            else:
                h = req["hold_id"]
                self.confirmed.append(h)
            self.by_req[r] = {"hold": h, "seq": self.seq}
        g = self.by_req[r]
        self.api.observe("flex_commit", r, {
            "source": self.source, "action": action, "buyer": msg["sender"], "assessment": req.get("assessment_id"),
            "hold": g["hold"], "meter_step": self.holds[g["hold"]]["meter_step"], "w_requested": req["w"],
            "outcome": action, "w_granted": req["w"], "flags": [], "reason_code": None, "seq": None,
            "receipt_sha256": None, "request_sha256": sha256_bytes(dump_bytes(req)), "verified": False,
            "attempt": self.attempts[r]})
        self.ctx.live(self.api.now, self.name, f"grants {action} {r} -> {g['hold']} {req['w'] // 1000} kW "
                      f"(no capacity check, no receipt)")
        return r, g["hold"], req["w"]

    def handle_flex_hold(self, msg):
        r, h, w = self._grant(msg, "hold")
        self.api.reply(msg, "flex_hold_result", {"request": r, "outcome": "hold", "hold": h, "w_granted": w,
                                                 "flags": [], "reason_code": None})

    def handle_flex_confirm(self, msg):
        r, h, _ = self._grant(msg, "confirm")
        self.api.reply(msg, "flex_confirm_result", {"request": r, "outcome": "confirm", "hold": h})

    def handle_flex_readings(self, msg):
        for h in self.confirmed:
            for attempt in (1, 2):
                self.api.observe("flex_delivery", h, {
                    "source": self.source, "seller": self.holds[h]["seller"], "seq": None, "receipt_sha256": None,
                    "verdict": "delivered", "delivered_fraction_bp": 10_000, "verified": False, "attempt": attempt})
            self.verified[self.holds[h]["seller"]] += 1
            self.api.send(self.holds[h]["buyer"], "flex_settled", {"hold": h, "verdict": "delivered"})
        for s in self.sellers:
            for ph in ("post", "repeat"):
                self.api.observe("flex_trust", s, {"source": self.source, "phase": ph,
                                                   "deliveries_verified": self.verified[s], "committed_wh": 0,
                                                   "tier": None})
        live: dict[str, int] = {}
        for h in self.holds.values():
            live[h["meter_step"]] = live.get(h["meter_step"], 0) + h["w"]
        for k in sorted(self.offered):
            self.api.observe("flex_capacity", k, {"source": self.source, "assessed_w": self.offered[k],
                                                  "live_w": live.get(k, 0)})
        self.ctx.put("mapping.json", {"control": True, "holds": sorted(self.holds)})
        self.ctx.put("provenance.json", json.loads(os.environ.get("BONA_FLEX_PROVENANCE", "{}"))
                     | {"source": self.source, "bona_did": None})
        sha = self.ctx.write_sidecar()
        self.api.observe("bona_sidecar_bound", "run", {"source": self.source, "manifest_sha256": sha,
                                                       "files": len(self.ctx.files)})
        self.ctx.live(self.api.now, self.name, "control sidecar written and bound")
