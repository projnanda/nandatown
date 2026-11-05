"""`@validator("bona_flex")`: stages for the VeriFlex (Bona) energy-flexibility Lab pack, from the trace alone.

PURE: imports only `nandatown` and the standard library, so a bundle can be re-judged by anyone without Bona's
private code. Every stage reads only Town events (`events.jsonl`); anything the events do not show is reported as
missing (`not_enough_evidence`), never inferred. Receipts themselves live in the hash-bound sidecar and are
re-verified offline by `check_receipts.py`; the stages here judge what the desk recorded about them.

Event vocabulary (observer = the desk; `detail.source` is "bona" for the real desk, "control_model" for the naive
control desk). Ids are aliases (a-N assessment, r-N request, h-N hold); no wall-clock value is ever in an event.
- flex_assessed     a-N  {source, buyer, seller, meter, step, offered_w, feasible_w, status}
- flex_commit       r-N  {source, action: hold|confirm, buyer, assessment, hold, meter_step, w_requested, outcome:
                          hold|confirm|refusal, w_granted, flags, reason_code, seq, receipt_sha256, request_sha256,
                          verified, attempt}          attempt: 1 = first send, 2.. = a retry of the same request
- flex_delivery     h-N  {source, seller, seq, receipt_sha256, verdict, delivered_fraction_bp, verified, attempt}
- flex_trust        <seller> {source, phase: pre|post|repeat, deliveries_verified, committed_wh, tier}
- flex_capacity     <meter@step> {source, assessed_w, live_w}       the desk's final ledger snapshot
- flex_clock        run  {source, checks: {name: true|false}}       the six rev3 §4.4 checks
- bona_sidecar_bound run {source, manifest_sha256, files}           last desk event
Escrow uses Town's own payments layer (escrow_held / escrow_released / escrow_refunded, ref = h-N).
"""
from __future__ import annotations

from nandatown.records import StageResult
from nandatown.sim.validators import _check, _failed, _missing, _passed, validator

CLOCK_CHECKS = ("delivery_due", "freshness", "assessment_valid", "hold_valid", "run_budget", "monotonic_seq")
RECEIPT_EVENTS = ("flex_commit", "flex_delivery")


def _d(e) -> dict:
    return e.detail if isinstance(e.detail, dict) else {}


def _desk_cfg(spec) -> dict:
    for a in spec.agents:
        if a.role in ("flex_desk", "flex_desk_naive"):
            return dict(a.config)
    return {}


def no_double_sell(trace) -> StageResult:
    snaps = trace.find("flex_capacity")
    granted: dict[str, int] = {}
    caps: dict[str, int] = {}
    ev = []
    for e in trace.find("flex_assessed"):
        d = _d(e)
        if d.get("status") == "ok":
            k = f"{d.get('meter')}@{d.get('step')}"
            caps[k] = max(caps.get(k, 0), int(d.get("feasible_w") or 0))
    for e in trace.find("flex_commit"):
        d = _d(e)
        if d.get("action") == "hold" and d.get("outcome") == "hold" and d.get("attempt") == 1:
            k = d.get("meter_step")
            granted[k] = granted.get(k, 0) + int(d.get("w_granted") or 0)
            ev.append(e.event_id)
            if granted[k] > caps.get(k, 0):
                return _failed("no_double_sell", ev, f"{k}: {granted[k]} W held > assessed {caps.get(k, 0)} W")
    for e in snaps:
        d = _d(e)
        ev.append(e.event_id)
        if int(d.get("live_w", 0)) > int(d.get("assessed_w", 0)):
            return _failed("no_double_sell", ev, f"{e.subject}: final live {d.get('live_w')} W > {d.get('assessed_w')} W")
    if not snaps:
        return _missing("no_double_sell", "no final capacity snapshot")
    return _check("no_double_sell", True, ev, "", "held + confirmed W never exceeded the assessed capacity")


def acceptance_order_respected(spec, trace) -> StageResult:
    want = _desk_cfg(spec).get("expect_grants")
    if not want:
        return StageResult(name="acceptance_order_respected", status="not_tested",
                           note="no contention in this scenario")
    firsts = [e for e in trace.find("flex_commit", action="hold") if _d(e).get("attempt") == 1]
    got = [[_d(e).get("outcome"), int(_d(e).get("w_granted") or 0), "partial_fill" in (_d(e).get("flags") or [])]
           for e in firsts]
    ok = got == want
    return _check("acceptance_order_respected", ok, [e.event_id for e in firsts],
                  f"grants in Bona's acceptance order were {got}, expected {want}",
                  f"first commit won; grants {got}")


def _retries(trace, action="hold"):
    by_req: dict[str, list] = {}
    for e in trace.find("flex_commit", action=action):
        by_req.setdefault(e.subject, []).append(e)
    return {r: es for r, es in by_req.items() if len(es) > 1}


def retry_replays_original(trace) -> StageResult:
    rs = _retries(trace)
    if not rs:
        return StageResult(name="retry_replays_original", status="not_tested", note="no retried request")
    ev, bad = [], []
    for r, es in rs.items():
        ev += [e.event_id for e in es]
        first = _d(es[0])
        for e in es[1:]:
            d = _d(e)
            if first.get("outcome") == "refusal":
                if d.get("outcome") != "refusal" or d.get("w_granted"):
                    bad.append(r)       # a refused hold is not stored: its retry must be refused again, no grant
            elif any(d.get(k) != first.get(k) for k in ("outcome", "hold", "w_granted", "seq", "receipt_sha256")):
                bad.append(f"{r} (fresh grant)")     # another hold, W, seq or receipt: not a replay
            elif not first.get("receipt_sha256"):
                bad.append(f"{r} (unsigned replay)")  # rev3 4.3: "...the same seq and a byte-identical SIGNED receipt"
    return _check("retry_replays_original", not bad, ev, f"retry did not replay a signed original receipt: {bad}",
                  "every retry returned the same seq and a byte-identical signed receipt")


def retry_sends_identical_request(trace) -> StageResult:
    rs = _retries(trace)
    if not rs:
        return StageResult(name="retry_sends_identical_request", status="not_tested", note="no retried request")
    # Observed on the wire (QA 5199c77 M2): the sha256 the BUYER put in each flex_hold it sent (Town's message_sent
    # events), and the sha256 of the bytes the desk then submitted; both must agree across every attempt.
    sent: dict[str, list] = {}
    for e in trace.find("message_sent", kind="flex_hold"):
        b = _d(e).get("body") or {}
        sent.setdefault(b.get("request"), []).append((e.event_id, b.get("request_sha256")))
    bad, ev = [], []
    for r, es in rs.items():
        wire = sent.get(r, [])
        ev += [e.event_id for e in es] + [i for i, _ in wire]
        hashes = {h for _, h in wire} | {_d(e).get("request_sha256") for e in es}
        if len(hashes) != 1 or None in hashes or not wire:
            bad.append(r)
    return _check("retry_sends_identical_request", not bad, ev, f"retried bytes differ or were not observed: {bad}",
                  "every resend carried the same request sha256 on the wire, and the desk submitted exactly those bytes")


def duplicate_no_extra_commitment(trace) -> StageResult:
    holds = [e for e in trace.find("flex_commit", action="hold") if _d(e).get("outcome") == "hold"]
    if not holds:
        return _missing("duplicate_no_extra_commitment", "no granted hold")
    per: dict[str, set] = {}
    for e in holds:
        per.setdefault(e.subject, set()).add(_d(e).get("hold"))
    bad = [r for r, hs in per.items() if len(hs) != 1]
    dups = [e.event_id for e in trace.find("message_duplicated", kind="flex_hold")]
    ev = [e.event_id for e in holds] + dups
    return _check("duplicate_no_extra_commitment", not bad, ev, f"one request produced several holds: {bad}",
                  f"exactly one hold per (buyer, idempotency key); {len(dups)} duplicated delivery(ies) seen")


def settled_once(trace) -> StageResult:
    confirmed = {_d(e).get("hold") for e in trace.find("flex_commit", action="confirm")
                 if _d(e).get("outcome") == "confirm"}
    if not confirmed:
        return _missing("settled_once", "no confirmed commitment")
    dels: dict[str, set] = {}
    ev = []
    for e in trace.find("flex_delivery"):
        dels.setdefault(e.subject, set()).add((_d(e).get("seq"), _d(e).get("receipt_sha256")))
        ev.append(e.event_id)
    # exactly one distinct settlement record per confirmed commitment (a repeat must return it unchanged); whether
    # that record is a verified Bona receipt is receipts_verify's question, not this stage's
    bad = sorted(h for h in confirmed if len(dels.get(h, set())) != 1)
    return _check("settled_once", not bad, ev, f"not exactly one delivery receipt for {bad}",
                  "one delivery receipt per confirmed commitment; repeated settles returned it unchanged")


def trust_accounted_once(trace) -> StageResult:
    settled: dict[str, int] = {}
    for e in trace.find("flex_delivery"):
        if _d(e).get("attempt") == 1 and _d(e).get("verdict") in ("delivered", "shortfall"):
            seller = _d(e).get("seller")
            settled[seller] = settled.get(seller, 0) + 1
    ev, bad = [], []
    sellers = {e.subject for e in trace.find("flex_trust")}
    if not sellers:
        return _missing("trust_accounted_once", "no trust query")
    for s in sorted(sellers):
        ph = {_d(e).get("phase"): _d(e) for e in trace.find("flex_trust", subject=s)}
        ev += trace.ids("flex_trust", subject=s)
        if "pre" not in ph or "post" not in ph:
            bad.append(f"{s}: pre/post missing")
            continue
        delta = int(ph["post"].get("deliveries_verified") or 0) - int(ph["pre"].get("deliveries_verified") or 0)
        if delta != settled.get(s, 0):
            bad.append(f"{s}: +{delta} verified, {settled.get(s, 0)} settled")
        if "repeat" in ph and {k: v for k, v in ph["repeat"].items() if k != "phase"} != \
                {k: v for k, v in ph["post"].items() if k != "phase"}:
            bad.append(f"{s}: a repeated trust query changed")
    return _check("trust_accounted_once", not bad, ev, "; ".join(bad),
                  "trust moved by exactly the settled commitments; repeats changed nothing")


def receipts_verify(trace) -> StageResult:
    es = [e for k in RECEIPT_EVENTS for e in trace.find(k)]
    if not es:
        return _missing("receipts_verify", "no receipt-bearing event")
    bad = [e.event_id for e in es if _d(e).get("source") != "bona" or not _d(e).get("receipt_sha256")
           or _d(e).get("verified") is not True]
    return _check("receipts_verify", not bad, [e.event_id for e in es],
                  f"{len(bad)} event(s) without a verified Bona receipt (e.g. {bad[:3]})",
                  "every commitment and delivery carries a Bona receipt the desk verified (re-checked offline)")


def money_follows_verdict(trace) -> StageResult:
    verdict = {e.subject: _d(e).get("verdict") for e in trace.find("flex_delivery") if _d(e).get("attempt") == 1}
    held = {e.subject for e in trace.find("escrow_held")}
    if not held:
        return _missing("money_follows_verdict", "no escrow hold")
    rel = {e.subject for e in trace.find("escrow_released")}
    ref = {e.subject for e in trace.find("escrow_refunded")}
    bad = [h for h in held if (h in rel) == (h in ref)
           or (h in rel and verdict.get(h) != "delivered") or (h in ref and verdict.get(h) == "delivered")]
    ev = trace.ids("escrow_held") + trace.ids("escrow_released") + trace.ids("escrow_refunded")
    return _check("money_follows_verdict", not bad, ev, f"escrow not settled by verdict: {sorted(bad)}",
                  "escrow released only for credited verdicts, refunded otherwise")


def capacity_not_disclosed(spec, trace) -> StageResult:
    desk = next((a.name for a in spec.agents if a.role in ("flex_desk", "flex_desk_naive")), None)
    buyers = {a.name for a in spec.agents if a.role == "flex_buyer"}
    replies = [e for e in trace.find("message_sent", observer=desk)
               if isinstance(_d(e).get("body"), dict) and _d(e).get("to") in buyers]
    if not replies:
        return _missing("capacity_not_disclosed", "no desk reply")
    banned = ("remaining_w", "cap_w", "committed_w", "other_buyer", "live_w")

    def leaks(o):
        if isinstance(o, dict):
            return any(k in banned or leaks(v) for k, v in o.items())
        if isinstance(o, list):
            return any(leaks(v) for v in o)
        return False
    bad = [e.event_id for e in replies if leaks(_d(e)["body"])]
    return _check("capacity_not_disclosed", not bad, [e.event_id for e in replies],
                  "a desk reply disclosed capacity or another buyer's W", "no desk reply disclosed capacity")


def clock_valid(trace) -> StageResult:
    es = trace.find("flex_clock")
    if not es:
        return _missing("clock_valid", "no clock checks recorded")
    checks = _d(es[-1]).get("checks") or {}
    missing = [c for c in CLOCK_CHECKS if c not in checks]
    failed = [c for c in CLOCK_CHECKS if checks.get(c) is False]
    if missing:
        return _missing("clock_valid", f"clock checks missing: {missing}")
    if failed:
        return StageResult(name="clock_valid", status="not_enough_evidence", evidence=[es[-1].event_id],
                           note=f"INCOMPLETE: clock checks failed {failed}; the run proves nothing")
    return _passed("clock_valid", [es[-1].event_id], "all six clock-validity checks held")


def sidecar_bound(trace) -> StageResult:
    es = trace.find("bona_sidecar_bound")
    if not es:
        return _missing("sidecar_bound", "no bona_sidecar_bound event")
    last = trace.events[-1] if trace.events else None
    desk_events = [e for e in trace.events if e.kind.startswith("flex_") or e.kind == "bona_sidecar_bound"]
    ok = bool(_d(es[-1]).get("manifest_sha256")) and len(es) == 1 and desk_events[-1] is es[-1]
    return _check("sidecar_bound", ok, [es[-1].event_id] + ([last.event_id] if last else []),
                  "the sidecar binding is not the single, last desk event",
                  "sidecar manifest sha256 bound in the trace (files re-checked offline)")


@validator("bona_flex")
def bona_flex(spec, trace) -> list[StageResult]:
    return [no_double_sell(trace), acceptance_order_respected(spec, trace), retry_replays_original(trace),
            retry_sends_identical_request(trace), duplicate_no_extra_commitment(trace), settled_once(trace),
            trust_accounted_once(trace), receipts_verify(trace), money_follows_verdict(trace),
            capacity_not_disclosed(spec, trace), clock_valid(trace), sidecar_bound(trace)]
