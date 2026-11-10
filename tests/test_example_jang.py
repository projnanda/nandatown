"""The JANG example runs in CI without a JANG server.

With JANG unreachable every JANG stage must be inconclusive, never passed or
failed. The signing, agreement-verification and strategy checks run offline
against fixtures recorded from JANGmarket 4fb4eca.
"""

import importlib.util
import json
import os
import sys

import pytest

from nandatown.sim.runner import run_lab

HERE = os.path.join(os.path.dirname(__file__), "..", "examples", "jang")
SCENARIOS = ["jang_market.yaml", "jang_mandate_violation.yaml",
             "jang_tampered_agreement.yaml", "jang_tampered_agreement_unverified.yaml",
             "jang_no_zopa.yaml", "jang_claude.yaml"]
STANDING = {"ledger_conserved", "privacy"}


def _load(name):
    path = os.path.join(HERE, name)
    spec = importlib.util.spec_from_file_location(f"jang_example_{name[:-3].replace('/', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module here
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_unreachable_jang_is_inconclusive(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("JANG_URL", "http://127.0.0.1:9")
    _, result = run_lab(os.path.join(HERE, scenario), str(tmp_path))
    judged = [s for s in result.stages if s.name not in STANDING]
    assert result.verdict != "passed"
    assert judged and all(s.status == "not_enough_evidence" for s in judged), \
        [(s.name, s.status) for s in judged]


def test_canonical_json_matches_jang():
    sign = _load("jang_sign.py")
    with open(os.path.join(HERE, "fixtures", "canonical_golden.json"), encoding="utf-8") as fh:
        golden = json.load(fh)
    for case in golden["cases"]:
        assert sign.canonical_json(case["input"]) == case["expected"]


def test_agreement_signature_and_tamper():
    sign = _load("jang_sign.py")
    with open(os.path.join(HERE, "fixtures", "agreement.json")) as fh:
        fx = json.load(fh)
    key, agreement = fx["trusted_jang_public_key"], fx["agreement"]
    assert sign.verify_agreement(agreement, key)
    tampered = {**agreement, "terms": {**agreement["terms"],
                                       "price": agreement["terms"]["price"] + 90}}
    assert not sign.verify_agreement(tampered, key)
    # A forged agreement carrying its own key must not verify against the trusted one.
    other = sign.OwnerKey.generate()
    forged = {**tampered, "jangPublicKey": other.public_pem,
              "signature": sign.sign_payload(
                  {k: v for k, v in tampered.items()
                   if k not in sign.AGREEMENT_ENVELOPE_KEYS}, other.private_pem)}
    assert not sign.verify_agreement(forged, key)


def test_python_mandate_signature_round_trips():
    sign = _load("jang_sign.py")
    owner = sign.OwnerKey.generate()
    body = {"mandateId": "m", "agentId": "a", "ownerId": "o", "role": "buyer", "itemId": "i",
            "constraints": {"price": {"max": 1300}},
            "preferences": {"price": {"weight": 5, "ideal": 999.0, "worst": 1300}},
            "reservationUtility": 0.2, "expiresAt": "2099-01-01T00:00:00.000Z"}
    mandate = sign.PythonSigner().sign_mandate(body, owner)
    assert sign.verify_payload(sign.mandate_body(mandate), mandate["signature"], owner.public_pem)
    assert '"ideal":999,' in sign.canonical_json(body)  # JS writes 999.0 as 999


def test_strategy_port_matches_typescript():
    strategy = _load("jang_strategy.py")
    cases = _load("parity/record_cases.py")
    with open(os.path.join(HERE, "parity", "decide_cases.json")) as fh:
        data = json.load(fh)
    assert len(data["cases"]) >= 100
    for i, case in enumerate(data["cases"]):
        move = strategy.decide(cases.expand(data, case), case["options"])
        move = {k: v for k, v in move.items() if k not in ("note", "reason", "targetUtility")}
        assert move == case["ts"], (i, case["scenario"], case["options"])
