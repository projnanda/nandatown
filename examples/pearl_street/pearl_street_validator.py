"""Stage checks for pearl_street_peak, independent of Pearl Street's code.

Nothing here imports `exchange`. Ground truth is the scenario recipe (the
roster, each provider's configured offers and planted behaviour, the meter's
attested limits, the operator's needs) plus the meter's own attestations.
Clearing and settlement are recomputed from those and compared with what the
exchange reported, so a wrong award, a wrong price or a settlement that
ignored the meter fails. Every check names the configured roster and fails
when a configured participant is missing from it, rather than checking only
the messages that happened to arrive.

A scenario may post several needs (the operator's `needs` list). Clearing,
settlement and coverage are then checked per event, and each note line is
prefixed with "event N:". With one need the notes read as before.

Rules recomputed (Pearl Street M1, documented in its exchange/clearing.py and
exchange/settlement.py):
- clearing: admitted offers with floor <= price_cap, sorted by (floor,
  arrival); accept until kw_needed is covered, the last one possibly in part;
  one uniform price = the floor of the last accepted offer.
- settlement: delivered_kw = mean over the window's steps of
  accredited_kw * min(1, metered_kw / accredited_kw); credit = price *
  delivered_kw * hours / 1000; charge = price_cap * shortfall_kw * hours / 1000;
  the delivery record is cumulative per agent: delivered kWh / awarded kWh.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nandatown.records import StageResult
from nandatown.sim.validators import validator

TOL = 1e-6
POLICY_CODE = "town_policy_excluded"


def _ok(name, evidence, note=""):
    return StageResult(name=name, status="passed", evidence=evidence[:8], note=note)


def _fail(name, evidence, note):
    return StageResult(name=name, status="failed", evidence=evidence[:8], note=note)


def _missing(name, note):
    return StageResult(name=name, status="not_enough_evidence", note=note)


def _t(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _steps(window, minutes=15):
    t, end, out = _t(window["start_utc"]), _t(window["end_utc"]), []
    while t < end:
        out.append(t.strftime("%Y-%m-%dT%H:%M:%SZ"))
        t += timedelta(minutes=minutes)
    return out


class Recipe:
    """The roster and expectations, read from the scenario, not the trace."""

    def __init__(self, spec):
        by_role = {}
        for a in spec.agents:
            by_role.setdefault(a.role, []).append(a)
        self.exchange = by_role["pearl_exchange"][0].name
        op = by_role["grid_operator"][0]
        self.operator, self.op = op.name, op.config
        self.needs = list(op.config.get("needs") or [op.config])
        self.policy = bool(op.config.get("skip_negative_reputation"))
        meter = by_role["meter"][0]
        self.meter = meter.name
        self.limits = {a["agent"]: a for a in meter.config["assets"]}
        self.providers = {a.name: a.config for a in by_role.get("flex_provider", [])}
        self.multi = len(self.needs) > 1

    def offer(self, provider: str, i: int):
        c = self.providers[provider]
        if "offers" in c:
            return c["offers"][i] if i < len(c["offers"]) else None
        return c.get("offer") if i == 0 else None

    def tag(self, i: int, text: str) -> str:
        return f"event {i + 1}: {text}" if self.multi else text


def _need_ids(r, trace) -> list[str]:
    """Need ids in posting order, matched to the recipe's needs by position."""
    return [e.subject for e in trace.find("pearl_need", observer=r.exchange)]


def _messages(r, trace):
    return trace.find("pearl_message", observer=r.exchange)


def discovery_and_registration(r, trace):
    ev, missing = [], []
    for name in [r.exchange, r.meter, *r.providers]:
        cards = trace.find("card_registered", subject=name, verified=True)
        if not cards:
            missing.append(f"{name}: no verified Town card")
        ev += [e.event_id for e in cards]
        forged = [e for e in trace.find("card_unverified", subject=name)
                  if not cards or trace.index(e) > trace.index(cards[-1])]
        if forged:
            missing.append(f"{name}: verified listing replaced by an unverified card"
                           f" published by {forged[-1].detail.get('publisher')}")
            ev += [e.event_id for e in forged]
    for p in r.providers:
        reg = [e for e in _messages(r, trace)
               if e.detail.get("from") == p and e.detail.get("msg_type") == "register"]
        if not reg:
            missing.append(f"{p}: never sent register")
        elif reg[0].detail.get("outcome") != "accepted":
            missing.append(f"{p}: register rejected ({reg[0].detail.get('code')})")
        else:
            ev.append(reg[0].event_id)
        if not trace.find("receipt_attested", observer=r.meter, subject=p,
                          claim="asset_limits"):
            missing.append(f"{p}: no attested asset limits")
    if missing:
        return _fail("discovery_and_registration", ev, "; ".join(missing))
    return _ok("discovery_and_registration", ev,
               f"exchange, meter and {len(r.providers)} providers published verified"
               " cards; every provider's limits were attested and its register accepted")


def boundary_rejections(r, trace):
    planted = {p: c["planted"] for p, c in r.providers.items() if c.get("planted", {}).get("expect")}
    if not planted:
        return _missing("boundary_rejections", "the scenario plants no boundary case")
    ev, problems, seen = [], [], []
    msgs = _messages(r, trace)
    for p, plant in planted.items():
        want = plant["expect"]
        hit = [e for e in msgs if e.detail.get("from") == p
               and e.detail.get("msg_type") == want["msg_type"]
               and e.detail.get("outcome") == "rejected"
               and e.detail.get("code") == want["code"]]
        if hit:
            ev.append(hit[0].event_id)
            seen.append(f"{p} {want['msg_type']} -> {want['code']}")
        else:
            problems.append(f"{p}: expected {want['msg_type']} rejected as {want['code']}")
    if problems:
        return _fail("boundary_rejections", ev, "; ".join(problems))
    return _ok("boundary_rejections", ev, "; ".join(seen))


def _recompute_clearing(r, trace, i, need_id):
    """Admitted offers for one need (checked against the recipe), the
    recomputed {agent: kW} and the uniform price."""
    admitted = [e for e in _messages(r, trace)
                if e.detail.get("msg_type") == "offer" and e.detail.get("outcome") == "accepted"
                and (not r.multi or e.detail["payload"].get("need_id") == need_id)]
    offers = []
    for seq, e in enumerate(admitted):
        p = e.detail["from"]
        cfg = r.offer(p, i) or {}
        kw, floor = e.detail["payload"]["kw"], e.detail["payload"]["floor_usd_mwh"]
        if (kw, floor) != (cfg.get("kw"), cfg.get("floor_usd_mwh")):
            raise ValueError(r.tag(i, f"{p}'s admitted offer {kw} kW @ {floor} differs from its recipe"))
        offers.append({"agent": p, "kw": kw, "floor": floor, "seq": seq, "event": e.event_id})
    need = r.needs[i]
    cap, left = need["price_cap"], need["kw_needed"]
    accepted = []
    for o in sorted((o for o in offers if o["kw"] > 0 and o["floor"] <= cap),
                    key=lambda o: (o["floor"], o["seq"])):
        if left <= 1e-9:
            break
        q = min(o["kw"], left)
        accepted.append((o, q))
        left -= q
    price = accepted[-1][0]["floor"] if accepted else None
    return offers, {o["agent"]: q for o, q in accepted}, price


def _events(r, trace):
    """(index, need_id) for each recipe need that the exchange broadcast."""
    ids = _need_ids(r, trace)
    if not r.multi:
        return [(0, ids[0] if ids else None)]
    return list(enumerate(ids[:len(r.needs)]))


def _award_events(r, trace, need_id=None):
    return [e for e in trace.find("pearl_award", observer=r.exchange)
            if need_id is None or e.detail["need_id"] == need_id]


def uniform_price_clearing(r, trace):
    ev, problems, notes, any_offer = [], [], [], False
    for i, nid in _events(r, trace):
        try:
            offers, want, price = _recompute_clearing(r, trace, i, nid)
        except ValueError as exc:
            return _fail("uniform_price_clearing", [], str(exc))
        awards = _award_events(r, trace, nid if r.multi else None)
        if not offers:
            continue
        any_offer = True
        got = {e.subject: e.detail for e in awards}
        ev += [o["event"] for o in offers] + [e.event_id for e in awards]
        if set(got) != set(want):
            problems.append(r.tag(i, f"winners {sorted(got)} != recomputed {sorted(want)}"))
        for agent, q in want.items():
            d = got.get(agent)
            if d is None:
                continue
            if abs(d["accredited_kw"] - q) > TOL:
                problems.append(r.tag(i, f"{agent} awarded {d['accredited_kw']} kW, recomputed {q}"))
            if abs(d["price_usd_mwh"] - price) > TOL:
                problems.append(r.tag(i, f"{agent} paid {d['price_usd_mwh']} $/MWh, recomputed uniform {price}"))
        stack = ", ".join(f"{a} {q:g} kW" for a, q in want.items())
        notes.append(r.tag(i, f"{stack}; uniform price {price:g} $/MWh = floor of the last accepted offer"))
    if r.multi and len(_need_ids(r, trace)) != len(r.needs):
        problems.append(f"{len(_need_ids(r, trace))} needs broadcast, recipe has {len(r.needs)}")
    if not any_offer and not problems:
        return _missing("uniform_price_clearing", "no offer was admitted")
    if problems:
        return _fail("uniform_price_clearing", ev, "; ".join(problems))
    return _ok("uniform_price_clearing", ev, "; ".join(notes))


def _meter(r, trace, agent):
    reads = {}
    for e in trace.find("receipt_attested", observer=r.meter, subject=agent, claim="meter_kw"):
        v = e.detail["value"]
        reads[v["t_utc"]] = float(v["kw"])
    return reads


def _expected_settlements(r, trace):
    """Recomputed settlement per award_id, with the cumulative record in
    settlement order."""
    index = {nid: i for i, nid in _events(r, trace)}
    awards = {e.detail["award_id"]: dict(e.detail, agent=e.subject) for e in _award_events(r, trace)}
    out, cum = {}, {}
    for s in trace.find("pearl_settle", observer=r.exchange):
        a = awards.get(s.detail["award_id"])
        if a is None:
            continue
        i = index.get(a["need_id"], 0)
        steps = _steps(r.needs[i]["window"])
        hours = len(steps) * 0.25
        kw, price, cap = a["accredited_kw"], a["price_usd_mwh"], r.needs[i]["price_cap"]
        reads = _meter(r, trace, a["agent"])
        per_step = [kw * min(1.0, max(reads.get(t, 0.0), 0.0) / kw) if kw > 0 else 0.0 for t in steps]
        delivered = sum(per_step) / len(steps)
        d, w = cum.get(a["agent"], (0.0, 0.0))
        d, w = d + delivered * hours, w + kw * hours
        cum[a["agent"]] = (d, w)
        out[s.detail["award_id"]] = {
            "delivered_kw": delivered,
            "credit_usd": price * delivered * hours / 1000.0,
            "charge_usd": cap * max(kw - delivered, 0.0) * hours / 1000.0,
            "new_delivery_record": d / w if w else 1.0,
            "readings": sum(1 for t in steps if t in reads),
            "steps": len(steps), "event": i}
    return out


def settlement_matches_meter(r, trace):
    awards = _award_events(r, trace)
    settles = {e.detail["award_id"]: e for e in trace.find("pearl_settle", observer=r.exchange)}
    if not awards:
        return _missing("settlement_matches_meter", "no award to settle")
    want_all = _expected_settlements(r, trace)
    index = {nid: i for i, nid in _events(r, trace)}
    ev, problems, lines = [], [], []
    for a in awards:
        agent, aid = a.subject, a.detail["award_id"]
        i = index.get(a.detail["need_id"], 0)
        s = settles.get(aid)
        if s is None:
            problems.append(r.tag(i, f"{agent}: awarded but never settled"))
            continue
        want = want_all[aid]
        if want["readings"] != want["steps"]:
            problems.append(r.tag(i, f"{agent}: {want['readings']} meter readings for {want['steps']} steps"))
        for k in ("delivered_kw", "credit_usd", "charge_usd", "new_delivery_record"):
            if abs(s.detail[k] - want[k]) > TOL:
                problems.append(r.tag(i, f"{agent} {k} {s.detail[k]} != recomputed {want[k]}"))
        ev.append(s.event_id)
        lines.append(r.tag(i, f"{agent} delivered {want['delivered_kw']:g} of {a.detail['accredited_kw']:g} kW,"
                              f" credit ${want['credit_usd']:.2f}, charge ${want['charge_usd']:.2f}"))
    if problems:
        return _fail("settlement_matches_meter", ev, "; ".join(problems))
    return _ok("settlement_matches_meter", ev, "; ".join(lines))


def ledger_mirrors_settlement(r, trace):
    settles = trace.find("pearl_settle", observer=r.exchange)
    if not settles:
        return _missing("ledger_mirrors_settlement", "no settlement to pay")
    ev, problems = [], []
    for s in settles:
        agent, aid = s.subject, s.detail["award_id"]
        for memo, frm, to, usd in ((f"pearl-credit:{aid}", r.operator, agent, s.detail["credit_usd"]),
                                   (f"pearl-charge:{aid}", agent, r.operator, s.detail["charge_usd"])):
            cents = int(round(usd * 100))
            paid = trace.find("payment_settled", subject=memo)
            if cents == 0:
                if paid:
                    problems.append(f"{memo}: paid though nothing was owed")
                continue
            if len(paid) != 1 or paid[0].detail != {"from": frm, "to": to, "cents": cents}:
                problems.append(f"{memo}: expected one transfer {frm}->{to} {cents} cents,"
                                f" saw {[p.detail for p in paid]}")
            else:
                ev.append(paid[0].event_id)
    if problems:
        return _fail("ledger_mirrors_settlement", ev, "; ".join(problems))
    return _ok("ledger_mirrors_settlement", ev,
               "every credit was paid operator->provider and every shortfall charge"
               " provider->operator, in cents rounded from the settlement")


def reputation_follows_delivery(r, trace):
    settles = trace.find("pearl_settle", observer=r.exchange)
    if not settles:
        return _missing("reputation_follows_delivery", "no settlement to rate")
    good_record = r.op["good_record"]
    ratings = {}
    for u in trace.find("reputation_updated", observer=r.operator):
        ratings.setdefault(u.subject, []).append(u)
    count = {}
    for s in settles:
        count[s.subject] = count.get(s.subject, 0) + 1
    seen: dict[str, int] = {}
    ev, problems, lines = [], [], []
    for s in settles:
        want = "good" if s.detail["new_delivery_record"] >= good_record else "bad"
        ups = ratings.get(s.subject, [])
        k = seen.get(s.subject, 0)
        seen[s.subject] = k + 1
        if len(ups) != count[s.subject] or ups[k].detail.get("outcome") != want:
            problems.append(f"{s.subject}: expected one {want} rating, saw"
                            f" {[u.detail.get('outcome') for u in ups]}")
        else:
            ev.append(ups[k].event_id)
            lines.append(f"{s.subject} record {s.detail['new_delivery_record']:.3f} -> {want}"
                         f" (score {ups[k].detail['score']})")
    if problems:
        return _fail("reputation_follows_delivery", ev, "; ".join(problems))
    return _ok("reputation_follows_delivery", ev, "; ".join(lines))


def settlement_coverage(r, trace):
    """Every configured provider is accounted for in every event: rejected
    as planted, excluded by the operator's declared policy, or admitted and
    (if it won) settled exactly once. A dropped message cannot turn into a
    pass by leaving a provider out of the checks."""
    settles = trace.find("pearl_settle", observer=r.exchange)
    award_need = {e.detail["award_id"]: e.detail["need_id"] for e in _award_events(r, trace)}
    problems, lines = [], []
    for i, nid in _events(r, trace):
        try:
            _, winners, _ = _recompute_clearing(r, trace, i, nid)
        except ValueError as exc:
            return _fail("settlement_coverage", [], str(exc))
        count = {}
        for s in settles:
            if not r.multi or award_need.get(s.detail["award_id"]) == nid:
                count[s.subject] = count.get(s.subject, 0) + 1
        for p, c in r.providers.items():
            if r.offer(p, i) is None:
                continue
            expect = c.get("planted", {}).get("expect", {})
            offers = [e.detail for e in _messages(r, trace)
                      if e.detail.get("from") == p and e.detail.get("msg_type") == "offer"
                      and (not r.multi or e.detail["payload"].get("need_id") == nid)]
            if expect.get("msg_type") == "offer":
                if p in winners or p in count:
                    problems.append(r.tag(i, f"{p}: planted offer rejection, yet awarded or settled"))
                else:
                    lines.append(r.tag(i, f"{p} rejected"))
            elif r.policy and any(d.get("code") == POLICY_CODE for d in offers):
                if p in winners or p in count:
                    problems.append(r.tag(i, f"{p}: excluded by operator policy, yet awarded or settled"))
                else:
                    lines.append(r.tag(i, f"{p} excluded by operator policy"))
            elif p not in winners:
                seen = ", ".join(f"{d['outcome']}{' ' + d['code'] if d.get('code') else ''}"
                                 f" at {d['t_utc']}" for d in offers) or "never reached the exchange"
                problems.append(r.tag(i, f"{p}: no planted rejection, yet no award (offer: {seen})"))
            elif count.get(p) != 1:
                problems.append(r.tag(i, f"{p}: settled {count.get(p, 0)} times"))
            else:
                lines.append(r.tag(i, f"{p} settled once"))
    if problems:
        return _fail("settlement_coverage", [s.event_id for s in settles], "; ".join(problems))
    return _ok("settlement_coverage", [s.event_id for s in settles],
               f"all {len(r.providers)} configured providers accounted for: " + ", ".join(lines))


def honest_harm(r, trace):
    """Honest providers (no `planted` behaviour) that were charged a shortfall
    or rated down: {provider: [reasons]}."""
    honest = {p for p, c in r.providers.items() if not c.get("planted")}
    harm: dict[str, list[str]] = {}
    for s in trace.find("pearl_settle", observer=r.exchange):
        if s.subject in honest and s.detail["charge_usd"] > TOL:
            harm.setdefault(s.subject, []).append(
                f"charged ${s.detail['charge_usd']:.2f} (record {s.detail['new_delivery_record']:.3f})")
    for u in trace.find("reputation_updated", outcome="bad"):
        if u.subject in honest:
            harm.setdefault(u.subject, []).append("rated bad")
    return harm


def honest_providers_unharmed(r, trace):
    """No honest provider is charged a shortfall or rated down. Under a
    transport fault this is where a lost meter reading or award shows up as
    harm to an agent that did nothing wrong."""
    settles = trace.find("pearl_settle", observer=r.exchange)
    if not settles:
        return _missing("honest_providers_unharmed", "no settlement")
    harm = honest_harm(r, trace)
    ev = [s.event_id for s in settles]
    if harm:
        return _fail("honest_providers_unharmed", ev,
                     "; ".join(f"{p}: {', '.join(v)}" for p, v in sorted(harm.items())))
    return _ok("honest_providers_unharmed", ev,
               "no provider without planted behaviour was charged or rated down")


def awards_within_attested_limits(r, trace):
    """The rule: a provider is never awarded more than its registered,
    metering-attested capacity. Pearl Street caps offers at the mandate the
    provider declares itself (max_kw) and does not compare that declaration
    with the attested power_kw, so this stage is where an overpromise shows."""
    awards = _award_events(r, trace)
    if not awards:
        return _missing("awards_within_attested_limits", "no award was made")
    index = {nid: i for i, nid in _events(r, trace)}
    ev = [e.event_id for e in awards]
    over = []
    for e in awards:
        agent, a = e.subject, e.detail
        i = index.get(a["need_id"], 0)
        limit = r.limits.get(agent, {}).get("power_kw")
        if limit is None:
            over.append(r.tag(i, f"{agent}: no attested limit in the meter's roster"))
        elif a["accredited_kw"] > limit + TOL:
            declared = r.providers[agent]["declared_max_kw"]
            over.append(r.tag(i, f"{agent} awarded {a['accredited_kw']:g} kW against an attested"
                                 f" {limit:g} kW (declared max_kw {declared:g}, accepted unchecked)"))
    if over:
        return _fail("awards_within_attested_limits", ev, "; ".join(over))
    return _ok("awards_within_attested_limits", ev,
               "every award is within the provider's attested power")


@validator("pearl_street_peak")
def pearl_street_peak(spec, trace):
    r = Recipe(spec)
    return [
        discovery_and_registration(r, trace),
        boundary_rejections(r, trace),
        uniform_price_clearing(r, trace),
        settlement_matches_meter(r, trace),
        ledger_mirrors_settlement(r, trace),
        reputation_follows_delivery(r, trace),
        settlement_coverage(r, trace),
        honest_providers_unharmed(r, trace),
        awards_within_attested_limits(r, trace),
    ]
