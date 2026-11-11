"""Capture fresh scorecards from the live gym into the fixture.

Calls the public evaluation endpoint once (one of its rate-limited runs) and
writes ``fixtures/sme_loan_cards.json``. Every card is checked with Town's
own reader before anything is written, so a fixture never holds a card that
did not verify at capture time.

Example::

    python integrations/agents_gym/tools/capture_fixture.py
    python integrations/agents_gym/tools/capture_fixture.py --from-file response.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"nandatown_agentsgym_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=None,
                        help="evaluation endpoint (default: the public gym)")
    parser.add_argument("--from-file", default=None,
                        help="build from a saved endpoint response instead")
    parser.add_argument("--out", default=str(HERE / "fixtures"
                                             / "sme_loan_cards.json"))
    args = parser.parse_args(argv)

    gs = _load("gym_source")
    sc = _load("scorecard")
    url = args.url or gs.DEFAULT_LIVE_URL
    if args.from_file:
        source = gs.GymSource.from_response(
            url, json.loads(Path(args.from_file).read_text()))
    else:
        source = gs.GymSource.live(url)
    if source.error:
        print(f"capture failed: {source.error}")
        return 1
    for name in gs.CANDIDATES:
        card = source.card_for(name)
        if card is None:
            print(f"capture failed: no card for {name}")
            return 1
        verdict = sc.verify(card, pinned_key=gs.PINNED_KEY,
                            as_of=source.captured_at)
        if not verdict.ok:
            print(f"capture failed: {name} card does not verify:"
                  f" {verdict.reasons}")
            return 1
    fixture = {
        "schema": "nandatown.agentsgym.fixture.v1",
        "endpoint": url,
        "captured_at": source.captured_at,
        "workflow": gs.WORKFLOW,
        "perturbation": gs.PERTURBATION,
        "pinned_key": gs.PINNED_KEY,
        "key_id": "agk_02d9388016c5e166",
        "note": ("Scorecards for Agents Gym's five scripted candidates,"
                 " issued by the production gym. gym_verify is the gym"
                 " side's own four-level verification at capture time."),
        "candidates": {
            name: {"signed_card": source.card_for(name),
                   "gym_verify": source.gym_verdicts.get(name, {})}
            for name in gs.CANDIDATES
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out} (captured {source.captured_at})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
