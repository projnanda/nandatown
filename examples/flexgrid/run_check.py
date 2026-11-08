#!/usr/bin/env python3
"""FlexGrid's demand-flex market on a local Town run, every node an agent.

    FLEXGRID_HOME=/path/to/flexgrid python examples/flexgrid/run_check.py --model URL
    ... run_check.py --cases normal,normal_rules --model URL

Each case starts Town's own coordinator, opens one run whose roster is
FlexGrid's coordinator, two buyers, 8 homes and 3 buildings, starts FlexGrid's
processes on it, and plays one market day: order with deposit, call for
offers, offers, clearing, contracts, delivery, readings, settlement, payment.
Every market message goes through Town's mailbox.

There are 8 scenarios, 1 normal and 7 unusual, and each is played two ways:

  * every node an agent (the case's own name): a model decides for all 8
    homes and all 3 buildings at every point, with code's rules as one input
    it may take, change or drop. A rule decides a point only when the model
    fails, and that is counted. Needs --model.
  * every node on rules (NAME_rules): the control, with no model at all.

The clock waits for the agents. At each 15-minute step every clock holds until
every agent has answered, then all of them move one step together, so no
decision is skipped because the model was slow. This is a test and demo
device: a real evening doesn't wait.

Town's `run_town` starts exactly one buyer and one seller, so this file drives
the same pieces itself: the coordinator, then `evaluate`, `write_bundle` and
`attest_bundle` on what the run recorded. Town's stage evaluator knows quotes,
not demand flexibility, so a day closes with one quote: Town's stock buyer asks
FlexGrid's coordinator for its closing statement and compares the answer with
a total frozen in the profile before the day began. A rules day is held to the
money FlexGrid's offline simulator pays on the same day with no Town in it. An
agent's day can't be: what its agents offer is theirs. It is held to the count
of nodes whose day closed on their own readings. Everything else about the day
is judged here, from Town's records and FlexGrid's own ledgers, and is reported
as this script's judgment, not Town's.

FLEXGRID_HOME is never written to: each run works on a staged copy of the code,
with its own config folder, ports and NANDATOWN_HOME.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
NODE = HERE / "flexgrid_node.py"
EVIDENCE = HERE / "evidence"
# What stays as plain files: the bundles the write-up points at, each with what
# `nandatown verify` reads and the signature it checks. Everything else goes in
# one archive, see record().
PLAIN_CASES = ("normal", "normal_rules", "network_failure",
               "network_failure_before")
BUNDLE_FILES = ("manifest.json", "profile.json", "run.json", "intents.jsonl",
                "events.jsonl", "result.json", "attestation.json")
EXTRAS = "extras.tar.gz"

# The community: 8 homes picked to cover the 200 (all 5 towns, all 6 household
# types, gas and heat pump, all 7 battery makes, 1 to 5 people, small to large
# solar and floor area, a pool), and the 3 buildings.
HOMES = ("hh-001", "hh-002", "hh-039", "hh-071", "hh-084", "hh-090", "hh-139",
         "hh-197")
BUILDINGS = ("bld-university", "bld-mall", "bld-office")
NODES = HOMES + BUILDINGS
DATASET, DATA_DIR = "ma_year_t1b", "data_year_t1b"
PRICE, DEPOSIT_SHARE = 2.04, 0.10
ORDERS = {"utility": 2726.52, "grid-operator": 1635.91}   # the live days' kW
BASE = {"utility": "utility", "grid-operator": "operator"}   # each one's API
# The benchmark's rule for sizing an order to a community (benchmark/common.py,
# scorer.size_ask): 95% of its lowest half hour of true relief in the window,
# on its rules day with an order larger than it can fill, split 62.5 / 37.5.
ASK_SHARE, BUYER_SHARE = 0.95, {"utility": 0.625, "grid-operator": 0.375}
SUMMER, WINTER = ("18:00", "21:00"), ("17:00", "19:00")

NOTED = "bld-office"                        # the building with a note on file
NOTE = ("Chiller 2 tripped this morning and stays off until the contractor comes"
        " on Friday. Chiller 1 is carrying the whole building at full load, so"
        " there is no cooling to give up tonight.")
OWNER_NOTE = "Going away tonight, need a full battery"   # benchmark/cases.py
NOTE_HOMES = ("hh-002", "hh-084", "hh-139")  # three battery makes, three towns
CAR_NOW = "hh-001"          # its car is plugged in at 19:00, waiting for 21:30

MODEL_BUDGET_S = 180.0      # model time for one answer
MODEL_TURN = threading.Lock()   # agent days take turns: they share one model
TEMPERATURE, SEED = 0.0, 7  # so the same day gives the same answers, mostly
IDLE_S = 600.0              # a held step that moves nothing this long goes on
OLD_MODEL_STEP = 25.0       # the code of 6 Nov has no waiting clock: its model
QUIET_X = 8                 # days ran at 25 s a step, the quiet hours 8x faster
WATCH_FROM = "15:00"        # back to the full step, before the agents wake
CLOSED = "http://127.0.0.1:9"               # nothing listens there
READER = "statement-reader"                 # Town's stock buyer, on this run
LIVE_PORTS = (set(range(7860, 7890)) | set(range(7970, 7990))
              | {8477, 11434, 11435, 18477, 18789})

STAGES = ("sold", "delivered", "readings_in", "settled", "paid")


def say(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# -- the days and the scenarios ----------------------------------------------

@dataclasses.dataclass(frozen=True)
class Day:
    date: str
    window: tuple[str, str] = SUMMER
    sized: bool = False     # orders sized from its rules day (ASK_SHARE)

    @property
    def next_day(self) -> str:
        d = dt.date.fromisoformat(self.date) + dt.timedelta(days=1)
        return d.isoformat()

    @property
    def slots(self) -> int:
        """Half hours in the window."""
        (h0, m0), (h1, m1) = (map(int, x.split(":")) for x in self.window)
        return ((h1 * 60 + m1) - (h0 * 60 + m0)) // 30

    @property
    def hours(self) -> float:
        return self.slots / 2


NORMAL_DAY = Day("2026-06-29")      # 22nd of the 45 sale evenings by demand
HOT_DAY = Day("2026-07-02")         # the hottest evening, 35.4 C, rank 1 of 45
SNOW_DAY = Day("2026-01-27", WINTER, sized=True)    # a Boston school closure


# o: what was observed on the Town run. r: the same day offline, with no Town
# (flexgrid_node.py reference). n: nodes in the community.
Check = Callable[[dict[str, Any], dict[str, Any], int], bool]
Expect = dict[str, tuple[str, Check]]


@dataclasses.dataclass(frozen=True)
class Case:
    name: str
    scenario: str
    kind: str                        # normal or unusual
    about: str
    day: Day
    arm: str = "agents"              # agents: every node an agent; rules
    events: tuple[dict[str, Any], ...] = ()     # the offline simulator's surprises
    passes: int = 1                  # the day played this many times, one run
    code: str = "current"            # or "baseline": the code of 6 Nov
    note: tuple[str, str] | tuple[()] = ()      # NODE, TEXT: on file before the day

    @property
    def model(self) -> bool:
        return self.arm == "agents"

    @property
    def waits(self) -> bool:
        """The clock waits for the agents: the current code only."""
        return self.code == "current"

    @property
    def exact(self) -> bool:
        """A rules day is the offline day, number for number, but where a call
        is asked during it: offline every node answers before its own step,
        and on Town just after it, so their answers differ a little."""
        return self.arm == "rules" and not any(e["kind"] == "emergency"
                                               for e in self.events)

    @property
    def same_as_offline(self) -> bool:
        """Held to the offline day's money: an exact day played once."""
        return self.exact and self.passes == 1

    @property
    def replayed(self) -> bool:
        return self.passes > 1

    @property
    def verdict(self) -> str:
        """What Town's evaluator should conclude."""
        return "failed" if self.code == "baseline" else "passed"

    @property
    def expect(self) -> Expect:
        if self.code == "baseline":
            return BEFORE
        return NORMAL if self.exact else AGENT_DAY


def near(a: Any, b: Any, tol: float = 0.01) -> bool:
    return a is not None and abs(float(a) - float(b)) <= tol


def slots_of(n: int, r: dict[str, Any]) -> int:
    return r["slots"] * n


def all_readings(o: dict[str, Any], r: dict[str, Any], n: int) -> bool:
    return all(o["readings_in"][who] == slots_of(n, r) for who in
               ("coordinator", "utility", "grid-operator"))


def same_pay(o: dict[str, Any], r: dict[str, Any], but: str = "") -> bool:
    return all(near(o["paid"]["paid_usd"].get(h), usd)
               for h, usd in r["paid_usd"].items() if h != but)


def buyers_pay(o: dict[str, Any]) -> float:
    return sum(o["settled"]["usd"].values())


def under_contract(o: dict[str, Any]) -> int:
    return int(o["sold"]["nodes"] or 0)


NORMAL: Expect = {
    "sold": ("{sold_kwh:,.1f} kWh under contract, all {n} nodes, both orders"
             " active, as offline",
             lambda o, r, n: o["sold"]["nodes"] == n
             and near(o["sold"]["kwh"], r["sold_kwh"], 0.1)
             and o["sold"]["orders_active"] == 2),
    "delivered": ("{delivered_kwh_measured:,.3f} kWh measured,"
                  " {delivered_kwh_true:,.3f} kWh true, as offline",
                  lambda o, r, n:
                  near(o["delivered"]["kwh_measured"],
                       r["delivered_kwh_measured"])
                  and near(o["delivered"]["kwh_true"],
                           r["delivered_kwh_true"])),
    "readings_in": ("{all_slots} at the coordinator and at each buyer",
                    all_readings),
    "settled": ("the buyers pay ${buyers_total:,.2f}, as offline, nothing"
                " lacking, no alarm",
                lambda o, r, n: all(near(o["settled"]["usd"].get(b), usd)
                                    for b, usd in r["buyers_usd"].items())
                and not o["settled"]["lacking"]
                and not o["settled"]["alarm"]),
    "paid": ("${pool_usd:,.2f} to the sellers, each node paid as offline, or"
             " not, and each node's own record agrees",
             lambda o, r, n: same_pay(o, r)
             and set(o["paid"]["paid_usd"]) == set(r["paid_usd"])
             and near(o["paid"]["pool_usd"], r["pool_usd"])
             and not o["paid"]["told_differently"]),
}

# A day the agents decide. What they offer is their own, so neither the sale
# nor the money is held to the offline day: both are reported beside the rules
# day instead. What is held is the network: every agent's answer carried, every
# reading of every node under contract in, the money closed on them.
AGENT_DAY: Expect = {
    "sold": ("every node's agent answered the call, both orders active",
             lambda o, r, n: o["sold"]["offers"] == n
             and o["sold"]["orders_active"] == 2),
    "delivered": ("kWh measured, beside the rules day: the agents' answers"
                  " shape the day",
                  lambda o, r, n: o["delivered"]["kwh_measured"] is not None),
    "readings_in": ("each node under contract, every half hour, at the"
                    " coordinator and at each buyer; nothing refused",
                    lambda o, r, n: all(
                        o["readings_in"][w] == r["slots"] * under_contract(o)
                        for w in ("coordinator", "utility", "grid-operator"))
                    and o["readings_in"]["refused_by_town"] == 0),
    "settled": ("both buyers settled, nothing lacking, no alarm",
                lambda o, r, n: len(o["settled"]["usd"]) == 2
                and not o["settled"]["lacking"]
                and not o["settled"]["alarm"]),
    "paid": ("every node under contract settled on its readings, each node's"
             " own record agrees",
             lambda o, r, n: o["paid"]["settled_or_told"] == under_contract(o)
             and not o["paid"]["told_differently"]),
}

# The code of 6 Nov, every node thinking, the same day played twice on one run:
# batch ids repeat, Town refuses them, and that code takes a refusal as sent.
BEFORE: Expect = {
    "sold": ("both orders active. With no waiting clock a thinking node can miss"
             " a step, so not every node need answer",
             lambda o, r, n: o["sold"]["orders_active"] == 2),
    "delivered": ("less than the rules day offline: refused readings measure"
                  " nothing",
                  lambda o, r, n: (o["delivered"]["kwh_measured"] or 0)
                  < r["delivered_kwh_measured"]),
    "readings_in": ("Town refuses the repeated batch ids, so readings go"
                    " missing, and FlexGrid logs nothing",
                    lambda o, r, n: o["readings_in"]["refused_by_town"] > 0
                    and o["readings_in"]["utility"] < slots_of(n, r)),
    "settled": ("the buyers pay less than the rules day's ${buyers_total:,.2f}",
                lambda o, r, n: buyers_pay(o) < 0.99 * sum(
                    r["buyers_usd"].values())),
    "paid": ("sellers whose batches were refused go unpaid",
             lambda o, r, n: len(o["paid"]["unpaid"]) > 0),
}


def at(day: Day, hm: str) -> str:
    return f"{day.date} {hm}"


SCENARIOS = (
    Case("normal", "normal", "normal",
         "Normal day, Mon 29 Jun 2026: the 22nd of the 45 sale evenings by grid"
         " demand. Weather as measured, steady supply, usual behaviour",
         NORMAL_DAY),
    Case("extreme_heat", "extreme_heat", "unusual",
         "Extreme heat, Thu 2 Jul 2026: the summer's hottest evening (35.4 C),"
         " full sun, the highest grid demand of the 45 (rank 1)", HOT_DAY),
    Case("snow_day", "snow_day", "unusual",
         "Snow day, Tue 27 Jan 2026: a real Boston school closure, so families"
         " and 60% of the others stay home, cars stay home, heat pumps work"
         " hard. Winter test evening 17:00 to 19:00, $2.04, orders sized from"
         " its rules day by the benchmark's rule", SNOW_DAY),
    Case("no_sunlight", "no_sunlight", "unusual",
         "No sunlight, 29 Jun: heavy overcast and rain all day, sunlight 20% of"
         " normal, not forecast, on homes and buildings", NORMAL_DAY,
         events=({"kind": "cloud", "at": at(NORMAL_DAY, "00:00"), "hours": 24,
                  "pv": 0.2},)),
    Case("special_schedules", "special_schedules", "unusual",
         "Special schedules, 29 Jun: the university closes unexpectedly at"
         " noon, and the shopping center holds an evening event from 17:30 in a"
         " quarter of the space it already offered for sale", NORMAL_DAY,
         events=({"kind": "building_closed", "at": at(NORMAL_DAY, "12:00"),
                  "homes": ["bld-university"]},
                 {"kind": "zone_event", "at": at(NORMAL_DAY, "17:30"),
                  "homes": ["bld-mall"], "share": 0.25, "hours": 3.5})),
    Case("unusual_people", "unusual_people", "unusual",
         "Unusual people, 29 Jun: three owners write \"Going away tonight, need"
         " a full battery\" at 17:00, one asks for the car charged now at"
         " 19:00, and the office manager's note about chiller 2 is on file",
         NORMAL_DAY,
         events=({"kind": "note", "at": at(NORMAL_DAY, "17:00"),
                  "homes": list(NOTE_HOMES), "text": OWNER_NOTE},
                 {"kind": "car_now", "at": at(NORMAL_DAY, "19:00"),
                  "homes": [CAR_NOW]}),
         note=(NOTED, NOTE)),
    Case("grid_emergency", "grid_emergency", "unusual",
         "Grid emergency, 29 Jun: the grid operator calls 2,000 kW at 18:45 for"
         " 19:00 to 20:00", NORMAL_DAY,
         events=({"kind": "emergency", "at": at(NORMAL_DAY, "18:45"),
                  "kw": 2000.0, "minutes": 60, "why": "grid emergency",
                  "buyer": "grid-operator"},)),
    Case("network_failure", "network_failure", "unusual",
         "Network failure, 29 Jun: the same day played twice on one Town run,"
         " so Town would hand out a message again under the same id if its"
         " sender reused it", NORMAL_DAY, passes=2),
)

CASES = {c.name: c for c in (
    *SCENARIOS,
    *(dataclasses.replace(c, name=f"{c.name}_rules", arm="rules",
                          about=c.about + ". Control: every node on rules")
      for c in SCENARIOS),
    dataclasses.replace(
        SCENARIOS[-1], name="network_failure_before", code="baseline",
        about="The same failure on the code of 6 Nov, every node thinking:"
              " batch ids repeat across plays, Town refuses them, and that"
              " code takes a refusal as sent. It has no waiting clock, so it"
              " runs at the old model days' pace"),
)}


# -- small tools -----------------------------------------------------------

def free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


PORTS_TAKEN: set[int] = set()
PORTS_LOCK = threading.Lock()
RUNNING: set["Stack"] = set()       # every stack's processes go when this script does
STOPPING = threading.Event()        # and no queued case starts after that


def stop_all(signum: int, frame: Any) -> None:
    STOPPING.set()
    for s in list(RUNNING):
        s.stop()
    raise SystemExit(128 + signum)


def take_ports(start: int, n: int) -> list[int]:
    """n free ports from start up, none another running case holds."""
    with PORTS_LOCK:
        out: list[int] = []
        while len(out) < n:
            if start not in LIVE_PORTS and start not in PORTS_TAKEN \
                    and free(start):
                out.append(start)
            start += 1
        PORTS_TAKEN.update(out)
        return out


def give_ports(ports: list[int]) -> None:
    with PORTS_LOCK:
        PORTS_TAKEN.difference_update(ports)


def wait_http(url: str, what: str, seconds: float = 180.0) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{what} did not answer at {url}")


def git(path: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(path), *args], text=True,
                         capture_output=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def flexgrid_version(home: Path) -> dict[str, Any]:
    """What can be said about a checkout: its git commit if it has one, and
    always a digest of the code that ran."""
    digest = hashlib.sha256()
    for f in sorted((home / "flexgrid_core").rglob("*.py")):
        digest.update(str(f.relative_to(home)).encode())
        digest.update(f.read_bytes())
    pyproject = home / "pyproject.toml"
    m = re.search(r'^version = "(.+)"', pyproject.read_text(), re.M) \
        if pyproject.exists() else None
    return {"package": "flexgrid-core", "version": m.group(1) if m else None,
            "commit": git(home, "rev-parse", "HEAD") or None,
            "uncommitted_changes":
                bool(git(home, "status", "--short", "flexgrid_core")),
            "code_sha256": digest.hexdigest()}


def town_version() -> dict[str, Any]:
    from nandatown import __version__
    from nandatown.evaluator import EVALUATOR_VERSION
    base = git(REPO, "merge-base", "HEAD", "origin/main") \
        or git(REPO, "rev-parse", "HEAD")
    return {"nandatown": __version__, "evaluator": EVALUATOR_VERSION,
            "main_commit": base, "python": sys.version.split()[0],
            "platform": sys.platform}


def model_version(url: str, name: str) -> dict[str, Any]:
    """The name and digest, so "the same model" means the same weights, and
    the sampling every answer used."""
    models = httpx.get(url + "/api/tags", timeout=10).json()["models"]
    return {"name": name, "digest": next((m.get("digest") for m in models
                                          if m.get("name") == name), None),
            "temperature": TEMPERATURE, "seed": SEED}


def signed_ok(body: dict[str, Any]) -> bool:
    """Ed25519 over FlexGrid's canonical JSON, checked here without FlexGrid.
    This shows who signed. It says nothing about whether the content is true."""
    rest = {k: v for k, v in body.items() if k != "sig"}
    text = json.dumps(rest, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(body["signer"]))
        key.verify(bytes.fromhex(body["sig"]), text.encode())
        return True
    except (InvalidSignature, KeyError, ValueError):
        return False


def size_orders(r: dict[str, Any], day: Day) -> dict[str, float]:
    """The benchmark's rule on a rules day with an order larger than it can
    fill: 95% of its lowest half hour of true relief in the window."""
    half = [kw for h, kw in r["delivered_kw_by_half_hour"].items()
            if at(day, day.window[0]) <= h < at(day, day.window[1])]
    if len(half) != day.slots or min(half) <= 0:
        raise RuntimeError(f"no ask from the rules day: half hours {half}")
    ask = min(half) * ASK_SHARE
    return {b: round(ask * s, 2) for b, s in BUYER_SHARE.items()}


# -- one Town run with FlexGrid on it --------------------------------------

class Stack:
    def __init__(self, case: Case, home: Path, work: Path, a: Any,
                 ports: list[int]):
        self.case, self.day, self.home, self.work, self.a = case, case.day, \
            home, work, a
        self.step = a.step if case.waits or not case.model else a.model_step
        self.procs: dict[str, subprocess.Popen] = {}
        self.port = dict(zip(("town", "coordinator", "utility", "operator",
                              "fleet", "buildings"), ports))
        (work / "logs").mkdir(parents=True)
        self.url = {k: f"http://127.0.0.1:{v}" for k, v in self.port.items()}
        self.homes = list(a.homes)
        self.nodes = self.homes + list(BUILDINGS)
        self.orders_kw = dict(ORDERS)
        self.admin_token = secrets.token_hex(16)
        self.admin = httpx.Client(base_url=self.url["town"], timeout=30,
                                  headers={"X-Town-Admin": self.admin_token})
        self.http = httpx.Client(timeout=30)
        self.code = self.stage_code()
        self.events_posted: list[dict[str, Any]] = []
        RUNNING.add(self)

    def stage_code(self) -> Path:
        """A private copy of the package, data linked. FlexGrid writes logs
        beside its code, and nothing may land in the checkout under test."""
        to = self.work / "flexgrid"
        to.mkdir()
        shutil.copytree(self.home / "flexgrid_core", to / "flexgrid_core",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (to / "tools").mkdir()
        for name in ("sim_event.py", "market_day.py", "scorecard.py"):
            shutil.copy(self.home / "tools" / name, to / "tools" / name)
        for name in (DATA_DIR, "data", "models"):
            if (self.home / name).exists():
                (to / name).symlink_to((self.home / name).resolve())
        (to / "logs").mkdir()
        return to

    def orders(self) -> list[dict[str, Any]]:
        d = self.day
        return [{"to": buyer, "date": d.date, "start": d.window[0],
                 "end": d.window[1], "kw": kw, "price_usd_per_kwh": PRICE,
                 "note": "Town run", "buyer": f"Control Room ({buyer})",
                 "deposit_usd": round(DEPOSIT_SHARE * PRICE * kw * d.hours, 2)}
                for buyer, kw in self.orders_kw.items()]

    def offline_day(self, name: str) -> dict[str, Any]:
        """FlexGrid's offline simulator on the same community, orders and
        surprises, every node on rules. Its day has no Town in it, so what it
        pays is a fair expectation for a rules day on Town."""
        folder = self.work / "config" / name
        folder.mkdir(parents=True)
        out = self.work / f"{name}.json"
        env = {**self.flexgrid_env(folder), "FLEXGRID_REFERENCE": json.dumps(
            {"day": self.day.date, "homes": self.homes, "orders": self.orders(),
             "buildings": list(BUILDINGS), "surprises": list(self.case.events)})}
        p = self.launch(name, [sys.executable, str(NODE), "reference",
                               str(out)], env)
        if p.wait(timeout=900) != 0:
            raise RuntimeError(f"the offline day failed: see logs/{name}.log")
        del self.procs[name]
        return json.loads(out.read_text())

    def run_reference(self) -> None:
        """The offline day, and on a day whose orders are sized, first the
        rules day they are sized from."""
        if self.day.sized:
            self.sized_from = self.offline_day("sizing")
            self.orders_kw = size_orders(self.sized_from, self.day)
        self.reference = {**self.offline_day("reference"),
                          "slots": self.day.slots}

    # -- Town -----------------------------------------------------------
    def env(self) -> dict[str, str]:
        keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "HOME", "SYSTEMROOT")
        return {**{k: os.environ[k] for k in keep if k in os.environ},
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
                "NANDATOWN_HOME": os.environ["NANDATOWN_HOME"],
                "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}

    def launch(self, name: str, cmd: list[str], env: dict[str, str],
               cwd: Path | None = None) -> subprocess.Popen:
        log = open(self.work / "logs" / f"{name}.log", "ab", buffering=0)
        p = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                             cwd=str(cwd or self.code), start_new_session=True,
                             stdin=subprocess.DEVNULL)
        self.procs[name] = p
        return p

    def start_town(self) -> None:
        self.launch("town", [sys.executable, "-m", "nandatown.coordinator",
                             "--db", str(self.work / "town.db"),
                             "--port", str(self.port["town"])],
                    {**self.env(), "TOWN_ADMIN_TOKEN": self.admin_token},
                    cwd=self.work)
        wait_http(self.url["town"] + "/health", "Town")

    def open_run(self) -> None:
        from nandatown.identity_portable import Keystore, default_keystore_dir
        from nandatown.records import TestProfile

        roles = {READER: "buyer", "coordinator": "seller",
                 **{b: "flex-buyer" for b in ORDERS},
                 **{h: "home" for h in self.homes},
                 **{b: "building" for b in BUILDINGS}}
        cents = round(100 * self.reference["pool_usd"])
        n = len(self.nodes)
        # A rules day played once is held to the offline day's money. Any other
        # is held to its count of nodes whose day closed, one "cent" each.
        task = ({"sku": "flexgrid-sellers-paid", "quantity": 1,
                 "unit_price_cents": cents, "expected_total_cents": cents}
                if self.case.same_as_offline else
                {"sku": "flexgrid-nodes-closed", "quantity": n,
                 "unit_price_cents": 1, "expected_total_cents": n})
        self.profile = TestProfile(
            name="flexgrid-market-day", task={"kind": "quote", **task},
            roles=roles, capabilities={"coordinator": ["quote.read"]},
            fault="none", lease_seconds=30.0, evaluator="stage-evaluator")
        keys = Keystore(default_keystore_dir())
        identities = {name: {k: v for k, v in keys.new_identity(name).items()
                             if k in ("agent_id", "controller_public")}
                      for name in roles}
        r = self.admin.post("/runs", json={"profile": self.profile.model_dump(),
                                           "identities": identities})
        r.raise_for_status()
        self.run_id, self.tokens = r.json()["run_id"], r.json()["join_tokens"]
        self.grants = {n_: keys.make_grant(n_, self.run_id) for n_ in roles}

    # -- FlexGrid --------------------------------------------------------
    def common(self) -> dict[str, Any]:
        wall = self.step / 60.0       # FlexGrid's real-time waits, at a step
        # Every clock waits at midnight for /demo/start. A looping clock follows
        # the wall, so it can boot past 10:00, when the coordinator asks the
        # buyers about tomorrow, and an answer landing after the reset leaves it
        # asking again all through the first day.
        c = {"town_url": self.url["town"], "town_run_id": self.run_id,
             "replay_start": f"{self.day.date} 00:00", "replay_loop_days": 0,
             "replay_step_seconds": self.step,
             "control_mode": "rolling", "forecast": "temp_regression",
             "langflow_url": CLOSED, "openclaw_url": CLOSED, "flows": {},
             "llm_runtime": "ollama", "llm_enabled": False, "llm_url": CLOSED,
             "buyers": list(ORDERS), "report_rule": "exception",
             "call_settle_after_min": 120,
             "town_poll_seconds": 0.5, "town_poll_idle_seconds": 0.5,
             "fleet_poll_seconds": 0.5, "fleet_poll_idle_seconds": 0.5,
             "utility_retry_wall_seconds": 60 * wall,
             "discover_retry_wall_seconds": 30 * wall,
             "confirm_retry_wall_seconds": 120 * wall,
             "offers_retry_wall_seconds": 120 * wall,
             "readings_chase_wall_seconds": 120 * wall,
             "readings_quiet_s": 30 * wall, "utility_settle_quiet_s": 30 * wall}
        return c

    def model_config(self) -> dict[str, Any]:
        """For the nodes only: the coordinator and the buyers stay code. Every
        node an agent: the model at every point, at temperature 0 with a fixed
        seed (all_agents, llm_temperature, llm_seed). The code of 6 Nov knows
        none of these three, so it gets thinking nodes as it knew them."""
        if not self.case.model:
            return {"thinking_homes": 0}
        return {"llm_enabled": True, "llm_url": self.a.model,
                "llm_model": self.a.model_name, "llm_slots": 1,
                "llm_offer_budget_s": MODEL_BUDGET_S,
                "llm_accept_budget_s": MODEL_BUDGET_S,
                "thinking_homes": list(self.homes), "all_agents": True,
                "llm_temperature": TEMPERATURE, "llm_seed": SEED}

    def folder(self, name: str, cfg: dict[str, Any],
               secrets_: dict[str, str]) -> Path:
        d = self.work / "config" / name
        (d / "secrets").mkdir(parents=True)
        os.chmod(d / "secrets", 0o700)
        for k, v in cfg.items():            # a last look before anything starts
            if k == "llm_url":              # read only, and given by --model
                continue
            for port in LIVE_PORTS:
                if re.search(rf":{port}\b", str(v)):
                    raise SystemExit(f"{name}: {k} points at a live port")
        (d / "agent.json").write_text(json.dumps(cfg, indent=1))
        for k, v in secrets_.items():
            (d / "secrets" / k).write_text(v)
            os.chmod(d / "secrets" / k, 0o600)
        return d

    def flexgrid_env(self, folder: Path) -> dict[str, str]:
        return {**self.env(), "PYTHONPATH": str(self.code),
                "FLEXGRID_CONFIG_DIR": str(folder), "FLEXGRID_DATASET": DATASET,
                "FG_BATTERY_MODE": "backup", "FG_MARKET": "2026",
                "FG_DEVICES": "hpwh,pool,dryer,dish,v2h",
                "FLEXGRID_COORDINATOR_URL": self.url["coordinator"],
                "FLEXGRID_UTILITY_URL": self.url["utility"]}

    def write_configs(self) -> None:
        p, u = self.port, self.url

        def held(names: Any) -> str:
            return json.dumps({n: self.grants[n] for n in names})

        def tokens(names: Any) -> str:
            return json.dumps({n: self.tokens[n] for n in names})

        mac = {**self.common(), "role": "coordinator",
               "agent_name": "coordinator", "coordinator_port": p["coordinator"],
               "utility_port": p["utility"], "buildings": list(BUILDINGS),
               "monitor_fleet_url": u["fleet"], "monitor_tesla_url": CLOSED,
               "monitor_enphase_url": CLOSED, "monitor_dc_url": CLOSED,
               "monitor_datacenters": False, "dc_port": 9,
               "monitor_building_urls": {b: f"{u['buildings']}/b/{b}"
                                         for b in BUILDINGS},
               "v2_roster": self.homes, "buyer_agent": False,
               # the clock waits for the agents, the second buyer's too
               "clock_waits": self.case.waits, "clock_wait_idle_s": IDLE_S,
               "clock_extra_targets": {"grid-operator": u["operator"]}}
        self.mac = self.folder("mac", mac, {
            "town_token": self.tokens["coordinator"],
            "utility_town_token": self.tokens["utility"],
            "grid-operator_town_token": self.tokens["grid-operator"],
            "town_grants.json": held(["coordinator", *ORDERS])})
        out = subprocess.run(
            [sys.executable, "-m", "flexgrid_core.coordinator", "pubkey"],
            env=self.flexgrid_env(self.mac), cwd=str(self.code),
            capture_output=True, text=True, check=True)
        pub = out.stdout.strip().splitlines()[-1]
        # Both buyers sign with one key, as live. Made here, or the second to
        # start can read the file while the first is writing it.
        subprocess.run([sys.executable, "-m", "flexgrid_core.utility", "pubkey"],
                       env=self.flexgrid_env(self.mac), cwd=str(self.code),
                       capture_output=True, check=True)
        nodes = {**self.common(), "coordinator_pubkeys": [pub],
                 "fleet_poll_workers": 4, "send_workers": 4,
                 "fleet_sweep_seconds": 10 * self.step,
                 **self.model_config()}
        self.fleet = self.folder(
            "fleet", {**nodes, "role": "fleet", "agent_name": "fleet",
                      "fleet_port": p["fleet"]},
            {"fleet_tokens.json": tokens(self.homes),
             "town_grants.json": held(self.homes)})
        self.bld = self.folder(
            "buildings", {**nodes, "role": "buildings", "fleet_poll_workers": 3,
                          "agent_name": "buildings", "send_workers": 3,
                          "buildings_port": p["buildings"],
                          "buildings": list(BUILDINGS)},
            {"fleet_tokens.json": tokens(BUILDINGS),
             "town_grants.json": held(BUILDINGS)})
        if self.case.note:          # what a manager's set_policy leaves on disk
            node, text = self.case.note
            (self.bld / "state").mkdir()
            (self.bld / "state" / f"{node}_policy.json").write_text(
                json.dumps({"notes": [text]}))

    def start_flexgrid(self) -> None:
        py, node = sys.executable, str(NODE)
        self.launch("coordinator", [py, node, "coordinator"],
                    self.flexgrid_env(self.mac))
        for buyer in ORDERS:
            self.launch(buyer, [py, node, "buyer", buyer, "--port",
                                str(self.port[BASE[buyer]])],
                        self.flexgrid_env(self.mac))
        self.launch("fleet", [py, node, "nodes"],
                    {**self.flexgrid_env(self.fleet),
                     "FLEXGRID_NODES": ",".join(self.homes)})
        self.launch("buildings", [py, node, "nodes", "--buildings"],
                    self.flexgrid_env(self.bld))
        for what in ("coordinator", "utility", "operator", "fleet",
                     "buildings"):
            wait_http(self.url[what] + "/health", what)

    # -- the day ---------------------------------------------------------
    def post(self, base: str, path: str, body: dict[str, Any]) -> httpx.Response:
        r = self.http.post(self.url[base] + path, json=body)
        if r.status_code >= 400:
            say(f"[{self.case.name}] {base} refused {path}: {r.status_code}"
                f" {r.text[:300]}")
        r.raise_for_status()
        return r

    def get(self, base: str, path: str) -> Any:
        r = self.http.get(self.url[base] + path)
        r.raise_for_status()
        return r.json()

    def gates(self) -> dict[str, list[dict[str, Any]]]:
        """Each step at which something happens, with what happens then."""
        out: dict[str, list[dict[str, Any]]] = {}
        for e in self.case.events:
            out.setdefault(e["at"], []).append(e)
        return out

    def start_day(self) -> None:
        """FlexGrid's own start: /demo/start reaches every clock the
        coordinator holds; each order goes to its buyer with its deposit. With
        the waiting clock, the coordinator is told first at which steps to stop
        and wait for this script, so each event lands on its own step, as in
        the offline day."""
        for base in ("utility", "operator"):
            self.post(base, "/demo/reset", {"clear_orders": True})
        self.pending = self.gates()
        if self.case.waits:
            self.post("coordinator", "/demo/conductor",
                      {"gates": sorted(self.pending)})
        # The code of 6 Nov has no waiting clock. On its model days its own pace
        # door runs the quiet hours faster, holds the full step while the market
        # talks, and follow() asks for it from WATCH_FROM on.
        slow = self.case.model and not self.case.waits
        start = {"event": self.day.date, "step_seconds": self.step, "force": True,
                 "lead_s": 6, "buyer_agent": False,
                 **({"pace": {"x": QUIET_X}} if slow else {})}
        # A play's clock goes on stepping after the play has ended. A step that
        # falls while /demo/start is moving the clocks puts the ones it has
        # moved back on the old line, and the buyer then refuses the order as
        # too late. /demo/start itself stops that clock for its lead, so a
        # second call moves every clock with nothing else moving them.
        if self.case.waits and getattr(self, "clock", None):
            self.post("coordinator", "/demo/start", start)
            time.sleep(0.5)
        st = self.post("coordinator", "/demo/start", start).json()
        lost = {k: v for k, v in st["agents"].items()
                if str(v).startswith("not reached")
                and not k.startswith("home-")}
        if lost:
            raise RuntimeError(f"not every node took the clock: {lost}")
        self.clock = {"start": st["starts_at"], "at": st["starts_at"],
                      "at_wall": st["wall_start"], "loop_days": 0,
                      "step_seconds": self.step}
        self.pace_seen = time.time()
        if not self.case.waits:       # that code doesn't hold the second buyer
            self.post("operator", "/demo/clock", self.clock)
        for order in self.orders():
            self.post(BASE[order["to"]], "/needs", order)

    def post_events(self, when: str, events: list[dict[str, Any]]) -> None:
        """What happens at this step, through each service's own what-if door,
        while every clock waits one step before it. Each one goes on Town's
        record as the runner's observation too."""
        for e in events:
            k, homes = e["kind"], e.get("homes") or []
            if k == "cloud":            # every node, the forecasts not knowing
                for base in ("fleet", "buildings"):
                    self.post(base, "/demo/scenario", {"weather": {
                        "pv": e["pv"], "from": when, "hours": e["hours"]}})
            elif k == "building_closed":
                for b in homes:
                    self.post("buildings", f"/b/{b}/demo/scenario",
                              {"closed": {"at": when}})
            elif k == "zone_event":
                for b in homes:
                    self.post("buildings", f"/b/{b}/demo/scenario",
                              {"zone_event": {"at": when, "share": e["share"],
                                              "hours": e["hours"]}})
            elif k == "note":           # from the owner's phone
                for h in homes:
                    self.post("fleet", "/demo/scenario", {"override": {
                        "home": h, "kind": "note", "value": e["text"],
                        "source": "owner's phone"}})
            elif k == "car_now":
                for h in homes:
                    self.post("fleet", "/demo/scenario", {"override": {
                        "home": h, "kind": "ev_now", "value": True,
                        "source": "owner's phone"}})
            elif k == "emergency":      # as live_bench.py emergency sends it
                self.post(BASE[e["buyer"]], "/alert", {
                    "kw": e["kw"], "minutes": e["minutes"], "why": e["why"],
                    "at": when})
            else:
                raise RuntimeError(f"no way to post {k} on a Town run")
            self.observe("scenario_event", k, e)
            self.events_posted.append({**e, "posted_at_step": when})
            say(f"[{self.case.name}] {when} {k} {homes or ''}")

    def mirror_pace(self) -> None:
        """The code of 6 Nov slows its clock while readings are due, but tells
        only its own targets. FlexGrid's live script passes each change on to
        the second buyer; this does the same."""
        for ch in self.get("coordinator", "/demo/pace").get("changes") or []:
            if float(ch.get("wall") or 0) > self.pace_seen:
                self.pace_seen = float(ch["wall"])
                self.clock = {"start": ch["sim"], "at": ch["sim"],
                              "at_wall": ch["wall"], "loop_days": 0,
                              "step_seconds": ch["step_seconds"]}
                self.post("operator", "/demo/clock", self.clock)

    def observe(self, kind: str, subject: str,
                detail: dict[str, Any] | None = None) -> None:
        self.admin.post(f"/runs/{self.run_id}/events", json={
            "observer": "runner", "kind": kind, "subject": subject,
            "detail": detail or {}}).raise_for_status()

    def wall_limit(self) -> float:
        """At the run's pace, plus what agents may hold the clock."""
        steps = 170 * self.step * 4 if not self.case.waits else 170 * self.step
        return steps + (4 * 3600 if self.case.model else 900)

    def follow(self) -> str:
        """Until the money is final: every seller settled and nothing still
        owed, or the clock has run far past the window with something owed."""
        limit = time.time() + self.wall_limit()
        seen, settled_at = None, None
        watching = self.case.waits or not self.case.model
        end = f"{self.day.next_day} 03:00"
        while time.time() < limit:
            time.sleep(0.5)
            for name, p in self.procs.items():
                if p.poll() is not None:
                    raise RuntimeError(f"{name} stopped with {p.returncode}")
            if self.case.waits:
                gate = self.get("coordinator", "/demo/conductor").get("at_gate")
                if gate and gate in self.pending:
                    self.post_events(gate, self.pending.pop(gate))
                    self.post("coordinator", "/demo/conductor", {"open": gate})
            else:
                self.mirror_pace()
            s = self.get("coordinator", "/v2/summary")
            now = s.get("now") or ""
            ev = next(iter((s.get("events") or {}).values()), None) or {}
            if not watching and now >= at(self.day, WATCH_FROM):
                watching = True
                self.post("coordinator", "/demo/pace", {"x": 1})
            line = (ev.get("phase"), ev.get("offers"), ev.get("accepted"),
                    ev.get("readings"))
            if line != seen:
                say(f"[{self.case.name}] {now} phase {line[0]} offers {line[1]}"
                    f" accepted {line[2]} readings {line[3]}")
                seen = line
            if ev.get("phase") in ("failed", "no_offer"):
                return ev["phase"]
            if ev.get("phase") == "done":
                settled_at = settled_at or time.time()
                quiet = time.time() - settled_at >= 8 * self.step
                if quiet and not ev.get("readings_missing"):
                    return "settled"
            if now >= end:
                return ("settled, with an alarm standing"
                        if ev.get("phase") == "done" else "not settled")
        return "timed out"

    def ledger(self) -> dict[str, Any]:
        """What FlexGrid's own parties hold about the day, read from their
        status APIs."""
        s = self.get("coordinator", "/v2/summary")
        eid, ev = next(iter(s["events"].items()))
        contracts, evidence = {}, {}
        for buyer, base in BASE.items():
            mine = self.get(base, "/contracts")       # the day-ahead order's
            contracts[buyer] = next((c for c in mine.values()
                                     if c.get("event_id") == eid),
                                    next(iter(mine.values())))
            evidence[buyer] = self.get(
                base, f"/evidence/{contracts[buyer]['event_id']}")
        told = {}
        for h in self.homes:
            told[h] = self.get("fleet", f"/homes/{h}")["payments"]
        for b in BUILDINGS:
            told[b] = self.get("buildings", f"/b/{b}/v2/status")["payments"]
        full = self.get("coordinator", f"/v2/events/{eid}")
        return {"event_id": eid, "summary": ev, "contracts": contracts,
                "meter_readings": {b: e["meter_readings"]
                                   for b, e in evidence.items()},
                "told": told,
                "calls": {b: self.get(base, "/calls") if self.has(base, "/calls")
                          else None for b, base in BASE.items()},
                "conductor": self.get("coordinator", "/demo/conductor")
                if self.case.waits else None,
                "order_terms": {
                    b: {k: o.get(k) for k in ("deposit_usd", "deposit_held_usd",
                                              "deposit_back_usd")}
                    for b, o in (full.get("orders") or {}).items()}}

    def has(self, base: str, path: str) -> bool:
        try:
            return self.http.get(self.url[base] + path).status_code == 200
        except httpx.HTTPError:
            return False

    def agents_now(self) -> dict[str, Any]:
        """Each node's agent as its service sees it: answers that fell back to
        a rule, flags, and every receipt and message to its person so far."""
        if not self.case.model or not self.case.waits:
            return {}
        out: dict[str, Any] = {}
        for base in ("fleet", "buildings"):
            for n, a in (self.get(base, "/agents").get("nodes") or {}).items():
                rc = self.get(base, f"/agents/{n}/receipts")
                out[n] = {"fallbacks": rc.get("fallbacks") or {},
                          "flags": rc.get("flags") or {},
                          "steps_skipped": a.get("steps_skipped"),
                          "receipts": rc.get("receipts") or [],
                          "told": rc.get("told") or []}
        return out

    def model_answers(self) -> dict[str, Any]:
        """Every answer the model gave, each receipt and each guard verdict,
        from each node process's own logs. The processes have stopped, so the
        logs are whole."""
        got: dict[str, Any] = {"answers": [], "chains": {}, "receipts": [],
                               "told": [], "verdicts": []}
        for name, folder in (("fleet", self.fleet), ("buildings", self.bld)):
            file = self.work / f"answers-{name}.json"
            p = self.launch(f"answers-{name}", [
                sys.executable, str(NODE), "answers", str(file)],
                self.flexgrid_env(folder))
            if p.wait(timeout=120) != 0:
                raise RuntimeError(f"see logs/answers-{name}.log")
            del self.procs[f"answers-{name}"]
            one = json.loads(file.read_text())
            for k in ("answers", "receipts", "told", "verdicts"):
                got[k] += one.get(k) or []
            got["chains"].update(one["chains"])
        got["answers"].sort(key=lambda r: (r["sim"] or "", r["node"]))
        return got

    def read_statement(self) -> int:
        """Town's stock buyer, unchanged, on the coordinator's quote_request
        adapter. It compares the answer with the profile's frozen total."""
        env = {**self.env(), "TOWN_URL": self.url["town"], "RUN_ID": self.run_id,
               "NAME": READER, "TOKEN": self.tokens[READER], "DEADLINE": "40",
               "STATE_DIR": str(self.work / "reader"),
               "TOWN_GRANT": json.dumps(self.grants[READER])}
        os.makedirs(env["STATE_DIR"])
        return subprocess.run([sys.executable, "-m",
                               "nandatown.participants.buyer"], env=env,
                              cwd=str(self.work), timeout=90,
                              stdout=subprocess.DEVNULL).returncode

    def stop(self) -> None:
        RUNNING.discard(self)
        running = [p for p in self.procs.values() if p.poll() is None]
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for p in running:
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    pass
            t0 = time.time()
            while time.time() - t0 < 5 and any(p.poll() is None
                                               for p in running):
                time.sleep(0.1)

    def export(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        self.admin.post(f"/runs/{self.run_id}/finish").raise_for_status()
        return (self.admin.get(f"/runs/{self.run_id}/events").json()["events"],
                self.admin.get(f"/runs/{self.run_id}/intents").json()["intents"])


# -- reading the day back --------------------------------------------------

def town_view(events: list[dict[str, Any]], intents: list[dict[str, Any]],
              since: float, until: float = float("inf")) -> dict[str, Any]:
    """The day as Town recorded it: every body Town accepted in this span."""
    accepted = {e["subject"] for e in events if e["kind"] == "message_accepted"
                and since <= e["at"] < until}
    sent: dict[str, dict[str, Any]] = {}
    for i in intents:
        p = i["payload"]
        if i["action"] == "send" and p.get("message_id") in accepted \
                and p["message_id"] not in sent and since <= i["at"] < until:
            sent[p["message_id"]] = {**p, "from": i["actor"]}
    v: dict[str, Any] = {"messages": len(sent), "offers": {}, "offer_rounds": {},
                         "answers": {}, "readings": {}, "readings_by_event": {},
                         "settlements": {},
                         "payments": {}, "calls": {},
                         "signers": {}, "bad_signatures": 0, "by_kind": dict(
                             Counter(m["kind"] for m in sent.values()
                                     if m["kind"].startswith("beckn.")))}
    for m in sent.values():
        body, who = m["body"], m["from"]
        msg = body.get("message") or {}
        if "sig" in body:
            if not signed_ok(body):
                v["bad_signatures"] += 1
            v["signers"].setdefault(who, set()).add(body.get("signer"))
        if m["kind"] == "beckn.on_discover" and "offer" in msg:
            v["offers"][who] = msg["offer"]         # the last one stands
            v["offer_rounds"].setdefault(who, []).append(msg["offer"])
        if m["kind"] == "beckn.on_confirm" and "contract" in msg \
                and who not in ORDERS:
            v["answers"][who] = msg["contract"].get("status")
        if m["kind"] == "beckn.rt_call" and "call" in msg:
            v["calls"][msg["call"].get("id")] = msg["call"]
        if m["kind"] == "beckn.on_status":
            inner = msg.get("batch") or ([body] if "performance" in msg else [])
            for b in inner:
                r = (b.get("message") or {}).get("performance") or {}
                if "slot_id" not in r:
                    continue
                if not signed_ok(b):
                    v["bad_signatures"] += 1
                eid = str(r.get("event_id") or "")
                v["readings_by_event"].setdefault(m["to"], {}).setdefault(
                    eid, {})[(who, int(r["slot_id"]))] = r
                if not eid.startswith("call-"):     # a call's own readings
                    v["readings"].setdefault(m["to"], {})[
                        (who, int(r["slot_id"]))] = r
            s = msg.get("settlement")
            if s and who in ORDERS:
                v["settlements"].setdefault(who, []).append(s)
            elif s and who == "coordinator":
                v["payments"].setdefault(m["to"], []).append(s)
    v["refused"] = [e for e in events if since <= e["at"] < until
                    and e["kind"] == "identity_reuse_rejected"]
    return v


def refused_readings(contract: dict[str, Any]) -> set[tuple[str, int]]:
    """Readings this buyer turned down after Town had carried them to it."""
    return {(h, int(slot)) for h, rows in
            (contract.get("rejected_slots") or {}).items() for slot in rows}


def observed(led: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
    ev, nodes = led["summary"], list(led["told"])
    res = ev.get("result") or {}
    split = res.get("split") or {}
    per = split.get("per_home") or {}
    by = res.get("by_buyer") or {}
    sold = sum((ev.get("capacity_offered_kw") or {}).values()) * 0.5
    told_last = {h: [p for p in rows if p.get("event_id") == led["event_id"]]
                 for h, rows in led["told"].items()}
    o = {"sold": {"nodes": ev.get("accepted"), "kwh": round(sold, 1),
                  "offers": ev.get("offers"), "deposits": led["order_terms"],
                  "orders_active": sum(c.get("status") in ("ACTIVE", "COMPLETE")
                                       for c in led["contracts"].values())},
         "delivered": {"kwh_measured": res.get("delivered_kwh_measured"),
                       "kwh_true": res.get("delivered_kwh_true"),
                       "by_buyer": {b: x.get("delivered_kwh")
                                    for b, x in by.items()},
                       "by_node": {h: round(sum(float(p.get("delivered_kwh")
                                                      or 0) for p in rows), 3)
                                   for h, rows in told_last.items()}},
         "readings_in": {"expected": ev.get("readings_expected"),
                         "coordinator": ev.get("readings"),
                         **{b: sum(len(s) for s in m.values()) for b, m in
                            led["meter_readings"].items()},
                         "chases": ev.get("chases", 0),
                         "refused_by_town": len(v["refused"]),
                         "refused_by_meter_check": {
                             "coordinator": ev.get("readings_refused", 0),
                             **{b: len(refused_readings(c)) for b, c in
                                led["contracts"].items()}}},
         "settled": {"usd": {b: c["settlement"]["total"]
                             for b, c in led["contracts"].items()
                             if c.get("settlement")},
                     "seller_usd": res.get("seller_usd"),
                     "deposits": {b: c["settlement"].get("deposit")
                                  for b, c in led["contracts"].items()
                                  if c.get("settlement")},
                     "lacking": {b: c["settlement"]["lacking"]
                                 for b, c in led["contracts"].items()
                                 if (c.get("settlement") or {}).get("lacking")},
                     "alarm": ev.get("readings_missing") or {}},
         "paid": {"sellers_settled": len(per), "pool_usd": split.get("pool"),
                  "fee_usd": split.get("fee"),
                  "paid_usd": per,
                  # under contract, and paid or told by the coordinator why not
                  "settled_or_told": len(set(per)
                                         | set(res.get("not_counted_homes")
                                               or [])),
                  "not_counted": res.get("not_counted_homes") or [],
                  "unpaid": sorted(set(nodes) - set(per)),
                  "told_differently": sorted(
                      h for h, x in per.items()
                      if abs(sum(float(p.get("payment") or 0)
                                 for p in told_last[h]) - x) > 0.02)}}
    return o


def cross_checks(led: dict[str, Any], v: dict[str, Any]) -> list[dict[str, Any]]:
    """Town's record against FlexGrid's ledgers, party by party."""
    ev = led["summary"]
    out = []

    def check(name: str, ok: bool, detail: Any) -> None:
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    check("every offer the coordinator counted is on Town's record",
          ev.get("offers") == len(v["offers"]),
          {"coordinator": ev.get("offers"), "town": len(v["offers"])})
    check("every accepted contract is a node's own answer on Town's record",
          ev.get("accepted") == sum(s == "ACCEPTED"
                                    for s in v["answers"].values()),
          {"coordinator": ev.get("accepted"),
           "town": dict(Counter(v["answers"].values()))})
    for buyer, meters in led["meter_readings"].items():
        held = {(h, int(s)): x for h, rows in meters.items()
                for s, x in rows.items()}
        eid = led["contracts"][buyer].get("event_id")
        town = (v["readings_by_event"].get(buyer) or {}).get(eid) \
            or v["readings"].get(buyer, {})
        refused = refused_readings(led["contracts"][buyer])
        same = set(held) | refused == set(town) and not set(held) & refused \
            and all(abs(float(town[k]["usage_kw"]) - float(held[k])) < 1e-6
                    for k in held)
        check(f"{buyer} counted the signed readings Town carried to it, but"
              " for those it refused", same,
              {"buyer": len(held), "refused": len(refused), "town": len(town)})
    for buyer, c in led["contracts"].items():
        rounds = v["settlements"].get(buyer) or []
        mine = [s for s in rounds if s.get("event_id") in (None, c.get("event_id"))
                or s.get("order") == c.get("event_id")] or rounds
        last = max(mine, key=lambda s: int(s.get("top_up") or 0), default={})
        check(f"{buyer}'s settlement on Town's record equals its ledger",
              abs(float(last.get("total") or 0)
                  - float((c.get("settlement") or {}).get("total") or 0)) < 1e-6,
              {"town": last.get("total"),
               "ledger": (c.get("settlement") or {}).get("total")})
    per = (((ev.get("result") or {}).get("split") or {}).get("per_home")) or {}
    town_paid = {h: round(sum(float(s.get("payment") or 0) for s in rows
                              if s.get("event_id") == led["event_id"]), 4)
                 for h, rows in v["payments"].items()}
    wrong = {h: (x, town_paid.get(h, 0.0)) for h, x in per.items()
             if abs(town_paid.get(h, 0.0) - x) > 0.02}
    check("each payment notice on Town's record sums to the split", not wrong,
          wrong or {"nodes": len(per)})
    check("every signature on Town's record verifies",
          v["bad_signatures"] == 0, {"bad": v["bad_signatures"]})
    check("one key per sender through the day",
          all(len(k) == 1 for k in v["signers"].values()),
          {h: len(k) for h, k in v["signers"].items() if len(k) != 1})
    return out


NO_REASON = "no reason given"       # flexgrid_core/node/agent.py
WIRE_KEYS = {"decided_by", "decision", "round"}


def agent_checks(case: Case, v: dict[str, Any], got: dict[str, Any],
                 now: dict[str, Any], nodes: list[str]) -> list[dict[str, Any]]:
    """Every node an agent, against what Town carried. Whether an answer was
    wise is not judged here: that it was the agent's, that the coordinator got
    only its amount, and that a cut came with a reason the person heard."""
    out = []

    def check(name: str, ok: bool, detail: Any) -> None:
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    rows = got["answers"]
    check("every model answer says which node, which step, what and how long",
          rows and all(r["node"] and r["step"] and r["ms"] is not None
                       and (r["decided"] is not None or r["error"])
                       for r in rows), {"answers": len(rows)})
    check("each node's decision log is an unbroken chain",
          set(got["chains"]) == set(nodes)
          and all(c["ok"] for c in got["chains"].values()), got["chains"])
    last = {n: (now.get(n) or {}).get("receipts") or [] for n in nodes}
    wrong = {}
    for n in nodes:                 # each node's offer, against its receipt
        offers = [r for r in last[n] if r["point"] in ("offer", "offer_raise")]
        sent = (v["offer_rounds"].get(n) or [None])[-1]
        if not offers or sent is None:
            wrong[n] = {"receipts": len(offers), "on_town": sent is not None}
            continue
        mine = [round(float(x), 2) for x in
                (offers[-1]["chose"] or {}).get("kw_per_half_hour") or []]
        town = [round(float(s.get("kw_offered") or 0), 2)
                for s in sent.get("slots") or []]
        if len(mine) != len(town) or any(abs(a - b) > 0.011
                                         for a, b in zip(mine, town)):
            wrong[n] = {"agent": mine, "town": town}
    check("each node's offer on Town's record is the amount its agent chose,"
          " by its receipt", not wrong, wrong or {"nodes": len(nodes)})
    loud = {n: o.get("agent") for n, rounds in v["offer_rounds"].items()
            for o in rounds if set((o.get("agent") or {})) - WIRE_KEYS}
    check("the coordinator heard only the amount: no reason or person's words"
          " in any offer on Town's record", not loud, loud or "only "
          + ", ".join(sorted(WIRE_KEYS)))
    missing = {}
    for n in nodes:
        points = Counter(r["point"] for r in last[n])
        need = {"offer"} | ({"commitment"} if v["answers"].get(n) else set())
        if need - set(points):
            missing[n] = sorted(need - set(points))
    check("every node's offer and commitment left a receipt", not missing,
          missing or {n: dict(Counter(r["point"] for r in last[n]))
                      for n in nodes})
    unheard = {}
    for n in nodes:
        heard = {(m["ts"], m["point"]) for m in (now.get(n) or {}).get("told")
                 or []}
        for r in last[n]:
            if not r.get("less_than_rule"):
                continue
            if not str(r.get("reason") or "").strip() or (
                    NO_REASON not in (r.get("flags") or [])
                    and (r["ts"], r["point"]) not in heard):
                unheard.setdefault(n, []).append(
                    {"point": r["point"], "ts": r["ts"],
                     "reason": r.get("reason"), "flags": r.get("flags")})
    check("each answer that gave less than the rule's amount has a reason, and"
          " the owner or manager was told it", not unheard,
          unheard or {"cuts": sum(bool(r.get("less_than_rule"))
                                  for n in nodes for r in last[n])})
    return out


def day_story(case: Case, o: dict[str, Any], now: dict[str, Any],
              got: dict[str, Any] | None, nodes: list[str]) -> dict[str, Any]:
    """For each node: what its agent decided at each point and why, beside the
    rule's plan; what its person heard; what the guard did with its device
    calls; what it delivered and what it was paid."""
    verdicts: dict[str, Counter] = {}
    held: dict[str, list[dict[str, Any]]] = {}
    for x in (got or {}).get("verdicts") or []:
        verdicts.setdefault(x["node"], Counter())[x["status"]] += 1
        if x["status"] not in ("done", "ok", "applied"):
            held.setdefault(x["node"], []).append(
                {k: x[k] for k in ("ts", "tool", "target", "status", "why")})
    def named(r: dict[str, Any], side: Any) -> Any:
        """A plan goes by this moment's letters: each one with its device."""
        letters = (r.get("asked") or {}).get("letters") or {}
        if not isinstance(side, dict) or not letters:
            return side
        return {k: ([letters.get(x, x) for x in v] if k in ("open",) else v)
                for k, v in side.items()}

    out = {}
    for n in nodes:
        a = now.get(n) or {}
        out[n] = {"decisions": [
            {"point": r["point"], "at": r["ts"], "rule": named(r, r.get("rule")),
             "chose": named(r, r.get("chose")),
             "less_than_rule": r.get("less_than_rule"),
             "by": r.get("by"), "reason": r.get("reason"),
             "facts": [f.get("says") for f in r.get("facts") or []],
             "flags": r.get("flags")} for r in a.get("receipts") or []],
            "told": [f'{m["ts"][11:16]} to the {m["to"]}: {m["text"]}'
                     for m in a.get("told") or []],
            "fallbacks": a.get("fallbacks") or {}, "flags": a.get("flags") or {},
            "guard": dict(verdicts.get(n) or {}),
            "guard_held_or_changed": (held.get(n) or [])[:12],
            "delivered_kwh": o["delivered"]["by_node"].get(n),
            "paid_usd": o["paid"]["paid_usd"].get(n, 0.0)}
    return out


def against_rules(o: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    """This day beside the same day on rules, offline. A comparison, not a
    check: the rules day on Town is its own case, NAME_rules."""
    return {"rules_day_offline": {"sold_kwh": r["sold_kwh"],
                                  "kwh_measured": r["delivered_kwh_measured"],
                                  "kwh_true": r["delivered_kwh_true"],
                                  "buyers_usd": sum(r["buyers_usd"].values()),
                                  "pool_usd": r["pool_usd"]},
            "this_day": {"sold_kwh": o["sold"]["kwh"],
                         "kwh_measured": o["delivered"]["kwh_measured"],
                         "kwh_true": o["delivered"]["kwh_true"],
                         "buyers_usd": buyers_pay(o),
                         "pool_usd": o["paid"]["pool_usd"]},
            "paid_usd_change": {
                h: round(float(o["paid"]["paid_usd"].get(h) or 0) - usd, 2)
                for h, usd in r["paid_usd"].items()}}


def same_traffic(v: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    """The day on Town carried the messages the offline day did, kind by kind."""
    want = r["messages_by_kind"]
    return {"name": "Town carried the offline day's messages, kind by kind",
            "ok": v["by_kind"] == want,
            "detail": {k: (want.get(k), v["by_kind"].get(k))
                       for k in sorted(set(want) | set(v["by_kind"]))
                       if want.get(k) != v["by_kind"].get(k)}}


def custody(events: list[dict[str, Any]]) -> dict[str, Any]:
    """What Town's own log says about custody, for the whole run."""
    accepted = {e["subject"] for e in events if e["kind"] == "message_accepted"}
    acked = {e["subject"] for e in events if e["kind"] == "ack_recorded"
             and e["detail"].get("status") != "retryable"}
    return {"events": dict(sorted(Counter(e["kind"] for e in events).items())),
            "accepted_by_kind": dict(sorted(Counter(
                e["detail"].get("kind") for e in events
                if e["kind"] == "message_accepted").items())),
            "accepted_never_settled": len(accepted - acked)}


# -- Town's evidence -------------------------------------------------------

def town_evidence(bundle: str) -> dict[str, Any]:
    from nandatown.bundle import verify_bundle
    from nandatown.receipt import make_receipt, render_proof, verify_receipt

    problems = verify_bundle(bundle)
    manifest = json.loads(Path(bundle, "manifest.json").read_text())
    out: dict[str, Any] = {"verify": problems or "ok",
                           "bundle_fingerprint": manifest["bundle_fingerprint"]}
    if not problems:
        make_receipt(bundle)
        receipt = verify_receipt(os.path.join(bundle, "receipt.json"), bundle)
        ok, text = render_proof(bundle)
        here = os.path.basename(bundle)         # the proof names its folder
        out.update({"receipt": receipt or "ok", "proof": ok, "proof_text":
                    text.replace(bundle, here).strip().splitlines()})
    return out


def write_market_bundle(s: Stack, events: list[dict[str, Any]],
                        intents: list[dict[str, Any]], record: dict[str, Any],
                        out_dir: Path) -> tuple[str, Any]:
    from nandatown.bundle import attest_bundle, write_bundle
    from nandatown.evaluator import EVALUATOR_VERSION, evaluate
    from nandatown.records import RunRecord, TownEvent, fingerprint

    version = (f"{record['flexgrid'].get('version')}"
               f" {record['flexgrid']['code_sha256'][:12]}")
    town_events = [TownEvent.model_validate(e) for e in events]
    run = RunRecord(
        run_id=s.run_id, profile_name=s.profile.name,
        profile_fingerprint=fingerprint(s.profile.model_dump()),
        created_at=next(e.at for e in town_events if e.kind == "run_created"),
        participants=[{
            "name": n, "role": r,
            "capabilities": s.profile.capabilities.get(n, []),
            "runtime": "scripted" if n == READER else "cmd",
            "release": "nandatown.participants.buyer" if n == READER
            else f"flexgrid-core {version}"}
            for n, r in s.profile.roles.items()],
        releases={"nandatown": record["town"]["nandatown"],
                  "evaluator": EVALUATOR_VERSION,
                  "python": record["town"]["python"], "flexgrid-core": version},
        config={"harness": "examples/flexgrid/run_check.py",
                "case": s.case.name, "about": s.case.about, "arm": s.case.arm,
                "town_main_commit": record["town"]["main_commit"],
                "flexgrid": record["flexgrid"], "day": s.day.date,
                "window": list(s.day.window), "orders_kw": s.orders_kw,
                "events": list(s.case.events),
                "clock_waits_for_agents": s.case.waits,
                "step_seconds": s.step, "homes": list(s.homes),
                "buildings": list(BUILDINGS),
                "model": record["model"],
                "subject": f"FlexGrid market day: coordinator, 2 buyers,"
                           f" {len(s.nodes)} nodes"
                           + (", every node an agent" if s.case.model
                              else ", every node on rules"),
                "rerun_command": "python examples/flexgrid/run_check.py"
                                 f" --cases {s.case.name}"
                                 + (" --model URL --model-name NAME"
                                    if s.case.model else ""),
                "rerun_required_inputs": {
                    "FLEXGRID_HOME": "a FlexGrid checkout with its data"}})
    result = evaluate(s.profile, s.run_id, town_events)
    bundle = str(out_dir / s.case.name)
    write_bundle(bundle, s.profile, run, intents, town_events, result)
    attest_bundle(bundle)
    return bundle, result


# -- the cases -------------------------------------------------------------

def stage_table(case: Case, o: dict[str, Any], r: dict[str, Any],
                n: int) -> list[dict[str, Any]]:
    facts = {**r, "n": n, "all_slots": slots_of(n, r),
             "buyers_total": sum(r["buyers_usd"].values())}
    rows = []
    for stage in STAGES:
        text, test = case.expect[stage]
        rows.append({"stage": stage, "expected": text.format(**facts),
                     "observed": o[stage], "ok": bool(test(o, r, n))})
    return rows


def run_market_case(name: str, a: Any, homes: dict[str, Path], out: Path,
                    ports: list[int]) -> dict[str, Any]:
    case = CASES[name]
    home = homes[case.code]
    s = Stack(case, home, Path(a.work) / name, a, ports)
    record: dict[str, Any] = {
        "case": name, "scenario": case.scenario, "kind": case.kind,
        "arm": case.arm, "about": case.about, "code": case.code,
        "flexgrid": flexgrid_version(home), "town": town_version(),
        "community": {"homes": list(s.homes), "buildings": list(BUILDINGS)},
        "day": case.day.date, "window": list(case.day.window),
        "price_usd_per_kwh": PRICE, "events": list(case.events),
        "note_on_file": dict([case.note]) if case.note else None,
        "clock_waits_for_agents": case.waits, "step_seconds": s.step,
        "model": model_version(a.model, a.model_name) if case.model else None}
    t0 = time.time()
    passes: list[dict[str, Any]] = []
    try:
        s.run_reference()
        record["orders_kw"] = s.orders_kw
        if case.day.sized:
            record["orders_sized_from"] = {
                "rule": f"{ASK_SHARE:.0%} of the lowest half hour of true relief"
                        " in the window, on the rules day offline with the live"
                        " days' orders, larger than it can fill; split"
                        f" {BUYER_SHARE['utility']:.1%} and"
                        f" {BUYER_SHARE['grid-operator']:.1%}",
                "orders_kw": ORDERS,
                "delivered_kw_by_half_hour":
                    s.sized_from["delivered_kw_by_half_hour"]}
        s.start_town()
        s.open_run()
        s.write_configs()
        s.start_flexgrid()
        for k in range(case.passes):
            mark = time.time()
            s.start_day()
            why = s.follow()
            say(f"[{name}] pass {k + 1}: {why}")
            passes.append({"pass": k + 1, "ended": why, "mark": mark,
                           "ledger": s.ledger(), "agents": s.agents_now(),
                           "read": time.time(),
                           "events_posted": list(s.events_posted)})
            s.events_posted.clear()
            if case.model:
                record["model_use"] = {
                    base: s.get(base, "/health").get("model")
                    for base in ("fleet", "buildings")}
        reader = s.read_statement()
        time.sleep(1.0)
        events, intents = s.export()
    finally:
        s.stop()
        give_ports(ports)
    n = len(s.nodes)
    got = s.model_answers() if case.model else None
    for row in passes:
        v = town_view(events, intents, row["mark"], row["read"])
        row["view"] = v
        row["observed"] = observed(row["ledger"], v)
        row["town_messages"] = v["messages"]
    last = passes[-1]
    stages = stage_table(case, last["observed"], s.reference, n)
    checks = cross_checks(last["ledger"], last["view"])
    if case.same_as_offline and not case.events:
        checks.append(same_traffic(last["view"], s.reference))
    if got and case.waits:
        # a new play builds each agent anew (fleet.reset), so what it holds
        # is this play's
        agents = dict(last["agents"])
        checks += agent_checks(case, last["view"], got, agents, s.nodes)
        record["nodes"] = day_story(case, last["observed"], agents, got,
                                    s.nodes)
        record["agents"] = agents   # each node's receipts and messages, whole
    elif case.model:                # the code of 6 Nov: no receipts to read
        record["nodes"] = day_story(case, last["observed"], {}, got, s.nodes)
    kinds = {e["kind"] for e in events}
    if case.events:
        posted = [p["events_posted"] for p in passes]
        checks.append({"name": "each event of the day is on Town's record, at"
                               " its own step",
                       "ok": "scenario_event" in kinds and all(
                           sorted((e["kind"], e["at"]) for e in p)
                           == sorted((e["kind"], e["at"]) for e in case.events)
                           and all(e["posted_at_step"] == e["at"] for e in p)
                           for p in posted),
                       "detail": posted})
    if case.waits:
        went_on = (last["ledger"].get("conductor") or {}).get("went_on") or []
        checks.append({"name": "the clock never went on without an answer it"
                               " waited for",
                       "ok": not went_on, "detail": went_on or {
                           "steps": (last["ledger"].get("conductor") or {})
                           .get("steps"),
                           "held_s": (last["ledger"].get("conductor") or {})
                           .get("held_s")}})
    if case.replayed and case.code == "current":
        checks.append({"name": "both plays: Town refused no repeated id",
                       "ok": all(p["observed"]["readings_in"]["refused_by_town"]
                                 == 0 for p in passes),
                       "detail": [p["observed"]["readings_in"]["refused_by_town"]
                                  for p in passes]})
    if case.replayed and case.arm == "rules":
        checks.append({"name": "the first play paid each node as offline",
                       "ok": same_pay(passes[0]["observed"], s.reference),
                       "detail": {"pool_usd": passes[0]["observed"]["paid"]
                                  ["pool_usd"]}})
    bundle, result = write_market_bundle(s, events, intents, record, out)
    town = town_evidence(bundle)
    if got:                         # the whole questions, for whoever wants them
        logs = Path(bundle) / "model_log"
        logs.mkdir()
        for folder in (s.fleet, s.bld):
            for f in sorted((folder / "state" / "decisions").glob("*.jsonl")):
                shutil.copyfile(f, logs / f.name)
        record.update({"model_answers": got["answers"],
                       "model_chains": got["chains"],
                       "guard_verdicts": dict(Counter(
                           x["status"] for x in got["verdicts"]))})
    kept = [{k: p[k] for k in ("pass", "ended", "town_messages", "observed",
                               "events_posted")}
            for p in passes]
    record.update({
        "wall_minutes": round((time.time() - t0) / 60, 1), "passes": kept,
        "offline_reference": s.reference,
        "against_rules_day": against_rules(last["observed"], s.reference),
        "conductor": last["ledger"].get("conductor"),
        "stages": stages, "checks": checks,
        "town_run": {"run_id": s.run_id, "verdict": result.verdict,
                     "expected_verdict": case.verdict,
                     "statement_buyer_exit": reader,
                     "quote": s.profile.task,
                     "stages": {x.name: {"status": x.status, "note": x.note}
                                for x in result.stages},
                     "custody": custody(events), **town}})
    record["ok"] = (result.verdict == case.verdict
                    and all(r["ok"] for r in stages)
                    and all(c["ok"] for c in checks)
                    and town["verify"] == "ok")
    say(f"[{name}] {'as expected' if record['ok'] else 'NOT as expected'}:"
        f" Town {result.verdict}; stages off:"
        f" {[r['stage'] for r in stages if not r['ok']]}; checks off:"
        f" {[c['name'] for c in checks if not c['ok']]}")
    return record


def run_case(name: str, a: Any, homes: dict[str, Path],
             out: Path) -> dict[str, Any]:
    """Ports are picked as a case starts: a queued one can wait a long time."""
    with MODEL_TURN if CASES[name].model else contextlib.nullcontext():
        if STOPPING.is_set():
            raise RuntimeError("the check was stopped before this case began")
        return run_market_case(name, a, homes, out, take_ports(a.base_port, 6))


def left_the_rule(d: dict[str, Any]) -> bool:
    """Did this decision differ from what the rule would have done?"""
    r, c, p = d.get("rule") or {}, d.get("chose") or {}, d["point"]
    if d.get("less_than_rule"):
        return True
    if p in ("opening", "exception"):
        return sorted(c.get("open") or []) != sorted(r.get("open") or []) \
            or bool(c.get("release"))
    if p == "pre-cool":
        return c.get("offset_c") != r.get("offset_c")
    if p == "commitment":
        return c.get("answer") != r.get("answer")
    if p in ("offer", "offer_raise"):
        return c.get("kw_per_half_hour") != r.get("kw_per_half_hour")
    if p == "note":                 # rules can't read a note: any need is the agent's
        return bool(c.get("needs") or c.get("ended"))
    return False


def comparison(results: dict[str, Any]) -> dict[str, Any]:
    """Each scenario, every node an agent beside every node on rules, both on
    Town: what the agents changed."""
    out = {}
    for c in SCENARIOS:
        ag, ru = results.get(c.name), results.get(f"{c.name}_rules")
        if not (ag and ru and "passes" in ag and "passes" in ru):
            continue
        oa, orr = ag["passes"][-1]["observed"], ru["passes"][-1]["observed"]

        def line(o: dict[str, Any]) -> dict[str, Any]:
            return {"sold_kwh": o["sold"]["kwh"],
                    "under_contract": o["sold"]["nodes"],
                    "kwh_measured": o["delivered"]["kwh_measured"],
                    "kwh_true": o["delivered"]["kwh_true"],
                    "buyers_usd": round(buyers_pay(o), 2),
                    "pool_usd": o["paid"]["pool_usd"]}
        nodes = {}
        for n in NODES:
            st = (ag.get("nodes") or {}).get(n) or {}
            nodes[n] = {
                "delivered_kwh": {"agent": oa["delivered"]["by_node"].get(n),
                                  "rules": orr["delivered"]["by_node"].get(n)},
                "paid_usd": {"agent": oa["paid"]["paid_usd"].get(n, 0.0),
                             "rules": orr["paid"]["paid_usd"].get(n, 0.0)},
                "left_the_rule": [
                    {k: d[k] for k in ("point", "at", "rule", "chose", "reason")}
                    for d in st.get("decisions") or [] if left_the_rule(d)],
                "told": st.get("told") or [],
                "fallbacks": st.get("fallbacks") or {}}
        out[c.name] = {"about": c.about, "agents": line(oa), "rules": line(orr),
                       "nodes": nodes}
    return out


def failure_story(results: dict[str, Any]) -> dict[str, Any] | None:
    """The repeated-id failure as baseline, failure, change, rerun."""
    old, new = results.get("network_failure_before"), \
        results.get("network_failure")
    if not (old and new and "passes" in old and "passes" in new):
        return None

    def line(case: dict[str, Any], k: int) -> dict[str, Any]:
        o = case["passes"][k]["observed"]
        return {"sellers_settled": o["paid"]["sellers_settled"],
                "unpaid": o["paid"]["unpaid"],
                "readings_at_utility": o["readings_in"]["utility"],
                "refused_by_town": o["readings_in"]["refused_by_town"],
                "seller_usd": o["settled"]["seller_usd"]}

    return {"baseline": {"code": old["flexgrid"], "first_play": line(old, 0)},
            "failure": {"second_play_same_run": line(old, 1),
                        "town_verdict": old["town_run"]["verdict"],
                        "town_correct": old["town_run"]["stages"]["correct"]},
            "change": "a message's id says which message it is, the same"
                      " within a play and new across plays (ids.py, town.Sent),"
                      " and a refused send is counted and logged"
                      " (town.TownSession.send)",
            "rerun": {"code": new["flexgrid"],
                      "first_play": line(new, 0),
                      "second_play_same_run": line(new, 1),
                      "town_verdict": new["town_run"]["verdict"]}}


def personal_paths(path: Path) -> list[str]:
    """Nothing that names this machine goes into recorded evidence."""
    home = str(Path.home()).encode()
    return [str(f.relative_to(path)) for f in path.rglob("*")
            if f.is_file() and home in f.read_bytes()]


def record(out: Path, to: Path) -> None:
    """Write this run as the evidence in `to`. summary.json and the bundles in
    PLAIN_CASES stay as plain files, so `nandatown verify` works on them as
    they are. All the other bundles go in one archive, written the same way
    each time, with every receipt, report and model log. `tar xzf` gives back
    every bundle whole, byte for byte."""
    def plain(f: Path) -> bool:
        parts = f.relative_to(out).parts
        return parts == ("summary.json",) or (
            len(parts) == 2 and parts[0] in PLAIN_CASES
            and parts[1] in BUNDLE_FILES)

    files = sorted(f for f in out.rglob("*") if f.is_file())
    shutil.rmtree(to, ignore_errors=True)
    to.mkdir(parents=True)
    for f in filter(plain, files):
        dest = to / f.relative_to(out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dest)
    with gzip.GzipFile(to / EXTRAS, "wb", mtime=0) as z, \
            tarfile.open(fileobj=z, mode="w") as tar:
        for f in files:
            if not plain(f):
                info = tarfile.TarInfo(str(f.relative_to(out)))
                info.size, info.mode = f.stat().st_size, 0o644
                with open(f, "rb") as body:
                    tar.addfile(info, body)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cases", default="all",
                    help=f"comma separated: {', '.join(CASES)}")
    ap.add_argument("--homes", default=",".join(HOMES),
                    help="the homes, by name")
    ap.add_argument("--step", type=float, default=2.0,
                    help="wall seconds per 15-minute dataset step, at least")
    ap.add_argument("--jobs", type=int, default=1,
                    help="cases at once: rules days run side by side, agent"
                         " days take turns on the model")
    ap.add_argument("--base-port", type=int, default=41400)
    ap.add_argument("--work", default=None, help="default: a temp folder")
    ap.add_argument("--record", action="store_true",
                    help=f"write the evidence to {EVIDENCE.relative_to(REPO)}")
    ap.add_argument("--model", default=None,
                    help="an Ollama-style URL: the agent days need it. Without"
                         " it only the rules days run")
    ap.add_argument("--model-name", default=None)
    ap.add_argument("--model-step", type=float, default=OLD_MODEL_STEP,
                    help="wall seconds per step on the 6 Nov code's model day,"
                         " which has no waiting clock")
    a = ap.parse_args()
    a.homes = [h for h in a.homes.split(",") if h]
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop_all)
    if not os.environ.get("FLEXGRID_HOME"):
        sys.exit("FLEXGRID_HOME is not set: see examples/flexgrid/README.md")
    homes = {"current": Path(os.environ["FLEXGRID_HOME"]).expanduser().resolve()}
    if os.environ.get("FLEXGRID_BASELINE_HOME"):
        homes["baseline"] = Path(
            os.environ["FLEXGRID_BASELINE_HOME"]).expanduser().resolve()
    if a.record and ((a.cases != "all" and set(a.cases.split(",")) != set(CASES))
                     or "baseline" not in homes or not a.model):
        sys.exit("--record replaces all the evidence: it needs every case,"
                 " FLEXGRID_BASELINE_HOME and --model")
    for home in homes.values():
        for need in ("flexgrid_core", DATA_DIR, "tools/sim_event.py"):
            if not (home / need).exists():
                sys.exit(f"{home} has no {need}: see examples/flexgrid/README.md")
    root = Path(a.work or tempfile.mkdtemp(prefix="flexgrid-town-")).resolve()
    a.work = str(root)
    os.environ["NANDATOWN_HOME"] = str(root / "town_home")
    os.environ.pop("FLEXGRID_CONFIG_DIR", None)
    out = root / "evidence"
    out.mkdir(parents=True, exist_ok=True)
    names = list(CASES) if a.cases == "all" \
        else [c for c in a.cases.split(",") if c]
    unknown = [c for c in names if c not in CASES]
    if unknown:
        sys.exit(f"no case {unknown}: one of {', '.join(CASES)}")
    skipped = [c for c in names if CASES[c].code not in homes
               or (CASES[c].model and not a.model)]
    names = [c for c in names if c not in skipped]
    say(f"cases: {names}; not run, no FLEXGRID_BASELINE_HOME or no --model:"
        f" {skipped}")
    results: dict[str, Any] = {}
    # the agent days one at a time, since they share one model, and the rules
    # days beside them, up to --jobs at once
    with ThreadPoolExecutor(max_workers=1) as agents, \
            ThreadPoolExecutor(max_workers=max(1, a.jobs)) as rules:
        futures = {c: (agents if CASES[c].model else rules).submit(
            run_case, c, a, homes, out) for c in names}
        for c in names:                 # the order they're listed
            try:
                results[c] = futures[c].result()
            except Exception as exc:        # one case must not lose the others
                say(f"[{c}] could not finish: {exc}")
                results[c] = {"case": c, "ok": False,
                              "error": f"{type(exc).__name__}: {exc}"}
    results = {c: results[c] for c in CASES if c in results}   # in their own order
    summary = {
        "what": "FlexGrid demand-flex market on a local Town run, every node an"
                " agent, beside every node on rules; see"
                " examples/flexgrid/README.md",
        "town": town_version(),
        "community": {"homes": list(a.homes), "buildings": list(BUILDINGS)},
        "clock": "the clock waits for the agents: every clock holds at each"
                 " 15-minute step until every agent has answered. A test and"
                 " demo device: a real evening doesn't wait",
        "model": {"temperature": TEMPERATURE, "seed": SEED,
                  "name": a.model_name} if a.model else None,
        "judged_by_town": "the closing statement of each market day (one"
                          " quote: accepted, claimed, received, processed,"
                          " response, correct), portable identity, and the"
                          " integrity of each bundle",
        "judged_by_run_check": "sold, delivered, readings in, settled, paid,"
                               " each cross-check of Town's records against"
                               " FlexGrid's ledgers, and on an agent day each"
                               " node's offer, receipt and reason against"
                               " Town's record",
        "not_run": skipped,
        "ok": bool(results) and all(r.get("ok") for r in results.values()),
        "agents_against_rules": comparison(results),
        "failure_case": failure_story(results), "cases": results}
    (out / "summary.json").write_text(json.dumps(summary, indent=1,
                                                 default=str) + "\n")
    leaked = personal_paths(out)
    if leaked:
        say(f"personal paths in the evidence: {leaked}")
    if a.record and summary["ok"] and not leaked:
        record(out, EVIDENCE)
        say(f"recorded in {EVIDENCE.relative_to(REPO)}")
    elif a.record:
        say("not recorded: this run isn't as expected, so what's there stays")
    for c, r in results.items():
        say(f"{c}: {'as expected' if r.get('ok') else 'NOT AS EXPECTED'}")
    say(f"evidence: {out}")
    return 0 if summary["ok"] and not leaked else 1


if __name__ == "__main__":
    sys.exit(main())
