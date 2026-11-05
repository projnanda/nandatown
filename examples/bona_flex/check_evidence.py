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
   are what Town's manifest covers).
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


def check_case(case: pathlib.Path) -> list[str]:
    from nandatown.bundle import verify_bundle
    problems: list[str] = []
    bundle, side = case / "bundle", case / "bona_sidecar"
    problems += [f"town: {p}" for p in verify_bundle(str(bundle))]
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
    for c in cs:
        probs = check_case(c)
        ok = ok and not probs
        print(f"{'OK ' if not probs else 'BAD'} {c.relative_to(root) if c != root else c.name}")
        for p in probs:
            print(f"    {p}")
    leaked = [m for m in ("bona", "bench") if m in sys.modules or any(k.startswith(m + ".") for k in sys.modules)]
    print(f"import guard: {'OK, no bona/bench module imported' if not leaked else 'LEAK ' + str(leaked)}")
    return 0 if ok and not leaked else 1


if __name__ == "__main__":
    sys.exit(main())
