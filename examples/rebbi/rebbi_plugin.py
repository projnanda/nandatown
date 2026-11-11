"""Rebbi in the Lab: Town agents borrow token budget through Rebbi's MCP server.

Rebbi lends spare token budget between the AI agents of one fleet. It is
hosted in its own repository and runs in its own virtual environment; this
file holds only the Town side:

  rebbi_adapter  the only role that talks to Rebbi. It opens one live MCP
                 stdio session per fleet agent (Rebbi takes an agent's
                 identity from REBBI_AGENT_ID, never from a tool argument),
                 turns each delivered Town message into exactly one tool
                 call, and meters work steps through Rebbi's own ledger, as
                 Rebbi's gateway meters model calls.
  rebbi_lender   publishes the reserve it will never lend.
  rebbi_worker   takes a job its budget cannot cover, borrows the
                 difference, works, delivers and settles the loan.
  rebbi_client   pays for the job through Town escrow.

Lender, worker and client are integration scripts written for this test,
not independently developed agents. Rebbi is the subject. Validators judge
only the recorded trace.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from typing import Any

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import validator

HERE = os.path.dirname(os.path.abspath(__file__))
EXPECTED_TOOLS = {"get_budget", "report_headroom", "post_handoff", "settle"}


def _load_sibling(name: str):
    spec = importlib.util.spec_from_file_location(
        f"nandatown_rebbi_{name}", os.path.join(HERE, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mcp_stdio = _load_sibling("mcp_stdio")


def _digest(names: list[str]) -> str:
    h = hashlib.sha256()
    for name in names:
        with open(os.path.join(HERE, name), "rb") as f:
            h.update(name.encode() + b"\0" + f.read().replace(b"\r\n", b"\n"))
    return "sha256:" + h.hexdigest()


def _project_version(project: str) -> tuple[str, bool]:
    """HEAD of the Rebbi checkout, and whether its tracked or new files
    differ from it. If git cannot say, the commit is unknown and dirty."""
    def git(*args):
        return subprocess.run(["git", "-C", project, *args],
                              capture_output=True, text=True, check=False)
    head, status = git("rev-parse", "HEAD"), git("status", "--porcelain")
    if head.returncode or status.returncode:
        return "unknown", True
    return head.stdout.strip(), bool(status.stdout.strip())


def _rebbi_env(project: str, **extra: str) -> dict[str, str]:
    """Rebbi is imported from the checkout under test, never from wherever
    its virtual environment was installed from."""
    path = os.pathsep.join(p for p in (project, os.environ.get("PYTHONPATH"))
                           if p)
    return dict(os.environ, PYTHONPATH=path, PYTHONIOENCODING="utf-8",
                **extra)


def _rebbi_python(project: str) -> str:
    explicit = os.environ.get("REBBI_PYTHON")
    if explicit:
        return explicit
    if os.name == "nt":
        return os.path.join(project, ".venv", "Scripts", "python.exe")
    return os.path.join(project, ".venv", "bin", "python")


# -- the adapter --------------------------------------------------------


@role("rebbi_adapter")
class RebbiAdapter(SimAgent):
    """Town messages in, Rebbi MCP tool calls out. One call per delivery:
    the adapter never deduplicates, so Rebbi sees what Town delivered."""

    def on_start(self):
        self.sessions: dict[str, Any] = {}
        self.project = os.environ.get("REBBI_PROJECT", "")
        self.python = _rebbi_python(self.project) if self.project else ""
        if not self.project or not os.path.isdir(self.project):
            self.api.observe("rebbi_unavailable", "rebbi",
                             {"reason": "REBBI_PROJECT is not a directory"})
            return
        commit, dirty = _project_version(self.project)
        self.api.observe("rebbi_project", "rebbi", {
            "commit": commit, "dirty": dirty,
            "adapter_digest": _digest(["rebbi_plugin.py", "mcp_stdio.py",
                                       "rebbi_meter.py"]),
            "fleet_digest": _digest(["fleet.toml"]),
        })
        log_path = os.environ.get("REBBI_SERVER_LOG")
        self._log = open(log_path, "ab") if log_path else subprocess.DEVNULL
        for agent_id in self.config["fleet"]:
            env = _rebbi_env(self.project, REBBI_AGENT_ID=agent_id)
            try:
                session = mcp_stdio.McpSession(
                    [self.python, "-m", "rebbi.mcp_server"], env=env,
                    cwd=self.project, stderr=self._log)
                session.start()
            except (OSError, mcp_stdio.McpError) as exc:
                self.api.observe("rebbi_session_failed", agent_id,
                                 {"error": str(exc)[:200]})
                continue
            self.sessions[agent_id] = session
            self.api.observe("rebbi_session_opened", agent_id, {
                "server": session.server_info.get("name", ""),
                "protocol_version": session.protocol_version,
                "tools": sorted(session.tools),
                "request_id_supported": session.accepts("post_handoff",
                                                        "request_id"),
            })
        self.api.later(self.config.get("snapshot_at", 30.0), self._finish)

    # Each handler makes exactly one Rebbi call for the agent that sent the
    # message; Town's auth layer has already verified the sender.

    def _call(self, msg, tool: str, **arguments) -> dict[str, Any] | None:
        sender = msg["sender"]
        session = self.sessions.get(sender)
        try:
            if session is None:
                raise mcp_stdio.McpError(f"{sender} has no Rebbi session")
            return session.call(tool, **arguments)
        except mcp_stdio.McpError as exc:
            self.api.observe("rebbi_call_failed", sender,
                             {"tool": tool, "error": str(exc)[:300]})
            self.api.reply(msg, "rebbi_error",
                           {"tool": tool, "error": str(exc)[:300],
                            **{k: msg["body"][k] for k in ("job_id",
                                                           "request_id")
                               if k in msg["body"]}})
            return None

    def handle_headroom(self, msg):
        b = msg["body"]
        r = self._call(msg, "report_headroom",
                       reserve_tokens=b["reserve_tokens"],
                       will_lend=b.get("will_lend", True))
        if r is None:
            return
        self.api.observe("rebbi_headroom", msg["sender"], {
            "reserve_tokens": r["reserve_tokens"],
            "will_lend": r["will_lend"], "lendable": r["lendable"]})
        self.api.reply(msg, "headroom_ack", {"lendable": r["lendable"]})

    def handle_budget_query(self, msg):
        r = self._call(msg, "get_budget")
        if r is None:
            return
        self.api.observe("rebbi_budget", msg["sender"], {
            "balance": r["balance"],
            "borrowed": [loan["loan_id"] for loan in r["borrowed"]]})
        self.api.reply(msg, "budget_reply", {"job_id": msg["body"]["job_id"],
                                             "balance": r["balance"]})

    def handle_borrow_request(self, msg):
        b = msg["body"]
        session = self.sessions.get(msg["sender"])
        arguments = {"goal": b["goal"], "budget_tokens": b["tokens"],
                     "context_ref": b["job_id"]}
        sent_id = bool(session and session.accepts("post_handoff",
                                                   "request_id"))
        if sent_id:
            arguments["request_id"] = b["request_id"]
        r = self._call(msg, "post_handoff", **arguments)
        if r is None:
            return
        decision = {
            "request_id": b["request_id"], "decision": r["decision"],
            "loan_id": r.get("loan_id"), "tokens": r.get("tokens"),
            "requested": r.get("requested"), "lender": r.get("from"),
            "replayed": bool(r.get("replayed", False)),
            "blocker": r.get("blocker", ""),
        }
        self.api.observe("rebbi_decision", b["request_id"], {
            **decision, "requester": msg["sender"],
            "delivery": msg["message_id"], "request_id_sent": sent_id,
            "lender_reserve_intact": r.get("lender_reserve_intact"),
            "considered": [{"agent": c["agent"], "reason": c["reason"]}
                           for c in r.get("considered", [])],
        })
        self.api.reply(msg, "borrow_decision", decision)

    def handle_usage(self, msg):
        b = msg["body"]
        r = self._meter("usage", msg["sender"], str(b["tokens"]),
                        f"{b['job_id']} step {b['step']}")
        if r is None:
            self.api.reply(msg, "rebbi_error", {"tool": "meter",
                                                "job_id": b["job_id"],
                                                "error": "meter failed"})
            return
        self.api.observe("rebbi_usage", msg["sender"], {
            "job_id": b["job_id"], "step": b["step"], "tokens": b["tokens"],
            "recorded": r["recorded"], "balance": r["balance"]})
        self.api.reply(msg, "usage_ack", {"job_id": b["job_id"],
                                          "step": b["step"], **r})

    def handle_settle_request(self, msg):
        b = msg["body"]
        r = self._call(msg, "settle", loan_id=b["loan_id"],
                       outcome=b.get("outcome", "done"))
        if r is None:
            return
        self.api.observe("rebbi_settled", f"loan-{r['loan_id']}", {
            "loan_id": r["loan_id"], "borrower": r["borrower"],
            "lender": r["lender"], "tokens": r["tokens"],
            "status": r["status"], "returned_tokens": r["returned_tokens"],
            "outcome": r.get("outcome")})
        self.api.reply(msg, "settle_ack", {"loan_id": r["loan_id"],
                                           "returned_tokens":
                                               r["returned_tokens"]})

    def _meter(self, *args: str) -> dict[str, Any] | None:
        env = _rebbi_env(self.project)
        try:
            done = subprocess.run(
                [self.python, os.path.join(HERE, "rebbi_meter.py"), *args],
                cwd=self.project, env=env, capture_output=True, text=True,
                timeout=60, check=False)
            if done.returncode:
                raise OSError(done.stderr.strip()[-300:])
            return json.loads(done.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            self.api.observe("rebbi_meter_failed", "rebbi",
                             {"step": args[0], "error": str(exc)[:300]})
            return None

    def _finish(self):
        snap = self._meter("snapshot")
        if snap is not None:
            self.api.observe("rebbi_ledger_snapshot", "rebbi", snap)
        for session in self.sessions.values():
            session.close()
        self.api.observe("rebbi_sessions_closed", "rebbi",
                         {"count": len(self.sessions)})
        self.sessions = {}
        if self._log is not subprocess.DEVNULL:
            self._log.close()


# -- Town scripts -------------------------------------------------------


@role("rebbi_lender")
class RebbiLender(SimAgent):
    def on_start(self):
        self.api.send(self.config["broker"], "headroom", {
            "reserve_tokens": self.config["reserve_tokens"],
            "will_lend": self.config.get("will_lend", True)})

    def handle_headroom_ack(self, msg):
        self.api.observe("headroom_published", self.name,
                         {"lendable": msg["body"]["lendable"]})


@role("rebbi_worker")
class RebbiWorker(SimAgent):
    """Keeps a journal of what it has handled, as Town asks of every
    participant: a second copy of a decision it already acted on is
    recorded and ignored, never acted on twice."""

    def on_start(self):
        self.jobs: dict[str, dict[str, Any]] = {}
        self.decided: dict[str, dict[str, Any]] = {}

    def _job_for(self, request_id: str) -> dict[str, Any] | None:
        return next((j for j in self.jobs.values()
                     if j.get("request_id") == request_id), None)

    def handle_job_request(self, msg):
        b = msg["body"]
        if b["job_id"] in self.jobs:
            self.api.observe("duplicate_job_ignored", b["job_id"], {})
            return
        self.jobs[b["job_id"]] = {
            "job_id": b["job_id"], "client": msg["sender"],
            "est_tokens": b["est_tokens"], "steps": list(self.config["steps"]),
            "loan_id": None, "attempts": 0, "closed": False}
        self.api.send(self.config["broker"], "budget_query",
                      {"job_id": b["job_id"]})

    def handle_budget_reply(self, msg):
        job = self.jobs[msg["body"]["job_id"]]
        shortfall = job["est_tokens"] - msg["body"]["balance"]
        if shortfall <= 0:
            self._next_step(job)
            return
        job["request_id"] = f"{self.name}/{job['job_id']}/borrow-1"
        job["borrow"] = {"request_id": job["request_id"],
                         "job_id": job["job_id"], "tokens": shortfall,
                         "goal": f"finish {job['job_id']}"}
        self._ask(job)

    def _ask(self, job):
        job["attempts"] += 1
        self.api.observe("borrow_requested", job["request_id"],
                         {"tokens": job["borrow"]["tokens"],
                          "attempt": job["attempts"]})
        # A retry resends the same request identity with identical content.
        self.api.send(self.config["broker"], "borrow_request", job["borrow"])
        self.api.later(self.config.get("retry_after", 2.0),
                       lambda: self._maybe_retry(job))

    def _maybe_retry(self, job):
        if job["request_id"] in self.decided or job["closed"]:
            return
        if job["attempts"] > self.config.get("max_retries", 1):
            self._decline(job, "no answer from Rebbi")
            return
        self.api.observe("borrow_retried", job["request_id"],
                         {"attempt": job["attempts"] + 1})
        self._ask(job)

    def handle_borrow_decision(self, msg):
        b = msg["body"]
        first = self.decided.get(b["request_id"])
        if first is not None:
            self.api.observe("duplicate_decision_ignored", b["request_id"], {
                "loan_id": b.get("loan_id"),
                "first_loan_id": first.get("loan_id"),
                "same_loan": b.get("loan_id") == first.get("loan_id")})
            return
        self.decided[b["request_id"]] = b
        job = self._job_for(b["request_id"])
        if job is None:
            return
        if b["decision"] != "LEND":
            self._decline(job, b.get("blocker") or b["decision"])
            return
        job["loan_id"] = b["loan_id"]
        self._next_step(job)

    def _next_step(self, job):
        if not job["steps"]:
            self._deliver(job)
            return
        tokens = job["steps"].pop(0)
        self.api.send(self.config["broker"], "usage", {
            "job_id": job["job_id"], "tokens": tokens,
            "step": len(self.config["steps"]) - len(job["steps"])})

    def handle_usage_ack(self, msg):
        job = self.jobs[msg["body"]["job_id"]]
        if not msg["body"]["recorded"]:
            self._decline(job, msg["body"].get("reason", "step refused"))
            return
        self._next_step(job)

    def _deliver(self, job):
        job["closed"] = True
        self.api.send(job["client"], "job_result", {"job_id": job["job_id"]})
        if job["loan_id"] is not None:
            self.api.send(self.config["broker"], "settle_request",
                          {"loan_id": job["loan_id"], "outcome": "done"})

    def _decline(self, job, reason: str):
        if job["closed"]:
            return
        job["closed"] = True
        self.api.observe("job_declined", job["job_id"], {"reason": reason})
        self.api.send(job["client"], "job_declined",
                      {"job_id": job["job_id"], "reason": reason})
        if job["loan_id"] is not None:
            self.api.send(self.config["broker"], "settle_request",
                          {"loan_id": job["loan_id"], "outcome": "abandoned"})

    def handle_settle_ack(self, msg):
        self.api.observe("loan_closed_seen", f"loan-{msg['body']['loan_id']}",
                         {"returned_tokens": msg["body"]["returned_tokens"]})

    def handle_rebbi_error(self, msg):
        job = self.jobs.get(msg["body"].get("job_id", ""))
        if job is None and msg["body"].get("request_id"):
            job = self._job_for(msg["body"]["request_id"])
        if job is not None:
            self._decline(job, f"Rebbi error in {msg['body']['tool']}")


@role("rebbi_client")
class RebbiClient(SimAgent):
    def on_start(self):
        self.closed = False
        self.api.later(self.config.get("start_at", 1.0), self._post)

    def _post(self):
        c = self.config
        self.api.escrow_hold(c["price_cents"], c["job_id"])
        self.api.send(c["worker"], "job_request", {
            "job_id": c["job_id"], "est_tokens": c["est_tokens"],
            "price_cents": c["price_cents"]})

    def handle_job_result(self, msg):
        if self.closed or msg["body"]["job_id"] != self.config["job_id"]:
            self.api.observe("job_result_ignored", msg["message_id"], {})
            return
        self.closed = True
        self.api.escrow_release(self.config["job_id"], msg["sender"])
        self.api.observe("job_paid", self.config["job_id"],
                         {"worker": msg["sender"]})

    def handle_job_declined(self, msg):
        if self.closed or msg["body"]["job_id"] != self.config["job_id"]:
            return
        self.closed = True
        self.api.escrow_refund(self.config["job_id"])
        self.api.observe("job_refunded", self.config["job_id"],
                         {"reason": msg["body"]["reason"]})


# -- validators ---------------------------------------------------------
#
# Every check reads only events.jsonl. Missing evidence is reported as
# missing, never passed.


def _stage(name: str, ok: bool, events: list, fail: str,
           passed: str = "") -> StageResult:
    ids = [e.event_id for e in events][:8]
    if not events:
        return StageResult(name=name, status="not_enough_evidence", note=fail)
    return StageResult(name=name, status="passed" if ok else "failed",
                       evidence=ids, note=passed if ok else fail)


def _agents(spec, role_name: str) -> list:
    return [a for a in spec.agents if a.role == role_name]


def _snapshot(trace):
    snaps = trace.find("rebbi_ledger_snapshot")
    return (snaps[-1], snaps[-1].detail) if snaps else (None, None)


def check_connected(spec, trace) -> StageResult:
    adapter = _agents(spec, "rebbi_adapter")[0]
    fleet = adapter.config["fleet"]
    opened = {e.subject: e for e in trace.find("rebbi_session_opened")}
    failed = trace.find("rebbi_session_failed") + trace.find(
        "rebbi_unavailable")
    missing = [a for a in fleet if a not in opened]
    short = {a: sorted(EXPECTED_TOOLS - set(e.detail.get("tools", [])))
             for a, e in opened.items()
             if EXPECTED_TOOLS - set(e.detail.get("tools", []))}
    ok = not missing and not short and not failed
    note = (f"missing sessions {missing}" if missing else
            f"tools missing {short}" if short else
            "a session failed to start" if failed else "")
    return _stage("rebbi_connected", ok, list(opened.values()) + failed,
                  note or "no Rebbi MCP session was opened",
                  f"{len(opened)} live MCP sessions, one per fleet agent")


def check_pinned(spec, trace) -> StageResult:
    found = trace.find("rebbi_project")
    detail = found[0].detail if found else {}
    commit = detail.get("commit", "")
    ok = bool(re.fullmatch(r"[0-9a-f]{40}", str(commit))) and \
        detail.get("dirty") is False
    return _stage("rebbi_version_pinned", ok, found,
                  f"Rebbi version not pinned: commit {commit!r}, dirty"
                  f" {detail.get('dirty')!r}" if found else
                  "no Rebbi version recorded",
                  f"Rebbi at {commit}, clean tree")


def check_headroom(spec, trace) -> StageResult:
    lenders = [a.name for a in _agents(spec, "rebbi_lender")]
    seen = {e.subject: e for e in trace.find("rebbi_headroom")}
    ok = all(name in seen for name in lenders)
    return _stage("headroom_published", ok, list(seen.values()),
                  f"reserves acknowledged for {sorted(seen)} of {lenders}",
                  "every lender's reserve is published in Rebbi")


def _decisions(trace) -> dict[str, list]:
    out: dict[str, list] = {}
    for e in trace.find("rebbi_decision"):
        out.setdefault(e.subject, []).append(e)
    return out


def check_loan_granted(spec, trace) -> StageResult:
    decisions = _decisions(trace)
    firsts = [events[0] for events in decisions.values()]
    ok = bool(firsts) and all(
        e.detail.get("decision") == "LEND"
        and e.detail.get("lender_reserve_intact") is True
        and e.detail.get("tokens") == e.detail.get("requested")
        for e in firsts)
    return _stage("loan_granted", ok, firsts,
                  "the first decision was not a full LEND with the"
                  " lender's reserve intact" if firsts else
                  "no borrow decision recorded",
                  "Rebbi lent the full shortfall, lender reserve intact")


def check_one_loan_per_request(spec, trace) -> StageResult:
    """At-least-once delivery may hand Rebbi the same request twice; a
    request identity must still book at most one loan."""
    decisions = _decisions(trace)
    _, snap = _snapshot(trace)
    evidence = [e for events in decisions.values() for e in events]
    problems = []
    for request_id, events in decisions.items():
        loans = sorted({e.detail["loan_id"] for e in events
                        if e.detail.get("loan_id") is not None})
        if len(loans) > 1:
            problems.append(f"{request_id} booked loans {loans}")
    lent = {r for r, events in decisions.items()
            if any(e.detail.get("decision") == "LEND" for e in events)}
    if snap is not None and len(snap["loans"]) != len(lent):
        problems.append(f"Rebbi's ledger holds {len(snap['loans'])}"
                        f" loans for {len(lent)} lent request(s)")
    return _stage("one_loan_per_request", not problems, evidence,
                  "; ".join(problems) or "no borrow decision recorded",
                  f"{len(decisions)} request(s), one loan each")


def check_job_paid(spec, trace) -> StageResult:
    paid = trace.find("job_paid")
    released = trace.find("escrow_released")
    ok = bool(paid) and bool(released) and all(
        e.detail.get("to") == p.detail.get("worker")
        for e in released for p in paid)
    return _stage("job_paid", ok, paid + released,
                  "the client did not pay the worker from escrow",
                  "the client received the result and released escrow")


def check_loans_settled(spec, trace) -> StageResult:
    """Every loan is closed, and returned what the borrower did not spend."""
    snap_event, snap = _snapshot(trace)
    if snap is None:
        return _stage("loans_settled", False, [], "no ledger snapshot")
    problems = []
    for loan in snap["loans"]:
        if loan["status"] != "settled":
            problems.append(f"loan {loan['loan_id']} is {loan['status']}")
    settled = trace.find("rebbi_settled")
    for e in settled:
        borrower = e.detail["borrower"]
        budget = [b for b in trace.find("rebbi_budget", subject=borrower)]
        used = sum(u.detail["tokens"] for u in trace.find(
            "rebbi_usage", subject=borrower) if u.detail.get("recorded"))
        borrowed = sum(loan["tokens"] for loan in snap["loans"]
                       if loan["borrower"] == borrower)
        if budget:
            left = budget[0].detail["balance"] + borrowed - used
            expected = max(0, min(left, e.detail["tokens"]))
            if e.detail["returned_tokens"] != expected:
                problems.append(f"loan {e.detail['loan_id']} returned"
                                f" {e.detail['returned_tokens']}, expected"
                                f" {expected}")
    if not snap["loans"]:
        problems.append("no loan was booked")
    return _stage("loans_settled", not problems, [snap_event] + settled,
                  "; ".join(problems),
                  "every loan settled; unspent tokens went back")


def check_invariants(spec, trace) -> StageResult:
    snap_event, snap = _snapshot(trace)
    if snap is None:
        return _stage("rebbi_invariants", False, [], "no ledger snapshot")
    problems = snap.get("invariant_problems", [])
    return _stage("rebbi_invariants", not problems, [snap_event],
                  "; ".join(problems)[:300],
                  "Rebbi's own ledger check passes")


def check_fault(trace, kind: str, action: str) -> StageResult:
    event_kind = {"drop": "message_dropped",
                  "duplicate": "message_duplicated"}[action]
    found = trace.find(event_kind, kind=kind)
    return _stage("fault_injected", bool(found), found,
                  f"no {action} of {kind} was recorded",
                  f"Town injected a {action} of {kind}")


def check_answered_with_original(trace, stage: str) -> StageResult:
    """Every later answer to a repeated request names the first loan."""
    decisions = _decisions(trace)
    repeated = {r: events for r, events in decisions.items()
                if len(events) > 1}
    evidence = [e for events in repeated.values() for e in events]
    problems = []
    for request_id, events in repeated.items():
        first = events[0].detail.get("loan_id")
        for later in events[1:]:
            if later.detail.get("loan_id") != first:
                problems.append(f"{request_id}: a repeat booked loan"
                                f" {later.detail.get('loan_id')} instead of"
                                f" returning loan {first}")
            elif not later.detail.get("replayed"):
                problems.append(f"{request_id}: a repeat was not marked as"
                                f" a replay")
    return _stage(stage, not problems, evidence,
                  "; ".join(problems) or "Rebbi never saw the request twice",
                  "the repeat got the original decision back, marked"
                  " replayed")


def _common(spec, trace) -> list[StageResult]:
    return [check_connected(spec, trace), check_pinned(spec, trace),
            check_headroom(spec, trace)]


def _lending(spec, trace) -> list[StageResult]:
    return [check_loan_granted(spec, trace),
            check_one_loan_per_request(spec, trace),
            check_job_paid(spec, trace), check_loans_settled(spec, trace),
            check_invariants(spec, trace)]


@validator("rebbi_lend")
def rebbi_lend(spec, trace) -> list[StageResult]:
    return _common(spec, trace) + _lending(spec, trace)


@validator("rebbi_lost_reply")
def rebbi_lost_reply(spec, trace) -> list[StageResult]:
    retried = trace.find("borrow_retried")
    return (_common(spec, trace)
            + [check_fault(trace, "borrow_decision", "drop"),
               _stage("worker_retried", bool(retried), retried,
                      "the worker never retried",
                      "the worker resent the same request")]
            + _lending(spec, trace)
            + [check_answered_with_original(trace, "retry_gets_original")])


@validator("rebbi_duplicate")
def rebbi_duplicate(spec, trace) -> list[StageResult]:
    return (_common(spec, trace)
            + [check_fault(trace, "borrow_request", "duplicate")]
            + _lending(spec, trace)
            + [check_answered_with_original(trace,
                                            "duplicate_gets_original")])


@validator("rebbi_no_room")
def rebbi_no_room(spec, trace) -> list[StageResult]:
    decisions = [e for events in _decisions(trace).values() for e in events]
    lenders = {a.name for a in _agents(spec, "rebbi_lender")}
    explained = bool(decisions) and all(
        e.detail.get("decision") == "CANNOT_MOVE"
        and {c["agent"] for c in e.detail.get("considered", [])} >= lenders
        for e in decisions)
    refunded = trace.find("job_refunded") + trace.find("escrow_refunded")
    worker = _agents(spec, "rebbi_worker")[0].name
    spent = [u for u in trace.find("rebbi_usage", subject=worker)
             if u.detail.get("recorded")]
    snap_event, snap = _snapshot(trace)
    no_loans = snap is not None and not snap["loans"]
    return _common(spec, trace) + [
        _stage("cannot_move_explained", explained, decisions,
               "Rebbi did not refuse with a reason for every lender"
               if decisions else "no borrow decision recorded",
               "CANNOT_MOVE, with each lender's reason"),
        _stage("job_refunded", len(refunded) >= 2, refunded,
               "the client was not refunded",
               "the worker declined and the client got its escrow back"),
        _stage("nothing_spent", not spent and snap is not None,
               [snap_event] if snap_event else [],
               f"the worker spent {sum(u.detail['tokens'] for u in spent)}"
               " tokens without a loan" if spent else "no ledger snapshot",
               "the worker spent nothing it did not have"),
        _stage("no_loans", no_loans, [snap_event] if snap_event else [],
               "Rebbi booked a loan nobody could afford"
               if snap is not None else "no ledger snapshot",
               "no loan was booked"),
        check_invariants(spec, trace),
    ]
