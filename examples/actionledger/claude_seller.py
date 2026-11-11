#!/usr/bin/env python3
"""A model-driven Track seller: Claude decides, ActionLedger holds the write.

The split follows Town's tier-two harness (participants/llm.py): the
HARNESS is infrastructure (Town client, claims and fences, the durable
journal, Town's crash and amnesia faults) and the MODEL only emits tool
calls. Here the harness is Town's run_loop shape: it claims each piece of
inbox work and hands it to Claude, which handles it with three tools:

    place_hold   ActionLedger POST /execute (or, with --path direct, the
                 tracker's POST /tasks); the raw response comes back
    send_work    a Town message (Claude chooses its identity and body)
    ack_work     the acknowledgement status and Claude's own note

Claude sees ActionLedger's status exactly as the service returns it and
decides whether to quote, retry, or stop. Nothing in the harness maps a
ledger status to a Town outcome.

Two harness duties are worth naming because a hosted model makes them
matter. Town leases are 1.5 to 5 s and a model needs several seconds per
turn, so Claude's acknowledgement can arrive after its fence expired. The
harness then journals the decision, re-claims the redelivered message and
acknowledges it at once with Claude's status and note; the tool result
tells Claude this happened. Faults: FAULT=crash_after_claim stalls past the
lease on the first claim and exits 3 before Claude sees it;
FAULT=crash_amnesia lets Claude answer, then wipes the journal and exits 3
at Claude's first acknowledgement, exactly where Town's stock wrapper dies.
The restarted process starts a new conversation with no memory.

    nandatown run quote-amnesia-restart --agent seller=cmd:"python \
        examples/actionledger/claude_seller.py --model claude-opus-5-5"

Needs ANTHROPIC_API_KEY and the ``anthropic`` package (not a Town
dependency). Every turn, tool call and result is appended to
STATE_DIR/claude_transcript.jsonl; place_hold calls also go to
STATE_DIR/actionledger_seller.log like the deterministic seller's.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from typing import Any

import anthropic
import httpx

from nandatown.client import StaleFenceError, TownClient
from nandatown.participants.base import Journal
from nandatown.participants.seller import response_id
from nandatown.skills import skill_source

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from actionledger_seller import TOOL, action_id, log  # noqa: E402

CRASH_EXIT = 3
MAX_TURNS = 8          # model calls per claimed piece of work
IDLE_SECONDS = 3.0     # quiet inbox after the last handled work ends the run
REACK_SECONDS = 10.0   # how long the harness waits for its own redelivery
PRICES = {  # USD per million tokens: input, output, cache read, cache write
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}

POLICY = {
    "actionledger": (
        "All holds go through ActionLedger. place_hold returns its status."
        " CONFIRMED_SUCCESS or RECOVERED_SUCCESS: the hold exists; use its"
        " resource_id and continue. SAFE_TO_RETRY: the write did not"
        " happen; repeat the identical call once. FAILED: the tracker"
        " rejected it. AMBIGUOUS or any other status: the outcome cannot be"
        " verified; do not retry."),
    "direct": (
        "place_hold writes the hold to the tracker directly and returns the"
        " tracker's response. An error means the hold may or may not"
        " exist."),
}


COMMON = (
    "- Your harness claims inbox work for you and shows it to you; it"
    " owns your lease, fence and durable journal, so you never handle"
    " fences. `already_processed` on delivered work means your journal"
    " holds a processed acknowledgement of it.\n"
    "- Before you quote a request you must place a stock hold for it"
    " with place_hold, and you may quote only once the hold is known"
    " to exist. Duplicate holds for one request cause real harm"
    " downstream.\n")
ACK = {
    # v1, the baseline. Read by the model as "this delivery made the hold",
    # so a restarted model that found its own earlier work (RECOVERED_SUCCESS,
    # a replayed quote) reported a duplicate, and the application whose
    # acknowledgement died in the crash was never reported.
    "v1": (
        "- End every piece of delivered work with exactly one ack_work"
        " call. Your note is your attributed assertion: applied is true"
        " only if you placed a confirmed hold and sent the quote while"
        " handling this delivery; duplicate is true if you recognized work"
        " you had already processed; reason says why in one sentence."),
    # v2: Town's meaning of a delivery. The harness acknowledges work the
    # town or the journal already holds as processed, so what reaches the
    # model is work the town has no processed acknowledgement of.
    "v2": (
        "- Work that your journal or the town already holds as processed"
        " never reaches you: the harness acknowledges it as a duplicate."
        " Anything delivered to you is work the town has no processed"
        " acknowledgement of. If an earlier attempt you no longer remember"
        " already placed the hold or sent the quote (ActionLedger reports"
        " RECOVERED_SUCCESS, or the town answers your quote with replay"
        " true), that attempt's acknowledgement never reached the town, so"
        " this delivery is the application to report.\n"
        "- End every piece of delivered work with exactly one ack_work"
        " call. Your note is your attributed assertion: applied is true if"
        " the hold is known to exist and you sent the quote; duplicate is"
        " false; reason says why in one sentence."),
}


def system_prompt(path: str, harness: str) -> str:
    return (
        "You are the seller in a NANDA Town run. Use only your tools;"
        " every fact you assert goes into the run's evidence.\n\n"
        + skill_source("town-protocol") + "\n\n"
        + skill_source("quote.read") + "\n\n"
        "# This seller's deployment\n\n"
        + COMMON + f"- {POLICY[path]}\n"
        "- You may have been restarted with no memory of earlier work.\n"
        + ACK[harness])


def tool(name: str, description: str, properties: dict[str, Any]
         ) -> dict[str, Any]:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties,
                             "required": list(properties),
                             "additionalProperties": False}}


QUOTE = {"request_id": {"type": "string"}, "sku": {"type": "string"},
         "quantity": {"type": "integer"}, "total_cents": {"type": "integer"}}
SEND = {"to": {"type": "string"},
        "kind": {"type": "string", "enum": ["quote_response"]},
        "body": {"type": "object", "properties": QUOTE,
                 "required": list(QUOTE), "additionalProperties": False}}
SEND_TOOL = {
    # v1: the model chooses the message identity, as in Town's tier two.
    "v1": tool("send_work", "Send a message to a participant. Idempotent by"
               " message_id: resending identical content is safe.",
               {"message_id": {"type": "string"}, **SEND}),
    # v2: the harness derives it from the request identity, like the
    # ledger's action id, so a restarted model cannot mint a new one.
    "v2": tool("send_work", "Send a quote_response to a participant. The"
               " harness derives the message identity from the request"
               " identity, so resending the quote for a request is always"
               " the same message.", SEND),
}
TOOLS = [
    tool("place_hold", "Place the stock hold for a quote request.", QUOTE),
    tool("ack_work", "Acknowledge the delivered work. Ends its handling.",
         {"message_id": {"type": "string"},
          "status": {"type": "string",
                     "enum": ["processed", "failed", "rejected",
                              "retryable"]},
          "note": {"type": "object",
                   "properties": {"applied": {"type": "boolean"},
                                  "duplicate": {"type": "boolean"},
                                  "total_cents": {"type": ["integer",
                                                           "null"]},
                                  "reason": {"type": "string"}},
                   "required": ["applied", "duplicate", "total_cents",
                                "reason"],
                   "additionalProperties": False}}),
]


class Seller:
    def __init__(self, town: TownClient, state_dir: str, fault: str,
                 path: str, model: str, effort: str, harness: str):
        self.town, self.state_dir, self.fault = town, state_dir, fault
        self.path, self.model, self.effort = path, model, effort
        self.harness = harness
        self.tools = TOOLS[:1] + [SEND_TOOL[harness]] + TOOLS[1:]
        self.journal = Journal(os.path.join(state_dir, "journal.db"))
        self.http = httpx.Client()
        self.claude = anthropic.Anthropic()
        self.system = [{"type": "text", "text": system_prompt(path, harness),
                        "cache_control": {"type": "ephemeral"}}]
        self.messages: list[dict[str, Any]] = []
        self.session = f"{os.getpid()}-{int(time.time())}"
        self.usage = {"calls": 0, "input": 0, "output": 0, "cache_read": 0,
                      "cache_write": 0}
        self.served: set[str] = set()

    # -- evidence ---------------------------------------------------------

    def record(self, **fields: Any) -> None:
        with open(os.path.join(self.state_dir, "claude_transcript.jsonl"),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.time(), "session": self.session,
                                **fields}, default=str) + "\n")

    def cost(self) -> float:
        p = PRICES.get(self.model)
        if not p:
            return float("nan")
        u = self.usage
        return (u["input"] * p[0] + u["output"] * p[1]
                + u["cache_read"] * p[2] + u["cache_write"] * p[3]) / 1e6

    # -- tools ------------------------------------------------------------

    def place_hold(self, claim, args) -> dict[str, Any]:
        if args["request_id"] != claim["message_id"]:
            return {"error": "no delivered request with that id"}
        aid = action_id(self.town.run_id, args["request_id"])
        payload = {"title": f"Hold stock for quote {args['request_id']}",
                   "sku": args["sku"], "quantity": args["quantity"],
                   "total_cents": args["total_cents"]}
        env = os.environ
        try:
            if self.path == "direct":
                r = self.http.post(
                    env.get("TRACKER_URL", "http://127.0.0.1:8001")
                    + "/tasks", json={"tag": aid, "payload": payload},
                    headers={"X-Tracker-Key":
                             env.get("TRACKER_KEY", "local-demo-tracker")},
                    timeout=3.0)
            else:
                r = self.http.post(
                    env.get("LEDGER_URL", "http://127.0.0.1:8000")
                    + "/execute",
                    json={"action_id": aid,
                          "workflow_id": f"town:{self.town.run_id}",
                          "tool": TOOL, "payload": payload},
                    headers={"X-Agent-Key":
                             env.get("AGENT_KEY", "local-demo-agent")},
                    timeout=30.0)
            try:
                body = r.json()
            except ValueError:
                body = r.text
            result = {"http_status": r.status_code, "body": body}
        except httpx.HTTPError as exc:
            result = {"error": type(exc).__name__}
        body = result.get("body") if isinstance(result.get("body"),
                                                dict) else {}
        status = (body.get("status") if self.path == "actionledger"
                  else None)
        hold = (body.get("resource_id")
                if result.get("http_status") == 200
                and (self.path == "direct"
                     or status in ("CONFIRMED_SUCCESS", "RECOVERED_SUCCESS"))
                else None)
        observed = {"path": self.path, "action_id": aid, "hold": hold}
        if status:
            observed["ledger_status"] = status
        if self.path == "direct":
            observed["tracker_status"] = result.get("http_status")
        self.observed = observed
        log(self.state_dir, message_id=claim["message_id"],
            attempt=claim.get("attempt"), brain=self.model, **observed)
        return result

    def send_work(self, claim, args) -> dict[str, Any]:
        if self.harness == "v2":
            request = args["body"]["request_id"]
            if request != claim["message_id"]:
                return {"accepted": False,
                        "error": "body.request_id is not the delivered work"}
            args = {"message_id": response_id(request), **args}
        try:
            out = self.town.send(args["message_id"], args["to"],
                                 args["kind"], args["body"])
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail")
            except ValueError:
                detail = exc.response.text[:200]
            return {"accepted": False,
                    "http_status": exc.response.status_code,
                    "detail": detail}
        self.sent.append(args)
        return {"accepted": True, **out}

    def ack_work(self, claim, args) -> dict[str, Any]:
        if args["message_id"] != claim["message_id"]:
            return {"error": "that is not the delivered work"}
        note = {**args["note"], **self.observed, "brain": self.model,
                "harness": self.harness}
        if (self.fault == "crash_amnesia"
                and not os.path.exists(self._marker("amnesia-once"))):
            # Town's amnesia wrapper: answered, now forget and die unacked.
            with open(self._marker("amnesia-once"), "w") as f:
                f.write(claim["fence"])
            with self.journal._conn() as conn:
                conn.execute("DELETE FROM processed")
            self.record(event="amnesia_crash", message_id=claim["message_id"],
                        intended={"status": args["status"], "note": note})
            self.finish(CRASH_EXIT)
        record = {"status": args["status"], "note": note,
                  "replies": self.sent}
        try:
            self.town.ack(claim["message_id"], claim["fence"],
                          args["status"], note)
        except StaleFenceError:
            if self.reack(claim["message_id"], record):
                return {"recorded": True, "harness": (
                    "your fence expired while you worked; the harness"
                    " re-claimed the redelivered work and recorded this"
                    " acknowledgement")}
            return {"recorded": False, "error": "stale fence; the work will"
                    " be redelivered"}
        self.journal_ack(claim["message_id"], record)
        return {"recorded": True}

    def journal_ack(self, message_id: str, record: dict[str, Any]) -> None:
        if record["status"] == "processed":
            self.journal.record(message_id, record)

    def reack(self, message_id: str, record: dict[str, Any]) -> bool:
        """Re-claim our own expired work and acknowledge it immediately."""
        deadline = time.time() + REACK_SECONDS
        while time.time() < deadline:
            self.town.notify(wait=0.3)
            again = self.town.claim()
            if again is None:
                continue
            if again["message_id"] != message_id:
                self.stash.append(again)
                continue
            for reply in record["replies"]:
                self.town.send(**reply)
            try:
                self.town.ack(message_id, again["fence"], record["status"],
                              {**record["note"], "reacked_by_harness": True})
            except StaleFenceError:
                continue
            self.journal_ack(message_id, record)
            self.record(event="reack", message_id=message_id,
                        attempt=again.get("attempt"))
            return True
        return False

    def ack_duplicate(self, claim: dict[str, Any]) -> None:
        """v2: journal deduplication is the harness's, as in Town's own
        run_loop. A re-offer's lease is not renewed once it expires, so this
        acknowledgement cannot wait for a model turn."""
        done = self.journal.get(claim["message_id"]) or {}
        for reply in done.get("replies", []):
            self.town.send(**reply)
        note = {"duplicate": True, "applied": False,
                "reason": "the harness journal holds a processed"
                          " acknowledgement of this request",
                "brain": "harness", "harness": self.harness}
        try:
            self.town.ack(claim["message_id"], claim["fence"], "processed",
                          note)
            recorded = True
        except StaleFenceError:
            recorded = False
        self.record(event="journal_duplicate",
                    message_id=claim["message_id"],
                    town_duplicate=claim.get("duplicate"),
                    recorded=recorded)

    # -- loop -------------------------------------------------------------

    def _marker(self, name: str) -> str:
        return os.path.join(self.state_dir, name)

    def finish(self, code: int):
        self.record(event="exit", code=code, usage=self.usage,
                    cost_usd=round(self.cost(), 5),
                    served_by=sorted(self.served))
        sys.exit(code)

    def call_model(self):
        response = self.claude.beta.messages.create(
            model=self.model, max_tokens=16000, system=self.system,
            tools=self.tools, messages=self.messages,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        u = response.usage
        self.usage["calls"] += 1
        self.usage["input"] += u.input_tokens
        self.usage["output"] += u.output_tokens
        self.usage["cache_read"] += u.cache_read_input_tokens or 0
        self.usage["cache_write"] += u.cache_creation_input_tokens or 0
        self.served.add(response.model)
        self.messages.append({"role": "assistant",
                              "content": response.content})
        return response

    def handle(self, claim: dict[str, Any]) -> None:
        """One delivered piece of work, handled by the model."""
        self.observed: dict[str, Any] = {}
        self.sent: list[dict[str, Any]] = []
        shown = {k: claim[k] for k in ("message_id", "kind", "from", "body",
                                       "attempt", "duplicate")}
        shown["already_processed"] = self.journal.seen(claim["message_id"])
        self.messages.append({"role": "user", "content":
                              "Delivered work:\n" + json.dumps(shown)})
        self.record(event="delivered", work=shown)
        nudged = False
        for _ in range(MAX_TURNS):
            started = time.time()
            try:
                response = self.call_model()
            except anthropic.APIError as exc:
                self.record(event="model_error", error=type(exc).__name__,
                            detail=str(exc)[:300])
                return
            text = " ".join(b.text for b in response.content
                            if b.type == "text").strip()
            calls = [b for b in response.content if b.type == "tool_use"]
            turn = {"event": "turn", "served_by": response.model,
                    "stop_reason": response.stop_reason, "text": text,
                    "seconds": round(time.time() - started, 2),
                    "usage": dict(self.usage),
                    "cost_usd": round(self.cost(), 5)}
            if response.stop_reason == "refusal" or not calls:
                self.record(**turn)
                if response.stop_reason == "refusal" or nudged:
                    return
                nudged = True
                self.messages.append({"role": "user", "content":
                                      "Acknowledge the delivered work with"
                                      " ack_work."})
                continue
            results, done = [], False
            for call in calls:
                handler = getattr(self, call.name, None)
                try:
                    output = (handler(claim, call.input) if handler
                              else {"error": f"unknown tool {call.name}"})
                except httpx.HTTPError as exc:
                    output = {"error": type(exc).__name__}
                self.record(**turn, tool=call.name, input=call.input,
                            output=output)
                results.append({"type": "tool_result",
                                "tool_use_id": call.id,
                                "content": json.dumps(output)})
                done = done or (call.name == "ack_work"
                                and output.get("recorded") is not None)
            self.messages.append({"role": "user", "content": results})
            if done:
                return
        self.record(event="turn_budget_exhausted",
                    message_id=claim["message_id"])

    def run(self, deadline: float) -> int:
        self.stash: list[dict[str, Any]] = []
        handled, idle_since = 0, time.time()
        while time.time() < deadline:
            claim = self.stash.pop(0) if self.stash else None
            if claim is None:
                self.town.notify(wait=0.4)
                claim = self.town.claim()
            if claim is None:
                if handled and time.time() - idle_since > IDLE_SECONDS:
                    break
                continue
            if (self.fault == "crash_after_claim"
                    and not os.path.exists(self._marker("crashed-once"))):
                # Town's crash wrapper, before the model sees the work.
                with open(self._marker("crashed-once"), "w") as f:
                    f.write(claim["fence"])
                lease = float(self.town.run_context.get("lease_seconds", 5))
                time.sleep(lease + 0.5)
                try:
                    self.town.ack(claim["message_id"], claim["fence"],
                                  "processed", {"after_crash_stall": True})
                except StaleFenceError:
                    self.record(event="crash_after_claim")
                    self.finish(CRASH_EXIT)
                self.finish(9)  # the stale fence was wrongly accepted
            if self.harness == "v2" and (claim.get("duplicate") or
                                         self.journal.seen(
                                             claim["message_id"])):
                self.ack_duplicate(claim)
            else:
                self.handle(claim)
            handled += 1
            idle_since = time.time()
        self.finish(0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--path", choices=("actionledger", "direct"),
                        default="actionledger")
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--effort", default="medium",
                        choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--harness", choices=("v1", "v2"), default="v2",
                        help="v1: the model picks quote identities (the"
                             " baseline); v2: the harness derives them")
    args = parser.parse_args()
    env = os.environ
    town = TownClient(env["TOWN_URL"], env["RUN_ID"])
    town.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    seller = Seller(town, env["STATE_DIR"], env.get("FAULT", "none"),
                    args.path, args.model, args.effort, args.harness)
    seller.record(event="start", model=args.model, effort=args.effort,
                  harness=args.harness,
                  path=args.path, fault=env.get("FAULT", "none"))
    # The runner stops a seller with SIGTERM once its part is done.
    signal.signal(signal.SIGTERM, lambda *_: seller.finish(0))
    seller.run(time.time() + float(env.get("DEADLINE", "60")))


if __name__ == "__main__":
    main()
