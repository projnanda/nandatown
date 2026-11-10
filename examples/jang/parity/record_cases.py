"""Record SessionView fixtures for the strategy parity check.

Plays the Python port of JANG's rule strategy against itself through a live
JANG server, for both of JANG's bundled scenarios and several strategy
options, and writes every view an agent decided on, with the Python decision,
to ``decide_cases.full.json``. ``ts_decide.mts`` then decides the same views with
JANG's TypeScript original and stores its decisions next to them, and
``compact`` writes the committed ``decide_cases.json`` (shared item specs and
mandates stored once). CI compares the Python port against the stored
TypeScript decisions without needing Node.

    JANG_REPO=../JANGmarket python examples/jang/parity/record_cases.py
    (cd $JANG_REPO && npx tsx <this dir>/ts_decide.mts <this dir>/decide_cases.full.json)
    python examples/jang/parity/record_cases.py compact
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from jang_client import JangClient, JangRefusal  # noqa: E402
from jang_sign import OwnerKey, PythonSigner  # noqa: E402
from jang_strategy import decide  # noqa: E402

OPTION_SETS = [
    ({}, {}),
    ({"toughness": 1.5}, {"toughness": 1.6}),
    ({"respectBounds": True}, {"respectBounds": True}),
    ({"deadlineAware": True}, {"deadlineAware": True, "toughness": 3}),
    ({"meso": True}, {"respectBounds": True}),
    ({"toughness": 1.2}, {"meso": True, "toughness": 1.8}),
]
FAR_FUTURE = "2099-01-01T00:00:00.000Z"


def play(base: str, scenario: dict, buyer_opts: dict, seller_opts: dict,
         disclose: bool) -> list[dict]:
    suffix = "-" + uuid.uuid4().hex[:6]
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    keys = {}
    for role in ("buyer", "seller"):
        party = scenario[role]
        agent_id = party["agentId"] + suffix
        api_key = JangClient(base).register_agent(agent_id, party["ownerId"])
        body = {"mandateId": f"mdt_{scenario['scenarioId']}_{role}{suffix}", "agentId": agent_id,
                "ownerId": party["ownerId"], "role": role, "itemId": scenario["itemSpec"]["itemId"],
                "constraints": party["constraints"], "preferences": party["preferences"],
                "reservationUtility": party["reservationUtility"], "expiresAt": expires}
        if disclose:
            body["disclosePriorities"] = True
        JangClient(base, api_key).lodge_mandate(PythonSigner().sign_mandate(body, OwnerKey.generate()))
        keys[role] = (agent_id, JangClient(base, api_key))
    seller = keys["seller"][1]
    sid = seller.open_session(keys["buyer"][0], scenario["itemSpec"],
                              scenario.get("maxRounds", 12))["sessionId"]
    opts = {"buyer": buyer_opts, "seller": seller_opts}
    cases = []
    turn = "seller"
    for _ in range(60):
        client = keys[turn][1]
        view = client.get_session(sid)
        if view["status"] != "open":
            break
        fixture = json.loads(json.dumps(view))
        fixture["yourMandate"]["expiresAt"] = FAR_FUTURE  # decisions must not depend on wall time
        move = decide(fixture, opts[turn])
        cases.append({"options": opts[turn], "view": fixture, "python": move})
        try:
            if move["action"] == "offer":
                client.offer(sid, move["terms"], move.get("note", ""), move.get("alternatives"))
            elif move["action"] == "accept":
                client.accept(sid, move.get("offerId"))
                break
            elif move["action"] == "stop":
                break
        except JangRefusal as exc:
            cases[-1]["refusal"] = exc.code
            break
        turn = "buyer" if turn == "seller" else "seller"
    return cases


def main() -> int:
    repo = os.environ.get("JANG_REPO")
    if not repo:
        print("set JANG_REPO to a JANGmarket checkout", file=sys.stderr)
        return 2
    base = os.environ.get("JANG_URL", "http://127.0.0.1:3939")
    cases = []
    for name in ("tv-clearance", "cambridge-rental"):
        with open(os.path.join(repo, "agents", "scenarios", f"{name}.json")) as fh:
            scenario = json.load(fh)
        for i, (b, s) in enumerate(OPTION_SETS):
            for case in play(base, scenario, b, s, disclose=(i % 2 == 1)):
                case["scenario"] = name
                cases.append(case)
    out = os.path.join(HERE, FULL)
    with open(out, "w") as fh:
        json.dump({"jang_commit": os.environ.get("JANG_COMMIT", ""), "cases": cases}, fh)
    print(f"recorded {len(cases)} views to {out}")
    return 0


# -- the committed fixture: shared item specs and mandates stored once ----

FULL = "decide_cases.full.json"
COMPACT = "decide_cases.json"
_DROP_OFFER = ("note", "createdAt")


def compact(data: dict) -> dict:
    items, mandates, cases = {}, {}, []
    for c in data["cases"]:
        view = json.loads(json.dumps(c["view"]))
        item = view.pop("itemSpec")
        item.pop("description", None)
        items[item["itemId"]] = item
        mandate = view.pop("yourMandate")
        mandates[mandate["mandateId"]] = mandate
        for o in view.get("offers", []) + ([view["standingOffer"]] if view.get("standingOffer") else []):
            for k in _DROP_OFFER:
                o.pop(k, None)
        view["_item"], view["_mandate"] = item["itemId"], mandate["mandateId"]
        strip = lambda m: {k: v for k, v in m.items() if k not in ("note", "reason", "targetUtility")}
        cases.append({"scenario": c["scenario"], "options": c["options"], "view": view,
                      "python": strip(c["python"]), "ts": strip(c["ts"])})
    return {"jang_commit": data.get("jang_commit", ""), "items": items,
            "mandates": mandates, "cases": cases}


def expand(data: dict, case: dict) -> dict:
    view = json.loads(json.dumps(case["view"]))
    view["itemSpec"] = data["items"][view.pop("_item")]
    view["yourMandate"] = data["mandates"][view.pop("_mandate")]
    return view


def write_compact() -> int:
    with open(os.path.join(HERE, FULL)) as fh:
        data = json.load(fh)
    if not all("ts" in c for c in data["cases"]):
        print("run ts_decide.mts on the full file first", file=sys.stderr)
        return 2
    with open(os.path.join(HERE, COMPACT), "w") as fh:
        json.dump(compact(data), fh, separators=(",", ":"))
    print(f"wrote {COMPACT}")
    return 0


if __name__ == "__main__":
    sys.exit(write_compact() if sys.argv[1:] == ["compact"] else main())
