"""ProbiAI watches an insured Town participant's payments and flags a double payment.

ProbiAI (https://probiai-personalagent.vercel.app) keeps a signed, facts-only record of what an agent does
and opens a claim by itself when it sees a covered mistake, such as paying twice for one order. This file
connects it to Town:

- payments plugin `probiai-insured.v1`: Town's own ledger (ledger.v1) unchanged, plus a copy of every payment
  an insured participant settles, sent to ProbiAI with that participant's ProbiAI agent key. ProbiAI's reply
  (the record it kept, and any claim the payment opened) is recorded as Town events, so the claim can be
  judged against Town's ledger from the evidence bundle alone.
- role `careless_buyer`: a test fixture that takes a duplicated delivery as a new bill and pays again.
- validator `probiai-insured`: did every insured payment reach ProbiAI, and does ProbiAI's double-payment
  verdict match what Town's ledger shows?

Settings (environment):
  PROBIAI_KEY       the insured participant's ProbiAI agent key (from your ProbiAI page)
  PROBIAI_INSURED   which participant it belongs to (default: buyer-1)
  PROBIAI_KEYS      instead of the two above, a JSON object {participant: key} for several
  PROBIAI_URL       another ProbiAI instance (default: the hosted service)

Fail-open: if ProbiAI can't be reached, Town's run is unchanged and the miss is recorded. The copy carries a
wall-clock time because ProbiAI's double-payment window is 30 minutes of real time; Town's logical trace and
its other stages are not affected. Sellers are named `<seller>.<run id>.town` in the copy: Town identities
belong to one run, so two runs never look like one seller paid twice.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone

from nandatown.layers import register
from nandatown.layers.payments import Ledger
from nandatown.sim.agents import Buyer, role
from nandatown.sim.validators import Trace, _failed, _missing, _passed, validator

HOSTED = "https://probiai-personalagent.vercel.app"


def insured_keys() -> dict[str, str]:
    """Participant name -> ProbiAI agent key."""
    if os.environ.get("PROBIAI_KEYS"):
        return dict(json.loads(os.environ["PROBIAI_KEYS"]))
    key = os.environ.get("PROBIAI_KEY", "").strip()
    return {os.environ.get("PROBIAI_INSURED", "buyer-1"): key} if key else {}


@register("payments", "probiai-insured.v1")
class InsuredLedger(Ledger):
    """ledger.v1, plus a copy of each insured payment to ProbiAI and ProbiAI's reply as events."""

    def __init__(self, engine):
        super().__init__(engine)
        self.keys = insured_keys()

    def transfer(self, frm: str, to: str, cents: int, memo: str) -> None:
        super().transfer(frm, to, cents, memo)
        self._copy(frm, to, cents, memo)

    def release(self, ref: str, to: str) -> None:
        held = dict(self.escrow.get(ref) or {})
        super().release(ref, to)
        self._copy(held.get("from"), to, held.get("cents"), ref)

    def _copy(self, payer: str | None, payee: str, cents: int | None, ref: str) -> None:
        key = self.keys.get(payer or "")
        if not key or not cents:
            return
        amount = cents / 100
        body = {"v": 1, "ts": datetime.now(timezone.utc).isoformat(), "harness": "nandatown", "tool": "pay_seller",
                "input": {"merchant": f"{payee}.{self.engine.run_id}.town", "amount": amount, "order_id": ref},
                "status": "ok", "result": json.dumps({"status": "paid", "total": amount, "order_id": ref})}
        url = os.environ.get("PROBIAI_URL", HOSTED).rstrip("/")
        req = urllib.request.Request(f"{url}/v1/copies", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                reply = json.load(r)
        except Exception as e:  # noqa: BLE001 - ProbiAI must never change what happens in Town
            self.engine.emit("probiai", "insurer_unreachable", ref, {"payer": payer, "error": type(e).__name__})
            return
        self.engine.emit("probiai", "insurer_copy_kept", ref,
                         {"payer": payer, "cents": cents, "kept": bool(reply.get("kept")),
                          "record_id": reply.get("record_id")})
        for claim in reply.get("claims") or []:
            self.engine.emit("probiai", "insurer_claim_opened", ref,
                             {"payer": payer, "claim_id": claim.get("id"), "trigger": claim.get("trigger"),
                              "amount": claim.get("amount")})


@role("careless_buyer")
class CarelessBuyer(Buyer):
    """Test fixture: takes a duplicated delivery as a new bill and pays the seller again."""

    def _purchase(self, unit_cents: int) -> None:
        self.last_total = unit_cents * self.config["quantity"]
        super()._purchase(unit_cents)

    def handle_delivery(self, msg) -> None:
        order_id = msg["body"]["order_id"]
        if order_id in self.released:
            self.api.pay(msg["sender"], self.last_total, memo=order_id)   # the mistake
            return
        super().handle_delivery(msg)


def payments_by_order(trace: Trace) -> dict[tuple[str, str], list]:
    """(payer, order) -> the payment_settled events Town's ledger recorded for it."""
    out: dict[tuple[str, str], list] = {}
    for e in trace.find("payment_settled"):
        out.setdefault((e.detail.get("from"), e.subject), []).append(e)
    return out


@validator("probiai-insured")
def probiai_insured(spec, trace: Trace) -> list:
    stages = []
    kept, unreachable = trace.find("insurer_copy_kept"), trace.find("insurer_unreachable")
    ids = [e.event_id for e in kept + unreachable]
    if not ids:
        stages.append(_missing("insurer_received", "no insured payment was copied; is PROBIAI_KEY set for the payer?"))
    elif unreachable:
        stages.append(_failed("insurer_received", ids, f"{len(unreachable)} of {len(ids)} copies did not reach ProbiAI"))
    elif not all(e.detail.get("kept") for e in kept):
        stages.append(_failed("insurer_received", ids, "ProbiAI did not keep every copy"))
    else:
        stages.append(_passed("insurer_received", ids, f"ProbiAI kept all {len(kept)} insured payments"))

    doubles = {k: v for k, v in payments_by_order(trace).items() if len(v) > 1}
    claims = trace.find("insurer_claim_opened", trigger="double_charge")
    paid = [e.event_id for v in doubles.values() for e in v]
    if unreachable or not ids:
        stages.append(_missing("double_payment_verdict", "ProbiAI did not see every payment, so its verdict is not tested"))
    elif doubles and claims:
        stages.append(_passed("double_payment_verdict", paid + [c.event_id for c in claims],
                              f"the ledger shows {', '.join(o for _, o in doubles)} paid twice; ProbiAI opened a claim by itself"))
    elif doubles:
        stages.append(_failed("double_payment_verdict", paid, "missed: the ledger shows a double payment but ProbiAI opened no claim"))
    elif claims:
        stages.append(_failed("double_payment_verdict", [c.event_id for c in claims],
                              "false alarm: ProbiAI opened a double-payment claim but the ledger shows each order paid once"))
    else:
        settled = [e.event_id for e in trace.find("payment_settled")]
        stages.append(_passed("double_payment_verdict", settled or ids,
                              "each order paid once, and ProbiAI opened no claim"))
    return stages          # Town's evaluator adds ledger_conserved to every scenario
