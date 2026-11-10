#!/usr/bin/env python3
"""Run the Adgentic compatibility matrix on Town Track profiles and check each run.

Usage (from the nandatown checkout, Town venv active, see README.md):
  python integrations/adgentic/run_matrix.py --adgentic-dir ../adgentic --repeats 3 \
      --out integrations/adgentic/results

For each case it runs `nandatown run PROFILE --agent buyer=cmd:...`, then checks the
run independently of what either agent says:
  - `nandatown verify` on the Town bundle (hashes, bindings, evaluator replay)
  - the Town verdict and stage results against the expected values below
  - the Adgentic exchange log: intents, Town quote requests and responses, offers,
    Ed25519 signatures (re-verified against the registered public keys), review
    verdicts and acceptances
Expected values are fixed in CASES before running. The first full run compared
verdicts case-sensitively (Town writes "passed"); that checker bug was fixed and the
matrix rerun. amnesia-fresh-ids-drain was added after that first run showed the
second response arrives after the exchange exits (see README).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOWN_ROOT = HERE.parent.parent

# Expected behavior, written before the matrix was run.
#   town: expected Town verdict;  accept: seller expected to win on Adgentic (None = nothing)
#   town_quote_used: whether the Town quote should become an Adgentic offer
CASES = [
    {"id": "clean", "profile": "quote-clean", "env": {}, "town": "PASSED",
     "accept": "town-seller", "town_quote_used": True},
    {"id": "crash-restart", "profile": "quote-crash-restart", "env": {}, "town": "PASSED",
     "accept": "town-seller", "town_quote_used": True},
    {"id": "amnesia-restart", "profile": "quote-amnesia-restart", "env": {}, "town": "PASSED",
     "accept": "town-seller", "town_quote_used": True},
    {"id": "drop-wakeup", "profile": "quote-drop-wakeup", "env": {}, "town": "PASSED",
     "accept": "town-seller", "town_quote_used": True},
    {"id": "duplicate-delivery", "profile": "quote-duplicate-delivery", "env": {}, "town": "PASSED",
     "accept": "town-seller", "town_quote_used": True},
    {"id": "lost-ack", "profile": "quote-lost-ack", "env": {}, "town": "PASSED",
     "accept": "town-seller", "town_quote_used": True},
    # Town's control that fails on purpose: the seller sends two distinct responses.
    # Adgentic should still create one offer from the first and accept once.
    {"id": "amnesia-fresh-ids", "profile": "quote-amnesia-fresh-ids", "env": {}, "town": "FAILED",
     "accept": "town-seller", "town_quote_used": True},
    # Same fault with a 6 s drain window, so the second distinct response reaches the
    # exchange: Adgentic should see 2 distinct responses, keep one offer, accept once.
    {"id": "amnesia-fresh-ids-drain", "profile": "quote-amnesia-fresh-ids",
     "env": {"ADGENTIC_TOWN_DRAIN": "6"}, "town": "FAILED", "accept": "town-seller",
     "town_quote_used": True, "town_responses_distinct": 2},
    # Boundary: Adgentic's seller deadline (1 s) is shorter than Town's crash recovery
    # (1.5 s lease + restart). Adgentic should skip the Town seller and buy nothing.
    {"id": "crash-restart-timeout-1s", "profile": "quote-crash-restart",
     "env": {"ADGENTIC_TOWN_TIMEOUT": "1"}, "town": "PASSED",
     "accept": None, "town_quote_used": False},
    # Mixed market: Adgentic's own sellers also list widgets. Its cheapest honest
    # seller (BudgetBasics, $33.92) should beat the Town seller ($39.90); the
    # deceptive seller should be flagged.
    {"id": "mixed-clean", "profile": "quote-clean", "env": {"ADGENTIC_TOWN_MARKET": "mixed"},
     "town": "PASSED", "accept": "s1", "town_quote_used": True},
    {"id": "mixed-crash-restart", "profile": "quote-crash-restart",
     "env": {"ADGENTIC_TOWN_MARKET": "mixed"}, "town": "PASSED", "accept": "s1",
     "town_quote_used": True},
]

SIGNED_FIELDS = ("offer_id", "intent_id", "seller_id", "product", "all_in_price",
                 "delivery_days", "return_days_claimed", "in_stock", "expires_at",
                 "sponsored", "pitch_text")


def sig_ok(offer: dict, pub_hex: str, py: str) -> bool:
    code = ("import json,sys;from nacl.signing import VerifyKey;from nacl.encoding import HexEncoder;"
            "o=json.loads(sys.argv[1]);k=sys.argv[2];f=json.loads(sys.argv[3]);"
            "p=json.dumps({x:o[x] for x in f},sort_keys=True,separators=(',',':')).encode();"
            "VerifyKey(k.encode(),encoder=HexEncoder).verify(p,bytes.fromhex(o['signature']));print('ok')")
    r = subprocess.run([py, "-c", code, json.dumps(offer), pub_hex, json.dumps(SIGNED_FIELDS)],
                       capture_output=True, text=True)
    return r.stdout.strip() == "ok"


def check(case: dict, bundle: Path, adg_py: str) -> dict:
    st = bundle / "state" / "buyer"
    result = json.load(open(bundle / "result.json"))
    verify = subprocess.run(["nandatown", "verify", str(bundle)], capture_output=True, text=True)
    log = [json.loads(l) for l in open(st / "adgentic_log.jsonl")] if (st / "adgentic_log.jsonl").exists() else []
    gt = json.load(open(st / "adgentic_ground_truth.json")) if (st / "adgentic_ground_truth.json").exists() else {}
    buyer = json.load(open(st / "adgentic_buyer.json")) if (st / "adgentic_buyer.json").exists() else {}
    ev = lambda name: [e for e in log if e["event"] == name]
    offers = [e["offer"] for e in ev("offer_signed")]
    pubs = {sid: s["public_key"] for sid, s in gt.get("sellers", {}).items()}
    pubs.update({e["seller_id"]: e["public_key"] for e in ev("seller_registered")})
    sigs = [sig_ok(o, pubs.get(o["seller_id"], ""), adg_py) for o in offers]
    accepted = ev("offer_accepted")
    town_offer = [o for o in offers if o["seller_id"] == "town-seller"]
    reviews = {e["offer_id"]: e for e in ev("offer_reviewed")}
    stages = {s["name"]: s["status"] for s in result.get("stages", [])} if isinstance(result.get("stages"), list) \
        else {k: v.get("status") if isinstance(v, dict) else v for k, v in result.get("stages", {}).items()}
    row = {
        "case": case["id"], "profile": case["profile"], "bundle": bundle.name,
        "town_verdict": result.get("verdict"), "town_verify_ok": verify.returncode == 0,
        "town_stages": stages,
        "intents": len(ev("intent_received")),
        "town_requests": len(ev("town_quote_requested")),
        "town_responses": len(ev("town_quote_received")),
        "town_responses_distinct": len({e["town_message_id"] for e in ev("town_quote_received")}),
        "town_timeouts": len(ev("town_quote_timeout")),
        "town_offer_created": bool(town_offer),
        "town_offer_review": (reviews.get(town_offer[0]["offer_id"], {}).get("review_status")
                              if town_offer else None),
        "offers": len(offers), "signatures_ok": f"{sum(sigs)}/{len(sigs)}",
        "acceptances": len(accepted),
        "accepted_seller": accepted[0]["receipt"]["seller_id"] if accepted else None,
        "accepted_price": accepted[0]["receipt"]["all_in_price"] if accepted else None,
        "flagged": sorted({(e["seller_id"], tuple(e["reason_codes"])) for e in ev("offer_reviewed")
                           if e["reason_codes"]}),
        "buyer_wall_s": buyer.get("wall_s"),
        "buyer_error": buyer.get("error"),
    }
    row["expected"] = {"town": case["town"], "accept": case["accept"],
                       "town_quote_used": case["town_quote_used"],
                       "town_responses_distinct": case.get("town_responses_distinct")}
    row["match"] = (str(row["town_verdict"]).upper() == case["town"] and row["town_verify_ok"]
                    and row["town_responses_distinct"] == case.get("town_responses_distinct",
                                                                   row["town_responses_distinct"])
                    and row["accepted_seller"] == case["accept"]
                    and row["town_offer_created"] == case["town_quote_used"]
                    and row["acceptances"] <= 1 and row["intents"] == 1
                    and row["town_requests"] == 1 and sum(sigs) == len(sigs))
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adgentic-dir", required=True)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--cases", default="")
    a = ap.parse_args()
    adg = Path(a.adgentic_dir).resolve()
    adg_py = str(adg / ".venv" / "bin" / "python")
    out = Path(a.out).resolve()
    work = out / "_work"
    work.mkdir(parents=True, exist_ok=True)
    cmd = f"{adg_py} {HERE / 'adgentic_buyer.py'} --adgentic-dir {adg}"
    rows = []
    selected = [c for c in CASES if not a.cases or c["id"] in a.cases.split(",")]
    for case in selected:
        for rep in range(1, a.repeats + 1):
            env = {**os.environ, **case["env"]}
            t0 = time.time()
            r = subprocess.run(["nandatown", "run", case["profile"], "--out", str(work),
                                "--agent", f"buyer=cmd:{cmd}"], env=env, capture_output=True, text=True)
            line = [l for l in r.stdout.splitlines() if l.startswith("Evidence bundle:")]
            bundle = Path(line[-1].split(":", 1)[1].strip())
            row = check(case, bundle, adg_py)
            row.update({"rep": rep, "town_wall_s": round(time.time() - t0, 2)})
            rows.append(row)
            # Keep the canonical Town records plus the Adgentic-side logs; drop private state.
            dest = out / "runs" / f"{case['id']}_r{rep}"
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True)
            for f in bundle.iterdir():
                if f.is_file():
                    shutil.copy2(f, dest / f.name)
            (dest / "adgentic").mkdir()
            for name in ("adgentic_log.jsonl", "adgentic_buyer.json", "adgentic_connector.json",
                         "adgentic_ground_truth.json"):
                src = bundle / "state" / "buyer" / name
                if src.exists():
                    shutil.copy2(src, dest / "adgentic" / name)
            print(f"{case['id']:26} r{rep} town={row['town_verdict']:7} verify={row['town_verify_ok']} "
                  f"accepted={row['accepted_seller']} town_offer={row['town_offer_created']} "
                  f"resp={row['town_responses']}/{row['town_responses_distinct']} "
                  f"timeouts={row['town_timeouts']} sigs={row['signatures_ok']} "
                  f"{'MATCH' if row['match'] else 'MISMATCH'}", flush=True)
    shutil.rmtree(work, ignore_errors=True)
    json.dump(rows, open(out / "results.json", "w"), indent=1)
    print(f"\n{sum(r['match'] for r in rows)}/{len(rows)} runs matched expectations")
    return 0 if all(r["match"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
