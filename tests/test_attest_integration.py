"""Attest x Town: an Attest-gated seller and a signing buyer through the Track.

Skipped unless ATTEST_HOME points at a checkout of github.com/smghaas/attest
(commit pinned in examples/attest/README.md). Starts the real gate over HTTP.
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ATTEST_HOME = os.environ.get("ATTEST_HOME")
pytestmark = pytest.mark.skipif(not ATTEST_HOME,
                                reason="set ATTEST_HOME to an Attest checkout")

from nandatown.runner import run_town  # noqa: E402

EX = Path(__file__).resolve().parent.parent / "examples" / "attest"


def _start_gate(tmp_path, monkeypatch, sandbox=False):
    """Seed the cast and start the real gate over HTTP on a free port. sandbox=True runs it as
    Attest's public sandbox (ATTEST_SANDBOX=1): self-registration and free test stake."""
    subprocess.run([sys.executable, str(EX / "world.py"), str(tmp_path)], check=True,
                   env={**os.environ, "ATTEST_HOME": ATTEST_HOME})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "ATTEST_STATE_DIR": str(tmp_path / "gate"),
           "ATTEST_LOG_DIR": str(tmp_path / "logs")}
    env.pop("ATTEST_SANDBOX", None)
    if sandbox:
        env["ATTEST_SANDBOX"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "gate.server:app", "--port", str(port)],
        cwd=ATTEST_HOME, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(url + "/pubkey", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    monkeypatch.setenv("ATTEST_URL", url)
    monkeypatch.setenv("ATTEST_CAST", str(tmp_path / "cast.json"))
    monkeypatch.setenv("DEADLINE", "8")
    return url, proc


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    url, proc = _start_gate(tmp_path, monkeypatch)
    yield url
    proc.kill()
    proc.wait(timeout=10)


@pytest.fixture()
def sandbox_gate(tmp_path, monkeypatch):
    url, proc = _start_gate(tmp_path, monkeypatch, sandbox=True)
    yield url
    proc.kill()
    proc.wait(timeout=10)


def _run(tmp_path, monkeypatch, profile, agent="proc", tamper=False,
         journal=True, stock_buyer=False, action=None, amount=None, signer=None,
         solve_pow=False, discover=False, identity=None, receiver=None, receiver_key=None):
    monkeypatch.setenv("ATTEST_TAMPER", "1" if tamper else "0")
    monkeypatch.setenv("ATTEST_JOURNAL", "1" if journal else "0")
    if agent is None or identity is not None:
        monkeypatch.delenv("ATTEST_CAST", raising=False)     # not a member of the cast
    for key, value in (("ATTEST_AGENT", agent), ("ATTEST_ACTION", action), ("ATTEST_AMOUNT", amount),
                       ("ATTEST_SIGNER", signer),
                       ("ATTEST_SOLVE_POW", "1" if solve_pow else None),
                       ("ATTEST_DISCOVER", "1" if discover else None),
                       ("ATTEST_IDENTITY", identity), ("ATTEST_RECEIVER", receiver),
                       ("ATTEST_RECEIVER_KEY", receiver_key)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))
    harnesses = {"seller": f"cmd:{sys.executable} {EX / 'attest_seller.py'}"}
    if not stock_buyer:
        harnesses["buyer"] = f"cmd:{sys.executable} {EX / 'attest_buyer.py'}"
    bundle_dir, result = run_town(profile, str(tmp_path / "runs"), harnesses=harnesses)
    stages = {s.name: str(getattr(s.status, "value", s.status)) for s in result.stages}
    events = [json.loads(line) for line in
              (Path(bundle_dir) / "events.jsonl").read_text().splitlines()]
    notes = [(e["observer"], e["detail"]["note"]) for e in events
             if e["kind"] == "ack_recorded"]
    return SimpleNamespace(stages=stages, notes=notes)


def _seller(run):
    return [n for who, n in run.notes if who == "seller"]


def test_signed_buyer_is_served_and_receipt_verifies(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-clean")
    assert r.stages["correct"] == "passed" and r.stages["processed"] == "passed"
    assert _seller(r)[0]["attest_decision"] == "allow"
    assert [n for who, n in r.notes if who == "buyer"][0]["attest_receipt_verified"] is True


@pytest.mark.parametrize("agent,tamper,decision,reason", [
    ("forger", False, "unknown", "pow_required"),          # unstaked signer behind an Acme name
    ("greedy", False, "refuse", "attenuation_violation"),  # sub-mandate wider than its parent's
    ("newcomer", False, "unknown", "pow_required"),        # no credential at all
    ("proc", True, "refuse", "bad_request_signature"),     # amount changed after signing
])
def test_unauthorized_buyer_gets_no_quote(gate, tmp_path, monkeypatch,
                                          agent, tamper, decision, reason):
    r = _run(tmp_path, monkeypatch, "quote-clean", agent=agent, tamper=tamper)
    note = _seller(r)[0]
    assert (note["attest_decision"], note["attest_reason"]) == (decision, reason)
    assert note["applied"] is False
    # Town reads a refusal as missing evidence, not as a passed or failed quote.
    assert r.stages["correct"] == "not_enough_evidence"


def test_stock_town_buyer_is_refused(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-clean", stock_buyer=True)
    note = _seller(r)[0]
    assert note["attest_decision"] == "refuse" and "no KYA envelope" in note["attest_reason"]
    assert note["attest_hint_sent"] is True      # the refusal says where the gate is and what to bring
    assert r.stages["correct"] == "not_enough_evidence"


def test_duplicate_delivery_with_journal_serves_once(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-duplicate-delivery")
    assert r.stages["processed"] == "passed" and r.stages["correct"] == "passed"
    notes = _seller(r)
    assert notes[0]["attest_decision"] == "allow" and notes[1]["duplicate"] is True


def test_duplicate_delivery_without_journal_is_caught_by_the_gate_repeat(gate, tmp_path, monkeypatch):
    # Before the gate answered repeats, the second delivery got `refuse` / `replay`. Now it gets the original
    # answer marked repeat, the seller does not apply it again, and Town sees one application.
    r = _run(tmp_path, monkeypatch, "quote-duplicate-delivery", journal=False)
    notes = _seller(r)
    assert notes[0]["attest_decision"] == "allow" and notes[0]["applied"] is True
    assert notes[1]["attest_repeat"] is True and notes[1]["duplicate"] is True
    assert notes[1]["attest_receipt_id"] == notes[0]["attest_receipt_id"]
    assert r.stages["processed"] == "passed"


# ---- the rest of the gate: delegation, scope, limits, escalation, revocation, slashing,
# ---- proof of work and the issuer stake cap, each judged by what the seller recorded.

def _decision(run, index=0):
    note = _seller(run)[index]
    return note["attest_decision"], note["attest_reason"], note["applied"]


def _gate_world(tmp_path):
    """Attest's own helpers plus the cast, to act on the gate the way an operator or receiver would."""
    sys.path.insert(0, ATTEST_HOME)
    from bench import world as bw
    from gate import crypto
    return bw, crypto, json.loads((tmp_path / "cast.json").read_text())


def test_delegated_sub_agent_is_served(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-clean", agent="sub_ok")
    assert _decision(r) == ("allow", "ok", True)
    assert r.stages["correct"] == "passed"


def test_sub_agent_outside_its_scope_is_refused(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-clean", agent="sub_ok", action="place_order")
    assert _decision(r) == ("refuse", "out_of_scope", False)


def test_order_above_mandate_limit_is_refused(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-clean", action="place_order", amount=12000)
    assert _decision(r) == ("refuse", "over_limit", False)


def test_order_inside_limit_is_served(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-clean", action="place_order", amount=4000)
    assert _decision(r) == ("allow", "ok", True)


def test_order_above_receiver_policy_is_escalated_not_served(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-clean", action="place_order", amount=9000)
    assert _decision(r) == ("escalate", "policy_escalation", False)


def test_order_splitting_is_caught_across_runs(gate, tmp_path, monkeypatch):
    first = _run(tmp_path, monkeypatch, "quote-clean", action="place_order", amount=6000)
    second = _run(tmp_path, monkeypatch, "quote-clean", action="place_order", amount=6000)
    assert _decision(first) == ("allow", "ok", True)
    assert _decision(second) == ("refuse", "cumulative_over_limit", False)


def test_revocation_cascades_to_sub_agents(gate, tmp_path, monkeypatch):
    bw, crypto, cast = _gate_world(tmp_path)
    acme, shady = cast["keys"]["acme"], bw.operator("ShadyCo")
    root = cast["proc"]["chain"][0]

    def revoke(by, reason):
        body = {"revoke": root["id"], "by": by["pub"], "reason": reason}
        return httpx.post(f"{gate}/revoke", json={"document": root, "by": by["pub"],
                                                  "sig": crypto.sign(by["priv"], body),
                                                  "reason": reason})

    assert revoke(shady, "hostile").status_code == 400            # only the signer may revoke
    before = _run(tmp_path, monkeypatch, "quote-clean", agent="sub_ok")
    assert revoke(acme, "key rotated").status_code == 200
    after = _run(tmp_path, monkeypatch, "quote-clean", agent="sub_ok")
    assert _decision(before)[0] == "allow"
    assert _decision(after) == ("refuse", "mandate_revoked", False)


def test_expired_mandate_is_refused(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-clean", agent="expired")
    assert _decision(r) == ("refuse", "mandate_expired", False)


def test_stolen_identity_is_refused(gate, tmp_path, monkeypatch):
    # request names proc but is signed with another key
    r = _run(tmp_path, monkeypatch, "quote-clean", agent="proc", signer="newcomer")
    assert _decision(r) == ("refuse", "bad_request_signature", False)


def test_slashed_operator_loses_access(gate, tmp_path, monkeypatch):
    bw, crypto, cast = _gate_world(tmp_path)
    kw = dict(agent="flaky", action="place_order", amount=1000)
    before = _run(tmp_path, monkeypatch, "quote-clean", **kw)
    # the receiver files a breach claim backed by a receipt the gate signed for it
    receipt = httpx.post(f"{gate}/verify/{cast['receiver']}", json=bw.request(
        cast["flaky"], "place_order", 500, "order later disputed")).json()["receipt"]
    body = {"claim": receipt["id"], "receiver": cast["receiver"], "breach": "payment never sent"}
    seller = cast["keys"]["seller"]
    filed = httpx.post(f"{gate}/claims", json={"receipt": receipt, "breach": body["breach"],
                                               "sig": crypto.sign(seller["priv"], body)})
    assert filed.status_code == 200
    httpx.get(f"{gate}/stakes")                                    # settles the undisputed claim
    after = _run(tmp_path, monkeypatch, "quote-clean", **kw)
    assert _decision(before)[0] == "allow"
    assert _decision(after) == ("refuse", "unstaked_money_action", False)


def test_unstaked_sender_solves_puzzle_for_a_quote_but_never_an_order(gate, tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "quote-clean", agent="newcomer", solve_pow=True)
    asked, solved = _seller(r)[0], _seller(r)[1]
    assert (asked["attest_decision"], asked["attest_reason"]) == ("unknown", "pow_required")
    assert (solved["attest_decision"], solved["attest_reason"]) == ("unknown", "pow_ok")
    assert solved["applied"] is True and solved["total_cents"] == 3990
    money = _run(tmp_path, monkeypatch, "quote-clean", agent="forger", action="place_order",
                 amount=2000)
    assert _decision(money) == ("refuse", "unstaked_money_action", False)


def test_issuer_stake_caps_what_many_agents_can_move(gate, tmp_path, monkeypatch):
    # AcmeCorp staked 25,000; four separate roots each order 7,000
    out = [_decision(_run(tmp_path, monkeypatch, "quote-clean", agent=f"cap{i}",
                          action="place_order", amount=7000)) for i in range(1, 5)]
    assert [d[0] for d in out[:3]] == ["allow"] * 3
    assert out[3] == ("refuse", "issuer_exposure_over_stake", False)


# ---- finding the gate from inside Town, and agents that signed themselves up ----

def _buyer(run):
    return [n for who, n in run.notes if who == "buyer"]


def test_stranger_learns_the_gate_from_the_seller_and_gets_a_public_quote(gate, tmp_path, monkeypatch):
    # The buyer has no credential and is told nothing about Attest: no gate URL, no receiver
    # name. It asks like any Town buyer, reads the refusal, and comes back with an envelope.
    r = _run(tmp_path, monkeypatch, "quote-clean", agent=None, discover=True, solve_pow=True)
    plain, asked, solved = _seller(r)[:3]
    assert (plain["attest_decision"], plain["attest_hint_sent"]) == ("refuse", True)
    assert (asked["attest_decision"], asked["attest_reason"]) == ("unknown", "pow_required")
    assert (solved["attest_reason"], solved["applied"], solved["total_cents"]) == ("pow_ok", True, 3990)
    learned = _buyer(r)[0]
    assert (learned["learned_gate"], learned["learned_receiver"]) == (gate, "supplierco")
    assert _buyer(r)[-1]["attest_receipt_verified"] is True
    assert _buyer(r)[-1]["correct"] is True


def test_self_registered_operator_is_served_through_town(sandbox_gate, tmp_path, monkeypatch):
    # Everything here is done the way an outside team would do it, with Attest's own SDK
    # against a sandbox gate: no cast, no access to the gate's state.
    monkeypatch.setenv("ATTEST_DIR", str(tmp_path / "ids"))
    sys.path.insert(0, ATTEST_HOME)
    from attest import identity
    from attest.client import Attest
    sdk = Attest(sandbox_gate)
    op = identity.new_operator("Widget Buyers")
    assert sdk.faucet(op)["stake"] > 0                              # free test stake, once
    shop = identity.new_receiver("town-seller")
    sdk.register_receiver(shop, min_stake=5000, escalate_above={"place_order": 1500})
    identity.new_agent("wb-procurement", op, "buys widgets", ["request_quote", "place_order"], 2000)
    me = str(tmp_path / "ids" / "wb-procurement.json")
    shop_key = str(tmp_path / "ids" / "town-seller.json")       # the seller signs its own checks

    quote = _run(tmp_path, monkeypatch, "quote-clean", identity=me, receiver="town-seller", receiver_key=shop_key,
                 discover=True)
    assert _seller(quote)[0]["attest_decision"] == "refuse"            # no envelope yet
    assert _decision(quote, 1) == ("allow", "ok", True)
    assert _buyer(quote)[0]["learned_receiver"] == "town-seller"
    assert _buyer(quote)[-1]["attest_receipt_verified"] is True
    assert _buyer(quote)[-1]["correct"] is True
    # Town grades quote-clean as one request and one response, matched to the first request.
    # Discovery takes two requests (plain, then with an envelope), so Town's own correct stage
    # does not pass here; the buyer's note above is the check.

    big = _run(tmp_path, monkeypatch, "quote-clean", identity=me, receiver="town-seller", receiver_key=shop_key,
               action="place_order", amount=1800)
    assert _decision(big) == ("escalate", "policy_escalation", False)   # its own policy
