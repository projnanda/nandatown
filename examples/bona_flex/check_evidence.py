"""Offline evidence check for the VeriFlex (Bona) Lab pack. No `bona` or `bench` import (asserted at the end).

    python examples/bona_flex/check_evidence.py <evidence dir or one case dir>

Per case (a folder holding Town's `bundle/` and the `bona_sidecar/` beside it):
1. Town's own integrity and replay: `nandatown.bundle.verify_bundle(bundle)` with the pure bona_flex validator
   loaded, so the recorded result is re-judged from the recorded events;
2. sidecar integrity: `sidecar_manifest.json` lists exactly the sidecar's files, each with its sha256, read as bytes;
3. binding: the sha256 of `sidecar_manifest.json` equals the `manifest_sha256` of the single `bona_sidecar_bound`
   event, and the events.jsonl hash is the one in Town's manifest (so Town's hashes cover the sidecar);
4. one-to-one receipts: the receipt hashes named by events equal the hashes of the files in `receipts/`;
5. sidecar files are UTF-8 with LF line endings only (Town's own records keep the bytes Town wrote; their hashes
   are what Town's manifest covers);
6. Town's attestation.json is present (verify_bundle skips a missing one), and, when the evidence root holds the
   committed summary.json, each run's Bona did:key (sidecar provenance) and Town controller key (attestation) equal
   the pins recorded there; a consistently re-signed case therefore needs the committed summary rewritten too, which
   git history and review show (the pins are not a third-party trust anchor);
7. the six clock checks the desk recorded are RE-DERIVED here from the sidecar's own requests, receipts and
   assessments (freshness, assessment and hold validity, delivery due, seq order); a mismatch is a problem.
Exit 0 only if every case passes. Prints one line per check.
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bona_flex_offline import file_sha256, receipt_sha256  # noqa: E402

import bona_flex_validator  # noqa: E402,F401  (registers @validator("bona_flex") for the replay)


def _events(bundle: pathlib.Path) -> list[dict]:
    return [json.loads(x) for x in (bundle / "events.jsonl").read_bytes().decode("utf-8").splitlines() if x.strip()]


FRESHNESS_S = 30


def _json(p: pathlib.Path):
    return json.loads(p.read_bytes().decode("utf-8"))


def clock_rederived(side: pathlib.Path, evs: list[dict]) -> dict | None:
    """The six checks from the sidecar files alone (None for a run without Bona receipts)."""
    if not (side / "clock_checks.json").is_file():
        return None
    rec = _json(side / "clock_checks.json")
    receipts = {int(p.stem): _json(p) for p in (side / "receipts").glob("*.json")}
    requests = {p.stem: _json(p) for p in (side / "requests").glob("*.json")}
    # requests/r-N.json are the buyer's bytes (aliases: a-N, h-N); mapping.json translates aliases to Bona's ids
    assess = {p.stem: _json(p) for p in (side / "assessments").glob("*.json")}
    real_hold = _json(side / "mapping.json")["holds"]          # {h-N: Bona hold id}
    by_real = {r["hold_id"]: r for r in receipts.values() if r.get("type") == "hold"}
    holds = {alias: by_real[real] for alias, real in real_hold.items() if real in by_real}
    commits = [e["detail"] | {"request": e["subject"]} for e in evs if e["kind"] == "flex_commit"]
    fresh, a_ok, h_ok = True, True, True
    for c in commits:
        rc, rq = receipts.get(c["seq"]), requests.get(c["request"])
        if rc is None or rq is None:
            return {"error": f"missing receipt or request for {c['request']}"}
        fresh &= abs(rq["created_at"] - rc["issued_at"]) <= FRESHNESS_S
        if c["action"] == "hold" and rc["type"] == "hold":
            a_ok &= rc["issued_at"] < assess[rq["assessment_id"]]["valid_until"]
        if c["action"] == "confirm" and rc["type"] == "confirm":
            h_ok &= rc["issued_at"] < holds[rq["hold_id"]]["expires_at"]
    first_issue = min(r["issued_at"] for r in receipts.values())
    seqs = [c["seq"] for c in commits if c["attempt"] == 1]
    # run_budget: the desk's wall time AND a signed lower bound (span of Bona's own issued_at) must be under the
    # budget (QA 95e5d91 L2). delivery_due uses the earliest signed issued_at (the assess call time is unsigned).
    span = max(r["issued_at"] for r in receipts.values()) - first_issue
    budget = rec["detail"]["wall_s"] is not None and rec["detail"]["wall_s"] < rec["detail"]["budget_s"]         and span < rec["detail"]["budget_s"]
    return {"delivery_due": all(a["interval"][1] <= first_issue for a in assess.values()), "freshness": fresh,
            "assessment_valid": a_ok, "hold_valid": h_ok, "run_budget": budget,
            "monotonic_seq": seqs == sorted(seqs) and len(set(seqs)) == len(seqs)}


def check_case(case: pathlib.Path, pins: dict | None = None) -> list[str]:
    from nandatown.bundle import verify_bundle
    problems: list[str] = []
    bundle, side = case / "bundle", case / "bona_sidecar"
    problems += [f"town: {p}" for p in verify_bundle(str(bundle))]
    if not (bundle / "attestation.json").is_file():
        problems.append("town: attestation.json missing (verify_bundle does not require it; this pack does)")
    man_p = side / "sidecar_manifest.json"
    if not man_p.is_file():
        return problems + ["sidecar: no sidecar_manifest.json"]
    listed = json.loads(man_p.read_bytes().decode("utf-8"))["files"]
    actual = {p.relative_to(side).as_posix() for p in side.rglob("*") if p.is_file()} - {"sidecar_manifest.json"}
    if set(listed) != actual:
        problems.append(f"sidecar: listed {sorted(set(listed) ^ actual)} differ from the files present")
    for rel, h in listed.items():
        p = side / rel
        if p.is_file() and file_sha256(p) != h:
            problems.append(f"sidecar: {rel} sha256 changed")
        if p.is_file() and b"\r" in p.read_bytes():
            problems.append(f"sidecar: {rel} has a CR byte (CRLF working copy?)")
    evs = _events(bundle)
    bound = [e for e in evs if e["kind"] == "bona_sidecar_bound"]
    if len(bound) != 1 or bound[0]["detail"].get("manifest_sha256") != file_sha256(man_p):
        problems.append("binding: sidecar_manifest.json sha256 is not the bound one")
    town_man = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if town_man["files"].get("events.jsonl") != "sha256:" + file_sha256(bundle / "events.jsonl"):
        problems.append("binding: events.jsonl is not the hash Town's manifest records")
    named = {e["detail"].get("receipt_sha256") for e in evs if e["kind"] in ("flex_commit", "flex_delivery")}
    named.discard(None)
    files = {receipt_sha256(json.loads(p.read_bytes().decode("utf-8"))) for p in (side / "receipts").glob("*.json")} \
        if (side / "receipts").is_dir() else set()
    if named != files:
        problems.append(f"receipts: events name {len(named)} receipt hash(es), sidecar holds {len(files)}; "
                        f"{len(named ^ files)} unmatched")
    clock_ev = [e for e in evs if e["kind"] == "flex_clock"]
    try:
        again = clock_rederived(side, evs)
    except (KeyError, ValueError, TypeError) as exc:
        again = {"error": f"{type(exc).__name__}: {exc}"}
    if again is not None:
        if not clock_ev or clock_ev[-1]["detail"].get("checks") != again:
            problems.append(f"clock: recorded checks differ from the ones re-derived from the sidecar: {again}")
    if pins is not None:
        prov = _json(side / "provenance.json") if (side / "provenance.json").is_file() else {}
        att = json.loads((bundle / "attestation.json").read_text(encoding="utf-8"))             if (bundle / "attestation.json").is_file() else {}
        if prov.get("bona_did") != pins.get("bona_did"):
            problems.append("pins: the sidecar's Bona did:key is not the one pinned in summary.json")
        if att.get("controller_public") != pins.get("town_controller_public"):
            problems.append("pins: the attestation's Town key is not the one pinned in summary.json")
    return problems


def cases(root: pathlib.Path) -> list[pathlib.Path]:
    if (root / "bundle").is_dir():
        return [root]
    found = sorted(p.parent for p in root.rglob("bundle") if (p / "manifest.json").is_file())
    return found


def main() -> int:
    root = pathlib.Path(sys.argv[1]).resolve()
    ok = True
    cs = cases(root)
    if not cs:
        print(f"no case folders under {root}")
        return 1
    # the committed pins: summary.json in the evidence root; for a single case folder, in its parent folders
    # (QA 95e5d91 L3, QA 0972d93 L1). No pins found is always a problem.
    base = next((d for d in (root, root.parent, root.parent.parent) if (d / "summary.json").is_file()), None)
    pin_by_dir = {r["dir"]: r for r in json.loads((base / "summary.json").read_text(encoding="utf-8"))["runs"]}         if base else {}
    if not pin_by_dir:
        print(f"BAD no summary.json at or above {root}: the key pins cannot be checked")
        ok = False
    for c in cs:
        rel = c.relative_to(base).as_posix() if base else c.name
        probs = check_case(c, pin_by_dir.get(rel) if pin_by_dir else None)
        if pin_by_dir and rel not in pin_by_dir:
            probs.append("pins: this case is not listed in summary.json")
        ok = ok and not probs
        print(f"{'OK ' if not probs else 'BAD'} {c.relative_to(root) if c != root else c.name}")
        for p in probs:
            print(f"    {p}")
    leaked = [m for m in ("bona", "bench") if m in sys.modules or any(k.startswith(m + ".") for k in sys.modules)]
    print(f"import guard: {'OK, no bona/bench module imported' if not leaked else 'LEAK ' + str(leaked)}")
    return 0 if ok and not leaked else 1


if __name__ == "__main__":
    sys.exit(main())
