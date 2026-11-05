"""The bring-your-own-buyer sealed-data market, run without a model.

LLM_MARKET_MODEL=scripted:v1 swaps inference for a fixed policy (buy the
cheapest listing, shop again after a failure), so the market's wiring,
the guardrail and the evidence are tested in CI. A real model run is
`LLM_MARKET_MODEL=llama3.1:8b nandatown run examples/llm_market/llm_market.yaml`
against a local Ollama.
"""

import importlib.util
import os

import pytest

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.sim.runner import run_lab

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples",
                       "llm_market")


@pytest.fixture(autouse=True)
def scripted_brain(monkeypatch):
    monkeypatch.setenv("LLM_MARKET_MODEL", "scripted:v1")


def _run(tmp_path, name):
    return run_lab(os.path.join(EXAMPLE, f"{name}.yaml"), str(tmp_path))


def _stages(result):
    return {s.name: s for s in result.stages}


def test_market_on_hashlock_passes_and_verifies(tmp_path):
    bundle_dir, result = _run(tmp_path, "llm_market")
    stages = _stages(result)
    assert result.verdict == "passed", {
        n: (s.status, s.note) for n, s in stages.items()}
    assert stages["damaged_box_refused"].status == "not_tested"
    assert verify_bundle(bundle_dir) == []
    events = load_bundle(bundle_dir)["events"]
    decisions = [e for e in events if e.kind == "model_decision"]
    assert decisions and all(e.detail["model"] == "scripted:v1"
                             for e in decisions)
    # The cheapest seller sealed junk: its claim was refused, the buyer
    # was refunded, rated it down and shopped again.
    refused = {e.subject: e.detail["reason"] for e in events
               if e.kind == "claim_refused"}
    assert refused["order-buyer-1-1"] == "content does not match the listing"
    assert any(e.kind == "reputation_updated" and e.subject == "seller-b"
               and e.detail["outcome"] == "bad" for e in events)
    assert any(e.detail.get("tool") == "shop_again" for e in decisions)
    opened = [e for e in events if e.kind == "goods_opened"]
    assert opened and all(e.detail["escrow"] == "released" for e in opened)


def test_market_without_hashlock_opens_unpaid_goods_and_fails(tmp_path):
    bundle_dir, result = _run(tmp_path, "llm_market_no_hashlock")
    stages = _stages(result)
    assert result.verdict == "failed"
    assert stages["atomic_exchange"].status == "failed"
    assert "goods opened without payment" in stages["atomic_exchange"].note
    assert stages["escrow_resolved"].status == "passed"
    assert verify_bundle(bundle_dir) == []


def test_the_control_differs_only_in_the_payments_layer():
    import yaml

    def load(name):
        with open(os.path.join(EXAMPLE, f"{name}.yaml")) as f:
            return yaml.safe_load(f)

    on, off = load("llm_market"), load("llm_market_no_hashlock")
    assert (on["layers"]["payments"], off["layers"]["payments"]) == (
        "hashlock.v1", "deadline.v1")
    for key in ["agents", "faults", "seed", "validator", "plugin_files"]:
        assert on[key] == off[key], key


def _agents_module():
    spec = importlib.util.spec_from_file_location(
        "llm_market_agents_under_test", os.path.join(EXAMPLE, "agents.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LISTINGS = {"listings": [{"seller": "seller-a", "price_cents": 1500},
                         {"seller": "seller-x", "price_cents": 2500}]}


@pytest.mark.parametrize("stage,tool,args,problem", [
    ("choose", "buy_from", {"seller": "seller-a", "reason": "r"}, None),
    ("choose", "buy_from", {"seller": "seller-z", "reason": "r"},
     "is not listed"),
    ("choose", "buy_from", {"seller": "seller-x", "reason": "r"},
     "above your cap"),
    ("choose", "pay_now", {}, "is not one of"),
    ("failed_order", "buy_from", {"seller": "seller-a"}, "is not one of"),
    ("failed_order", "shop_again", {"reason": "r"}, None),
])
def test_guardrail_decides_what_the_model_may_do(stage, tool, args,
                                                 problem):
    """The model proposes; code decides. Unlisted sellers, prices above
    the cap and tools outside the current stage are refused."""
    buyer = object.__new__(_agents_module().LLMDataBuyer)
    buyer.cap = 2000
    found = buyer._check(stage, LISTINGS, tool, args)
    if problem is None:
        assert found is None
    else:
        assert problem in found
