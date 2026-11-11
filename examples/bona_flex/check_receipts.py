"""Offline Ed25519 check of every Bona receipt in a case's sidecar. Standard library + `cryptography` only.

    python examples/bona_flex/check_receipts.py <case dir>     (the folder holding bona_sidecar/)

Each `bona_sidecar/receipts/<seq>.json` must verify (Ed25519 over RFC 8785 canonical JSON) against the run's Bona
did:key recorded in `bona_sidecar/provenance.json`, carry `env: bench`, and have the seq its file name says. As a
self-test, changing any one signed field of the first receipt must make it fail.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bona_flex_offline import verify_receipt  # noqa: E402


def pinned_did(case: pathlib.Path) -> str | None:
    """The run's Bona did:key pinned in the evidence root's committed summary.json (QA 95e5d91 L3)."""
    for root in (case.parent, case.parent.parent):
        s = root / "summary.json"
        if s.is_file():
            rel = case.relative_to(root).as_posix()
            for r in json.loads(s.read_text(encoding="utf-8"))["runs"]:
                if r["dir"] == rel:
                    return r.get("bona_did")
    return None


def check(case: pathlib.Path, did_pin: str | None = None) -> tuple[int, list[str]]:
    side = case / "bona_sidecar"
    did = json.loads((side / "provenance.json").read_bytes().decode("utf-8")).get("bona_did")
    pin = did_pin or pinned_did(case)
    if pin is None:
        return 0, ["no pinned Bona did:key (pass --did, or run inside an evidence root with summary.json)"]
    if did != pin:
        return 0, [f"the sidecar's did:key {did} is not the pinned {pin}"]
    if not did:
        return 0, ["no Bona did:key in provenance.json (a control run has no Bona receipts)"]
    probs, n = [], 0
    files = sorted((side / "receipts").glob("*.json"), key=lambda p: int(p.stem))
    for p in files:
        r = json.loads(p.read_bytes().decode("utf-8"))
        n += 1
        if str(r.get("seq")) != p.stem:
            probs.append(f"{p.name}: seq {r.get('seq')} != file name")
        if not verify_receipt(r, did):
            probs.append(f"{p.name}: signature does NOT verify")
    if files:
        r = json.loads(files[0].read_bytes().decode("utf-8"))
        for k in [k for k in r if k != "signature"]:
            v = r[k]
            m = dict(r, **{k: (v + 1 if isinstance(v, int) and not isinstance(v, bool) else f"{v}x")})
            if verify_receipt(m, did):
                probs.append(f"self-test: changing {k} did not break the signature")
    if n == 0:
        probs.append("no receipt in the sidecar")
    return n, probs


def main() -> int:
    case = pathlib.Path(sys.argv[1]).resolve()
    pin = sys.argv[sys.argv.index("--did") + 1] if "--did" in sys.argv else None
    n, probs = check(case, pin)
    print(f"{n} receipt(s) checked; {'all verify, every mutation fails' if not probs else 'PROBLEMS'}")
    for p in probs:
        print(f"    {p}")
    return 0 if not probs else 1


if __name__ == "__main__":
    sys.exit(main())
