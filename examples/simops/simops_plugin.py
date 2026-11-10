"""Town roles and a validator that play SimOps AI's Week 9 through the Lab.

SimOps AI is a simulation environment and coordination layer for construction AI agents: each trade's agent
negotiates one shared schedule, and a neutral referee checks every move against site rules (zones, crane,
crew limits, trade order) and commits only what every party signed. It runs as its own service; this file is
the Town side only.

Roles
  simops-desk   A Town service agent. It publishes a card with capability construction.schedule.referee and
                forwards each trade's call to SimOps over MCP with the seat token that came with the call. It
                records every SimOps answer as an attributed Town event, closes the week at `close_at` and
                records SimOps's own audit at `audit_at`. With `trust_sender: true` it ignores the token and
                picks one by sender name: the negative control.
  simops-trade  One trade's agent (civil, concrete, steel, mechanical or electrical). It finds the desk in the
                registry, reads the board and its brief, proposes its unscheduled work, follows the referee's
                alternative when it is refused, offers swaps to other trades over Town messages, answers the
                offers it gets from its brief, and signs what was agreed. Same rules as SimOps's own rule bot.
  simops-llm-trade  The same seat decided by an OpenAI model that sees SimOps's own MCP tool list (optional
                live case; needs OPENAI_API_KEY). Its tool calls go to the desk as Town messages.
  simops-rogue  Sends calls as another trade (a forged sender) and publishes a forged card for it. It has no
                seat token for that trade.

Validator simops_week9: stages judged from the recorded events alone, so `run_check.py --verify` replays them
offline. Seat tokens and brief bodies are declared private (redact_fields) and never leave the run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import Any

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import Trace, _check, _missing, validator


def _link():
    """simops_link.py from this folder, loaded once under its own name so run_check.py shares its SITES."""
    if "simops_link" in sys.modules:
        return sys.modules["simops_link"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simops_link.py")
    spec = importlib.util.spec_from_file_location("simops_link", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["simops_link"] = module
    spec.loader.exec_module(module)
    return module


link = _link()
REFEREE = "construction.schedule.referee"
WRITES = ("propose", "request_swap", "accept_swap", "reject_swap", "commit_week")
APPLIED = ("committed",)


def _site(api, config=None):
    engine = api._engine
    desk = next((a for a in engine.spec.agents if a.role == "simops-desk"), None)
    case = (desk.config if desk else config or {}).get("case", "week9")
    return link.site_for(engine.run_id, case)


# -- the desk -----------------------------------------------------------------------------------------


@role("simops-desk")
class SimOpsDesk(SimAgent):
    def on_start(self):
        self.site = _site(self.api, self.config)
        if self.site.ok:
            self.api.observe("simops_run_opened", self.site.run, self.site.public())
        else:
            self.api.observe("referee_unreachable", self.name, {"error": self.site.error[:200]})
        self.api.register([REFEREE], {"service": "SimOps AI", "case": self.site.case,
                                      "tools": ["get_board", "my_brief", "propose", "request_swap", "accept_swap",
                                                "reject_swap", "simulate_plan", "commit_week", "send_message"],
                                      "auth": "seat token per trade"})
        self.api.later(self.config.get("close_at", 8.0), self.close)
        self.api.later(self.config.get("audit_at", 20.0), self.audit)

    def on_message(self, msg):
        if msg["kind"].startswith("call."):
            return self.forward(msg)
        super().on_message(msg)

    def forward(self, msg):
        body = msg["body"]
        tool, args = body.get("tool", ""), dict(body.get("args") or {})
        if self.config.get("trust_sender"):
            token = self.site.token(msg["sender"])  # the control: identity taken from the Town sender name
        else:
            token = body.get("seat_token")
        if not self.site.ok:
            result = link.refusal("referee_unreachable", "SimOps did not answer when the run was opened.")
        else:
            try:
                result = self.site.seats.call(token, tool, args)
            except link.Unreachable as e:
                result = link.refusal("referee_unreachable", str(e)[:200])
        self.api.observe("simops_call", msg["message_id"], {"from": msg["sender"], "call_id": body.get("call_id"),
                                                             "tool": tool, "args": args, **summary(tool, result)})
        self.api.reply(msg, "simops_result", {"call_id": body.get("call_id"), "tool": tool, "result": result})

    def close(self):
        if not self.site.ok:
            return
        try:
            r = self.site.close_week()
        except (link.Unreachable, RuntimeError) as e:
            self.api.observe("referee_unreachable", self.name, {"at": "close", "error": str(e)[:200]})
            return
        self.api.observe("week_closed", self.site.run, {"status": r.get("status"), "frozen": r.get("frozen"),
                                                        "expired": r.get("expired", [])})

    def audit(self):
        if not self.site.ok:
            return
        try:
            a = self.site.read()
        except (link.Unreachable, RuntimeError) as e:
            self.api.observe("referee_unreachable", self.name, {"at": "audit", "error": str(e)[:200]})
            return
        v = a["verify"]
        self.api.observe("week_board", self.site.run, {
            "board": {t: {"zone": p.get("zone"), "days": p.get("days")} for t, p in a["board"].items()},
            "unscheduled": a["unscheduled"], "board_version": a["board_version"], "commits": a["commits"]})
        self.api.observe("simops_verified", self.site.run, {
            "ok": v["ok"], "violations": v["audit"]["violations"], "replay_ok": v["replay"]["ok"],
            "calls": v["replay"]["calls"], "commits_signed": v["authz"]["ok"], "refused": v["misuse"]["refused"],
            "refused_moved_board": v["misuse"]["moved_board"], "invariants": v["invariants"],
            "log_digest": a["log_digest"], "state_hash": a["state_hash"], "simops": a["simops"]})


def summary(tool, r):
    """What the Town record keeps of a SimOps answer: outcome, reason, thread, versions. No brief body."""
    out = {"ok": bool(r.get("ok")), "status": r.get("status"), "board_version": r.get("board_version")}
    for k in ("reason", "thread", "signatures", "waiting_for", "round"):
        if r.get(k) is not None:
            out[k] = r[k]
    if r.get("conflicts"):
        out["rules"] = sorted({c.get("rule") for c in r["conflicts"]})
    if r.get("alternatives") is not None:
        out["alternatives"] = len(r["alternatives"])
    if tool == "my_brief" and r.get("ok"):
        out["brief_of"] = r.get("trade")
    if tool == "simulate_plan" and r.get("ok"):
        out["hard_violations"] = r.get("hard_violations")
    if not r.get("ok") and r.get("explain"):
        out["explain"] = str(r["explain"])[:240]
    return out


# -- a trade ------------------------------------------------------------------------------------------


@role("simops-trade")
class SimOpsTrade(SimAgent):
    def on_start(self):
        self.trade = self.config.get("trade", self.name)
        self.token = _site(self.api).token(self.trade)  # handed over out of band, like a SimOps invite
        self.desk, self.brief = None, None
        self.offers: dict[str, dict] = {}
        self.answered: set[str] = set()
        self.calls = 0
        self.waiting, self.turn, self.again = None, None, False
        self.api.register([f"construction.trade.{self.trade}"], {"trade": self.trade})
        self.api.later(self.config.get("start_after", 0.2), self.find_desk)

    def find_desk(self):
        cards = self.api.lookup(REFEREE)
        if not cards:
            self.api.observe("referee_missing", self.name, {"capability": REFEREE})
            return
        self.desk = cards[0]["name"]
        self.api.observe("referee_found", self.desk, {"capability": REFEREE, "trade": self.trade})
        self.wake()

    # Turns are generators: each SimOps call is a Town message to the desk, and the turn resumes on its reply.

    def wake(self):
        if self.desk is None:
            return
        if self.turn is not None:
            self.again = True
            return
        self.turn = self.play()
        self._advance(None)

    def _advance(self, result):
        while True:
            try:
                step = self.turn.send(result)
            except StopIteration:
                self.turn = None
                if self.again:
                    self.again = False
                    self.wake()
                return
            if step[0] == "tell":
                _, to, kind, body = step
                self.api.send(to, kind, body)
                result = None
                continue
            _, tool, args = step
            self.calls += 1
            self.waiting = f"{self.trade}-{self.calls}"
            self.api.send(self.desk, f"call.{tool}", {"call_id": self.waiting, "tool": tool, "args": args,
                                                       "seat_token": self.token})
            return

    def handle_simops_result(self, msg):
        b = msg["body"]
        if b.get("call_id") != self.waiting:
            self.api.observe("reply_ignored", msg["message_id"], {"call_id": b.get("call_id"),
                                                                 "reason": "not the call this trade is waiting on"})
            return
        self.waiting = None
        self._advance(b.get("result") or {})

    def handle_swap_offer(self, msg):
        self.offers[msg["body"]["thread"]] = {"from": msg["sender"], "changes": msg["body"]["changes"]}
        self.wake()

    def handle_swap_accepted(self, msg):
        self.wake()

    def handle_swap_signed(self, msg):
        self.wake()

    def handle_swap_rejected(self, msg):
        self.wake()

    def handle_swap_committed(self, msg):
        pass

    def play(self):
        """One turn, the rules of SimOps's rule bot: answer offers, sign agreements, then place own work."""
        me = self.trade
        board = yield ("call", "get_board", {})
        if not board.get("ok"):
            self.api.observe("turn_stopped", self.name, {"reason": board.get("reason"), "at": "get_board"})
            return
        if self.brief is None:
            got = yield ("call", "my_brief", {})
            self.brief = got.get("brief", {}) if got.get("ok") else {}
        owner = {t["id"]: t["trade"] for t in board["tasks"]}

        offered = dict(self.offers)
        for t in board["threads"]:
            if (t["kind"] == "swap" and t["status"] == "open" and t["author"] != me
                    and me not in t.get("accepted", [])):
                offered.setdefault(t["id"], {"from": t["author"], "changes": t["changes"]})
        for tid, offer in offered.items():
            if tid in self.answered:
                continue
            self.answered.add(tid)
            mine = next((c for c in offer["changes"] if owner.get(c["task"]) == me), None)
            parties = sorted({owner.get(c["task"]) for c in offer["changes"]} - {me, None})
            if mine is None:
                continue
            if mine["zone"] in self.brief.get("staged_zones", [mine["zone"]]):
                r = yield ("call", "accept_swap", {"thread": tid})
                if r.get("ok"):
                    for p in parties:
                        yield ("tell", p, "swap_accepted", {"thread": tid})
                else:
                    self.api.observe("offer_refused_by_referee", tid, {"reason": r.get("reason")})
            else:
                yield ("call", "reject_swap", {"thread": tid, "reason": f"Our materials are not staged in "
                                                                       f"{mine['zone']}."})
                yield ("tell", offer["from"], "swap_rejected", {"thread": tid})

        board = yield ("call", "get_board", {})
        if not board.get("ok"):
            return
        for t in board["threads"]:
            if t["status"] == "accepted" and me not in t["signatures"]:
                sim = yield ("call", "simulate_plan", {"thread": t["id"]})
                if sim.get("hard_violations") == 0:
                    r = yield ("call", "commit_week", {"week": board["week"], "thread": t["id"]})
                    others = [p for p in t["parties"] if p != me]
                    kind = "swap_committed" if r.get("status") == "committed" else "swap_signed"
                    if r.get("ok"):
                        for p in others:
                            yield ("tell", p, kind, {"thread": t["id"]})

        live = {c["task"] for t in board["threads"] for c in t["changes"] if t["status"] in ("open", "accepted")}
        for task in board["tasks"]:
            if task["trade"] != me or task["status"] != "unscheduled" or task["id"] in live:
                continue
            first = board["days"][0]
            want = {"task": task["id"], "zone": task["allowed_zones"][0],
                    "days": [first, first + task["duration"] - 1]}
            r = yield ("call", "propose", want)
            if r.get("status") == "conflict" and r.get("alternatives"):
                alt = r["alternatives"][0]
                if alt["type"] == "move":
                    r = yield ("call", "propose", {"task": task["id"], "zone": alt["zone"], "days": alt["days"]})
                else:
                    mine = alt.get("mine", want)
                    theirs = alt.get("theirs") or {k: alt[k] for k in ("task", "zone", "days", "crane_days")
                                                   if k in alt}
                    why = alt.get("why", "").rstrip(".")
                    r = yield ("call", "request_swap", {
                        "with_trade": alt["with"], "mine": mine, "theirs": theirs,
                        "note": f"We need {mine['zone']} on days {mine['days'][0]}-{mine['days'][1]}. {why}."})
                    if r.get("status") == "thread_open":
                        changes = [mine, *(theirs if isinstance(theirs, list) else [theirs])]
                        for p in (alt["with"] if isinstance(alt["with"], list) else [alt["with"]]):
                            yield ("tell", p, "swap_offer", {"thread": r["thread"], "changes": changes})
            if r.get("status") == "drafted":
                sim = yield ("call", "simulate_plan", {"thread": r["thread"]})
                if sim.get("hard_violations") == 0:
                    yield ("call", "commit_week", {"week": board["week"], "thread": r["thread"]})


# -- a trade decided by a model -----------------------------------------------------------------------

BRIEFING = """You are the {trade} trade's scheduling agent on a construction site, Week 9 (days 41 to 45).
You act only through the SimOps tools; your seat is fixed by your token. Read the board and your brief first.
Place your trade's unscheduled work. If the referee refuses a move, it says why and offers alternatives: take one
(request_swap for a swap with another trade). Another trade must accept a swap before anyone signs; when a thread
you are in is accepted, simulate_plan it and sign it with commit_week. Never sign a plan with hard violations.
Stop calling tools when your work is placed, or when you are waiting for another trade to answer."""


@role("simops-llm-trade")
class SimOpsModelTrade(SimOpsTrade):
    """The same trade seat, decided by a model. Each tool call the model makes is a Town message to the desk,
    so it goes through the registry, auth, transport and privacy layers like any other. The harness only tells
    the other trades about swaps the model opened, accepted or signed, as the rule trade does."""

    def on_start(self):
        super().on_start()
        self.model = None
        self.inbox: list[str] = []

    def handle_swap_offer(self, msg):
        self.inbox.append(f"{msg['sender']} offers swap {msg['body']['thread']}: {json.dumps(msg['body']['changes'])}")
        self.offers[msg["body"]["thread"]] = {"from": msg["sender"], "changes": msg["body"]["changes"]}
        self.wake()

    def handle_swap_accepted(self, msg):
        self.inbox.append(f"{msg['sender']} accepted {msg['body']['thread']}; sign it if it is safe.")
        self.wake()

    def handle_swap_signed(self, msg):
        self.inbox.append(f"{msg['sender']} signed {msg['body']['thread']}.")
        self.wake()

    def play(self):
        if not os.environ.get("OPENAI_API_KEY"):
            self.api.observe("model_unavailable", self.name, {"reason": "OPENAI_API_KEY is not set"})
            return
        if self.model is None:
            name = self.config.get("model", "gpt-6-luna")
            tools = _site(self.api).seats.tools(self.token)
            self.model = link.Model(name, BRIEFING.format(trade=self.trade), tools)
            items = [{"role": "user", "content": "Week 9 is open. Start."}]
        else:
            items = [{"role": "user", "content": "\n".join(self.inbox) or "Check the board."}]
        self.inbox = []
        budget = self.config.get("calls_per_turn", 12)
        for _ in range(budget):
            try:
                calls, _text = self.model.step(items)
            except Exception as e:  # noqa: BLE001 - a model outage ends the turn, recorded
                self.api.observe("model_error", self.name, {"error": f"{type(e).__name__}: {str(e)[:160]}"})
                return
            if not calls:
                break
            items = []
            for c in calls:
                result = yield ("call", c["name"], c["arguments"])
                if c["name"] == "request_swap" and result.get("status") == "thread_open":
                    w = c["arguments"].get("with_trade")
                    changes = [c["arguments"].get("mine"), *(c["arguments"].get("theirs")
                               if isinstance(c["arguments"].get("theirs"), list) else [c["arguments"].get("theirs")])]
                    for p in (w if isinstance(w, list) else [w]):
                        yield ("tell", p, "swap_offer", {"thread": result["thread"], "changes": changes})
                if c["name"] in ("accept_swap", "commit_week") and result.get("ok"):
                    kind = {"accepted": "swap_accepted", "committed": "swap_committed"}.get(result.get("status"),
                                                                                           "swap_signed")
                    for p in sorted(set(result.get("waiting_for") or []) | self._parties(result.get("thread"))):
                        if p != self.trade:
                            yield ("tell", p, kind, {"thread": result.get("thread")})
                items.append({"type": "function_call_output", "call_id": c["call_id"],
                              "output": json.dumps(result)[:6000]})
        u = self.model.usage
        self.api.observe("model_turn", self.name, {"model": self.model.model, "calls": u["calls"],
                                                   "input_tokens": u["input_tokens"],
                                                   "output_tokens": u["output_tokens"],
                                                   "cost_usd_estimate": self.model.cost_usd()})

    def _parties(self, thread):
        offer = self.offers.get(thread)
        return {offer["from"]} if offer else set()


# -- a rogue ------------------------------------------------------------------------------------------


@role("simops-rogue")
class SimOpsRogue(SimAgent):
    """Speaks for another trade. It can forge a Town sender name; it cannot forge a SimOps seat token."""

    def on_start(self):
        self.victim = self.config.get("impersonate", "electrical")
        self.sent = 0
        if self.config.get("forge_card", True):
            self.api.register([f"construction.trade.{self.victim}"], {"trade": self.victim},
                              forge_key_of=self.victim)
        self.api.later(self.config.get("at", 4.0), self.strike)

    def _forged(self, desk, tool, args):
        """An envelope that claims to come from the victim, signed with the rogue's own key."""
        engine = self.api._engine
        self.sent += 1
        body = {"call_id": f"rogue-{self.sent}", "tool": tool, "args": args,
                "seat_token": self.config.get("guess_token", f"sk-simops-{self.victim}-guess")}
        envelope = engine.layers["communication"].envelope(self.victim, desk, f"call.{tool}", body)
        engine.layers["identity"].create(self.name)
        envelope["signature"] = engine.layers["auth"].sign_as(self.name, envelope["body"])
        engine.record_intent(self.name, "send_as", {"as": self.victim, "to": desk, "kind": envelope["kind"]})
        self.api.observe("forged_message", envelope["message_id"], {"as": self.victim, "tool": tool, "args": args})
        engine.layers["transport"].send(self.victim, desk, envelope)

    def strike(self):
        cards = self.api.lookup(REFEREE)
        if not cards:
            return
        desk = cards[0]["name"]
        move = self.config.get("move", {"task": "ELE-roughin", "zone": "H2", "days": [43, 45]})
        self._forged(desk, "propose", move)
        for i, thread in enumerate(self.config.get("guess_threads", ["T2", "T3"])):
            self.api.later(0.5 + 0.1 * i, lambda t=thread: self._forged(desk, "commit_week",
                                                                       {"week": 9, "thread": t}))


# -- the validator ------------------------------------------------------------------------------------


def _calls(trace):
    return trace.find("simops_call")


@validator("simops_week9")
def simops_week9(spec, trace: Trace) -> list[StageResult]:
    stages = []
    desks = [a.name for a in spec.agents if a.role == "simops-desk"]
    trades = [a.name for a in spec.agents if a.role in ("simops-trade", "simops-llm-trade")]
    rogues = [a.name for a in spec.agents if a.role == "simops-rogue"]
    calls = _calls(trace)
    reached = [e for e in calls if e.detail.get("reason") not in ("referee_unreachable",)]
    audit = trace.find("simops_verified")
    board = trace.find("week_board")
    closed = trace.find("week_closed")

    # referee_discovered: the desk's card verified, and every trade found that desk through the registry.
    card = [e for e in trace.find("card_registered", verified=True) if e.subject in desks
            and REFEREE in e.detail.get("capabilities", [])]
    found = [e for e in trace.find("referee_found") if e.subject in desks and e.observer in trades]
    who = {e.observer for e in found}
    stages.append(_check("referee_discovered", bool(card) and who == set(trades),
                         [e.event_id for e in card + found],
                         f"every trade must find the desk's verified card; found by {sorted(who)} of {trades}"))

    if not reached or trace.find("referee_unreachable"):
        note = "SimOps never answered, so no SimOps behaviour was observed"
        for name in ("briefs_private", "rules_enforced", "swap_signed_by_all", "applied_once",
                     "forged_seat_refused", "stale_calls_refused", "week_committed", "simops_verified"):
            stages.append(_missing(name, note))
        return stages

    # briefs_private: every brief SimOps handed out belonged to the trade whose message asked for it.
    briefs = [e for e in calls if e.detail.get("tool") == "my_brief" and e.detail.get("ok")]
    wrong = [e for e in briefs if e.detail.get("brief_of") != e.detail.get("from")]
    stages.append(_check("briefs_private", not wrong, [e.event_id for e in (wrong or briefs)],
                         "a brief went to a sender that is not its trade",
                         f"{len(briefs)} briefs, each to its own trade; bodies redacted"))

    # rules_enforced: every refused move names its reason, and SimOps's audit finds no violation on the board.
    refused = [e for e in calls if e.detail.get("tool") in WRITES and not e.detail.get("ok")]
    unnamed = [e for e in refused if not (e.detail.get("rules") or e.detail.get("reason"))]
    violations = audit[-1].detail.get("violations") if audit else None
    if not audit:
        stages.append(_missing("rules_enforced", "no SimOps audit was recorded"))
    else:
        stages.append(_check("rules_enforced", not unnamed and not violations,
                             [e.event_id for e in refused] + [audit[-1].event_id],
                             f"{len(unnamed)} refusals named no rule; {len(violations or [])} violations on the "
                             "final board", f"{len(refused)} refused moves, each with a reason; 0 violations"))

    # swap_signed_by_all: every commit carries a signature from each party to its thread.
    parties: dict[str, set] = {}
    for e in calls:
        d = e.detail
        if d.get("ok") and d.get("tool") == "request_swap" and d.get("thread"):
            w = d["args"].get("with_trade")
            parties[d["thread"]] = {d["from"], *(w if isinstance(w, list) else [w])}
        elif d.get("ok") and d.get("tool") == "propose" and d.get("thread"):
            parties[d["thread"]] = {d["from"]}
    commits = [e for e in calls if e.detail.get("status") in APPLIED]
    unsigned = [e for e in commits if not parties.get(e.detail.get("thread"), {None})
                <= set(e.detail.get("signatures") or [])]
    if not commits:
        stages.append(StageResult(name="swap_signed_by_all", status="not_tested",
                                  note="nothing was committed in this run"))
    else:
        stages.append(_check("swap_signed_by_all", not unsigned, [e.event_id for e in (unsigned or commits)],
                             "a commit is missing a party's signature",
                             f"{len(commits)} commits, each signed by every party"))

    # applied_once: each thread commits at most once, and a duplicated call never changes the board again.
    per: dict[str, int] = {}
    for e in commits:
        per[e.detail.get("thread")] = per.get(e.detail.get("thread"), 0) + 1
    twice = [t for t, n in per.items() if n > 1]
    dup_ids = {e.subject for e in trace.find("message_duplicated") if str(e.detail.get("kind", "")).startswith("call.")}
    repeats = [e for e in calls if e.subject in dup_ids]
    second = [e for i, e in enumerate(repeats) if e.subject in {x.subject for x in repeats[:i]}]
    harmful = [e for e in second if e.detail.get("tool") in WRITES and e.detail.get("ok")]
    if not commits and not second:
        stages.append(StageResult(name="applied_once", status="not_tested",
                                  note="nothing was committed and no call was delivered twice"))
    else:
        stages.append(_check("applied_once", not twice and not harmful,
                             [e.event_id for e in (second + commits)],
                             f"threads committed twice: {twice}; repeated calls that changed state: "
                             f"{[e.event_id for e in harmful]}",
                             f"{len(commits)} commits, {len(second)} repeated deliveries, none applied twice"))

    # forged_seat_refused: nothing the rogue sent changed SimOps's state.
    if not rogues:
        stages.append(StageResult(name="forged_seat_refused", status="not_tested", note="no rogue in this run"))
    else:
        forged = {e.subject: e for e in trace.find("forged_message") if e.observer in rogues}
        through = [e for e in calls if e.subject in forged]
        landed = [e for e in through if e.detail.get("tool") in WRITES and e.detail.get("ok")]
        bounced = trace.find("delivery_failed")
        bounced = [e for e in bounced if e.subject in forged]
        evidence = [e.event_id for e in (landed or through or bounced)] or [e.event_id for e in forged.values()]
        stages.append(_check("forged_seat_refused", bool(forged) and not landed, evidence,
                             f"{len(landed)} forged calls changed SimOps state",
                             f"{len(forged)} forged calls: {len(bounced)} stopped by Town auth, "
                             f"{len(through)} reached SimOps and were refused"))

    # stale_calls_refused: after the week closed, no write was accepted.
    if not closed:
        stages.append(_missing("stale_calls_refused", "the week was never closed"))
    else:
        at = trace.index(closed[0])
        late = [e for e in calls if trace.index(e) > at and e.detail.get("tool") in WRITES]
        if not late:
            stages.append(StageResult(name="stale_calls_refused", status="not_tested",
                                      note="no write arrived after the week closed"))
        else:
            taken = [e for e in late if e.detail.get("ok")]
            stages.append(_check("stale_calls_refused", not taken, [e.event_id for e in (taken or late)],
                                 f"{len(taken)} writes were accepted after the week closed",
                                 f"{len(late)} late writes, all refused"))

    # week_committed: the week ends with every task placed and nothing breaking a rule.
    if not board:
        stages.append(_missing("week_committed", "no final board was recorded"))
    else:
        b = board[-1].detail
        stages.append(_check("week_committed", not b.get("unscheduled") and not violations,
                             [board[-1].event_id], f"unscheduled at the end: {b.get('unscheduled')}",
                             f"every task placed, board v{b.get('board_version')}, {b.get('commits')} commits"))

    # simops_verified: SimOps's own audit (replay, signatures, misuse, invariants I1-I5) passed on this run.
    if not audit:
        stages.append(_missing("simops_verified", "no SimOps audit was recorded"))
    else:
        a = audit[-1].detail
        bad = sorted(k for k, ok in (a.get("invariants") or {}).items() if not ok)
        stages.append(_check("simops_verified", bool(a.get("ok")), [audit[-1].event_id],
                             f"SimOps verify failed: invariants {bad}, replay {a.get('replay_ok')}",
                             f"replayed {a.get('calls')} calls, I1-I5 hold, log {str(a.get('log_digest'))[:19]}…"))
    return stages
