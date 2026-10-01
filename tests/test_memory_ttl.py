"""Expiring memory (memory: ttl.v1): an expired entry is never served."""

from nandatown.sim.runner import build_engine
from nandatown.sim.scenario import load_bundled
from nandatown.sim.validators import evaluate_scenario

SCENARIO = "marketplace_expiring_memory"


def _run(memory_plugin=None):
    spec = load_bundled(SCENARIO)
    if memory_plugin:
        spec.layers["memory"] = memory_plugin
    engine = build_engine(spec)
    engine.run()
    return engine, evaluate_scenario(spec, engine.run_id, engine.events)


def _stage(result, name):
    return next(s for s in result.stages if s.name == name)


def test_entry_served_before_expiry_and_dropped_after():
    engine = build_engine(load_bundled(SCENARIO))
    mem = engine.layers["memory"]
    engine.now = 1.0
    mem.remember("buyer-1", "preferred_seller", "seller-b")
    engine.now = 1.4
    assert mem.recall("buyer-1", "preferred_seller") == "seller-b"
    engine.now = 1.5
    assert mem.recall("buyer-1", "preferred_seller") is None
    assert any(e.kind == "memory_expired" for e in engine.events)


def test_key_without_a_declared_limit_never_expires():
    engine = build_engine(load_bundled(SCENARIO))
    mem = engine.layers["memory"]
    engine.now = 1.0
    mem.remember("buyer-1", "other_key", "x")
    engine.now = 999.0
    assert mem.recall("buyer-1", "other_key") == "x"


def test_marketplace_rediscovers_after_expiry():
    engine, result = _run()
    assert _stage(result, "memory_expiry").status == "passed"
    assert any(e.kind == "memory_expired" for e in engine.events)


def test_negative_control_serves_the_stale_entry_and_fails():
    engine, result = _run("ttl_off.v1")
    assert _stage(result, "memory_expiry").status == "failed"
    assert any(e.kind == "memory_recalled" and e.detail.get("stale")
               for e in engine.events)
