#!/usr/bin/env python3
"""A Town seller whose unit of work is a Fervo contribution.

Fervo (repo management for agent contributors) is shared infrastructure
that many coding agents write into: claims, nodes, evaluation and merges,
with an idempotency key on every write. This participant joins a Town
Track run like examples/byoa_seller.py, and for each quote request it
drives the real `fervo` CLI against a live Fervo server and runner:

    context -> claim --checkout -> commit -> submit -> status -> merged

then answers the quote and acknowledges. Town delivers work at least
once, so the same request can arrive several times. Each Fervo write
uses an --key derived from the Town run and message, never from the
clock or a random value, so a redelivery replays the stored Fervo
response instead of creating a second claim, node or merge.

The adapter keeps one fact locally, in STATE_DIR: the parent node it
chose for each message. That makes a replayed claim byte-identical after
`main` has moved because of its own merge. Every other piece of progress
(lease, worktree, node, job, merge) is recovered by replaying keys
against Fervo.

Standard library only; no nandatown import. Environment:

    TOWN_URL RUN_ID NAME TOKEN STATE_DIR DEADLINE FAULT   from Town
    FERVO_CLONE   an agent clone made with `fervo clone` (required)
    FERVO_CLI     command that runs the Fervo CLI (default: fervo)
    FERVO_KEYS    derived (default) or fresh: the negative control, which
                  mints a new key on every delivery
    FERVO_STEP    how much the contribution raises score.txt (default 10)

FAULT=crash_after_claim is acted on here, the way Town's scripted seller
does: after the first Fervo claim the process stalls past its Town lease,
proves the stale fence is refused, and exits with code 3 so the runner
restarts it. Other faults (duplicate_delivery, lost_ack, drop_wakeup)
are injected by the coordinator and need nothing from the participant.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

CRASH_EXIT_CODE = 3
SCOPE = "score.txt"
# Fixed so a retried commit is byte-identical: same tree, parent, author,
# committer, dates and message give the same commit id, and Fervo's
# submit key then sees the same request.
COMMIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "town-seller",
    "GIT_AUTHOR_EMAIL": "town-seller@example.invalid",
    "GIT_COMMITTER_NAME": "town-seller",
    "GIT_COMMITTER_EMAIL": "town-seller@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}
TERMINAL = {"merged", "rejected", "abandoned", "reverted"}
MAX_REBASES = 3


class FervoError(Exception):
    pass


class StaleFence(Exception):
    pass


class IdentityReuse(Exception):
    """Town refused a response: its id was already accepted with other
    content. For this adapter that means a second Fervo pass produced a
    different contribution than the first, which only fresh keys do."""


# -- Town mailbox ---------------------------------------------------------

class Town:
    def __init__(self, url: str, run_id: str):
        self.url, self.run_id, self.session = url, run_id, None

    def call(self, method, path, body=None, retry=True):
        url = f"{self.url}/runs/{self.run_id}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.session:
            req.add_header("X-Town-Session", self.session)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 204:
                    return None
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 503 and retry:
                # lost_ack: the town drops the first acknowledgement before
                # recording it, so the same acknowledgement is safe to resend.
                time.sleep(0.2)
                return self.call(method, path, body, retry=False)
            if exc.code == 409:
                detail = json.loads(exc.read() or b"{}").get("detail", {})
                error = detail.get("error") if isinstance(detail, dict) else None
                if error == "stale_fence":
                    raise StaleFence(body.get("fence") if body else "")
                if error == "identity_reuse":
                    raise IdentityReuse(body.get("message_id") if body else "")
            raise

    def join(self, name, token):
        joined = self.call("POST", "/join", {"name": name, "token": token})
        self.session = joined["session"]
        return joined["run"]

    def ack(self, message_id, fence, status, note):
        self.call("POST", "/inbox/ack", {"message_id": message_id,
                                         "fence": fence, "status": status,
                                         "note": note})


# -- Fervo ----------------------------------------------------------------

class Fervo:
    """The Fervo CLI for keyed writes; plain HTTP for reads.

    Writes go through the CLI because claim --checkout and submit do the
    Git work (worktree, push to the lease branch). Reads use the same
    server URL and agent token the CLI stored in the clone, over HTTP,
    because a Town lease can be as short as 1.5 s and each CLI start costs
    a few hundred milliseconds.
    """

    def __init__(self, clone: str, cli: list[str], log):
        self.clone = os.path.abspath(clone)
        self.cli = cli
        self.log = log
        git_url = self._git(self.clone, "remote", "get-url", "origin")
        header = self._git(self.clone, "config", "--get",
                           f"http.{git_url}.extraHeader")
        self.auth = header.removeprefix("Authorization: ")
        self.url = self._git(self.clone, "config", "--get", "fervo.url")
        # The clone's own agent token must be the one used. A human token
        # exported in the operator's shell would make this seller act as
        # a human, so it never reaches the CLI.
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("FERVO_TOKEN", "FERVO_GRANT")}

    @staticmethod
    def _git(cwd, *args, env=None):
        out = subprocess.run(["git", *args], cwd=cwd, env=env,
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise FervoError(f"git {' '.join(args)}: {out.stderr.strip()}")
        return out.stdout.strip()

    def run(self, cwd, *args):
        started = time.time()
        out = subprocess.run([*self.cli, *args], cwd=cwd, env=self.env,
                             capture_output=True, text=True)
        self.log("fervo", args[0], f"{time.time() - started:.2f}s",
                 "ok" if out.returncode == 0 else out.stderr.strip())
        if out.returncode != 0:
            raise FervoError(f"fervo {args[0]}: {out.stderr.strip()}")
        return json.loads(out.stdout)

    def get(self, path, params=None):
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        req = urllib.request.Request(f"{self.url}{path}{query}")
        req.add_header("Authorization", self.auth)
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read())

    def context(self):
        return self.get("/context")

    def status(self, ident, wait=0):
        return self.get(f"/status/{urllib.parse.quote(ident)}",
                        {"wait": wait} if wait else None)


# -- the contribution -------------------------------------------------------

class Plan:
    """The parents this seller chose, per message and round, on disk.

    A claim's request includes its parent. After this seller's own merge
    `main` has moved, so re-reading it on a redelivery would turn the
    replay into a different request. Recording the choice before the
    claim keeps the replay identical.
    """

    def __init__(self, path):
        self.path = path
        try:
            with open(path) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {}

    def parent(self, message_id, round_no, choose):
        rounds = self.data.setdefault(message_id, {})
        if str(round_no) not in rounds:
            rounds[str(round_no)] = choose()
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        return rounds[str(round_no)]


def commit_contribution(fervo, worktree, parent_commit, step, message_id):
    """Raise score.txt by step in the worktree, exactly once.

    A replayed claim reuses the lease's worktree. When it already holds
    the commit, nothing is recommitted. When it was recreated, the
    deterministic commit has the same id as before.
    """
    head = fervo._git(worktree, "rev-parse", "HEAD")
    if head != parent_commit:
        if fervo._git(worktree, "rev-parse", "HEAD^") != parent_commit:
            raise FervoError(f"worktree {worktree} holds unexpected history")
        return head
    path = os.path.join(worktree, SCOPE)
    with open(path) as f:
        score = int(f.read().strip())
    with open(path, "w") as f:
        f.write(f"{score + step}\n")
    env = {**os.environ, **COMMIT_IDENTITY}
    fervo._git(worktree, "-c", "commit.gpgsign=false", "commit", "--quiet",
               "--no-verify", "-am",
               f"Raise score by {step} for Town request {message_id}",
               env=env)
    return fervo._git(worktree, "rev-parse", "HEAD")


def contribute(fervo, plan, keys, message_id, step, deadline, on_claimed):
    """Run one Fervo contribution to a terminal state; return its record.

    Rounds handle `next_action: rebase`: a node whose parent is no longer
    `main` gets a new claim with --supersedes on the current main.
    """
    record = {"rounds": []}
    supersedes = None
    for round_no in range(MAX_REBASES + 1):
        parent = plan.parent(message_id, round_no,
                             lambda: fervo.context()["main"]["node"])
        args = ["claim", "--parent", parent, "--scope", SCOPE,
                "--intent", f"Town request {message_id}: raise score by {step}",
                "--key", keys(f"claim-{round_no}"), "--checkout"]
        if supersedes:
            args += ["--supersedes", supersedes]
        claim = fervo.run(fervo.clone, *args)
        on_claimed(claim)
        commit = commit_contribution(fervo, claim["worktree"],
                                     claim["parent_commit"], step, message_id)
        submitted = fervo.run(claim["worktree"], "submit",
                              "--lease", claim["lease"],
                              "--key", keys(f"submit-{round_no}"))
        node = submitted["node"]
        record["rounds"].append({"lease": claim["lease"], "parent": parent,
                                 "commit": commit, "node": node})
        status = fervo.status(node)
        for retry_no in range(1, 3):
            while status["next_action"] == "wait" and time.time() < deadline:
                status = fervo.status(node, wait=5)
            if status["next_action"] != "retry":
                break
            # A failed evaluation is the runner's fault, not the change's.
            status = fervo.run(fervo.clone, "retry", node,
                               "--key", keys(f"retry-{round_no}-{retry_no}"))
        if status["next_action"] in ("wait", "retry"):
            raise FervoError(f"node {node} still {status['state']}"
                             f" ({status['next_action']}) at the deadline")
        if status["next_action"] == "rebase":
            supersedes = node
            continue
        record.update(node=node, state=status["state"],
                      outcome=status["outcome"], reason=status["reason"],
                      next_action=status["next_action"])
        if status["state"] == "merged":
            record["merged_commit"] = commit
        return record
    raise FervoError(f"no terminal state for {message_id} after"
                     f" {MAX_REBASES} rebases")


def fervo_key(run_id, message_id, step_name, nonce=None):
    """The idempotency key of one Fervo write for one Town request.

    Derived keys name the Town run and message, so every delivery of the
    request replays the same Fervo writes. Town's buyer reuses its message
    id in every run, hence the run id. A nonce, minted per delivery, is
    the fresh-keys negative control.
    """
    base = f"town-{run_id}-{message_id}-{step_name}"
    return f"{base}-{nonce}" if nonce else base


def response_body(request, message_id, record):
    """The quote response. Its content must be identical on every
    redelivery, or Town refuses the resend as identity reuse, so it
    carries only fields that a replay returns unchanged."""
    total = request["quantity"] * request["unit_price_cents"]
    return {"request_id": message_id, "sku": request["sku"],
            "quantity": request["quantity"], "total_cents": total,
            "fervo": {"node": record["node"], "state": record["state"],
                      "merged_commit": record.get("merged_commit")}}


def main():
    env = os.environ
    state_dir = env["STATE_DIR"]
    os.makedirs(state_dir, exist_ok=True)
    log_path = env.get("FERVO_LOG") or os.path.join(state_dir,
                                                    "fervo_contributor.log")

    def log(*parts):
        with open(log_path, "a") as f:
            f.write(f"{time.time():.3f} pid={os.getpid()} "
                    + " ".join(str(p) for p in parts) + "\n")

    deadline = time.time() + float(env.get("DEADLINE", "45"))
    fault = env.get("FAULT", "none")
    fresh = env.get("FERVO_KEYS", "derived") == "fresh"
    step = int(env.get("FERVO_STEP", "10"))
    fervo = Fervo(env["FERVO_CLONE"], shlex.split(env.get("FERVO_CLI",
                                                          "fervo")), log)
    plan = Plan(os.path.join(state_dir, "fervo_plan.json"))
    town = Town(env["TOWN_URL"], env["RUN_ID"])
    run = town.join(env["NAME"], env["TOKEN"])
    lease_seconds = float(run.get("lease_seconds", 5.0))
    crash_marker = os.path.join(state_dir, "crashed-once")
    errors: dict[str, int] = {}
    log("joined", env["RUN_ID"], f"fault={fault}", f"keys={'fresh' if fresh else 'derived'}")

    while time.time() < deadline:
        town.call("GET", "/inbox/notify?wait=0.4")
        claim = town.call("POST", "/inbox/claim")
        if claim is None:
            continue
        mid, fence = claim["message_id"], claim["fence"]
        duplicate = bool(claim.get("duplicate"))
        log("town claim", mid, f"attempt={claim['attempt']}",
            f"duplicate={duplicate}")
        if claim["kind"] != "quote_request":
            town.ack(mid, fence, "rejected", {"reason": "unknown kind"})
            continue

        nonce = uuid.uuid4().hex[:8] if fresh else None

        def keys(step_name, mid=mid, nonce=nonce):
            return fervo_key(env["RUN_ID"], mid, step_name, nonce)

        def on_claimed(fervo_claim, claim=claim):
            if fault != "crash_after_claim" or os.path.exists(crash_marker):
                return
            with open(crash_marker, "w") as f:
                f.write(json.dumps({"town_fence": claim["fence"],
                                    "fervo_lease": fervo_claim["lease"]}))
            log("crash_after_claim: stalling past the Town lease")
            time.sleep(lease_seconds + 0.5)
            try:
                town.ack(claim["message_id"], claim["fence"], "processed",
                         {"applied": True, "after_crash_stall": True})
            except StaleFence:
                log("crash_after_claim: stale fence refused; exiting")
                sys.exit(CRASH_EXIT_CODE)
            log("crash_after_claim: stale fence was accepted")
            sys.exit(9)

        try:
            record = contribute(fervo, plan, keys, mid, step, deadline,
                                on_claimed)
        except FervoError as exc:
            # Hand the work back for another delivery a couple of times,
            # since replaying the keys is safe; then give up on it.
            errors[mid] = errors.get(mid, 0) + 1
            status = "retryable" if errors[mid] < 3 else "failed"
            log("fervo error", mid, status, exc)
            try:
                town.ack(mid, fence, status,
                         {"reason": str(exc)[:500], "runtime": "fervo"})
            except StaleFence:
                pass
            continue
        log("fervo done", mid, json.dumps(record, sort_keys=True))

        reply = {"message_id": "r-" + mid.removeprefix("q-"),
                 "to": claim["from"], "kind": "quote_response",
                 "body": response_body(claim["body"], mid, record)}
        fervo_note = {"node": record["node"], "state": record["state"],
                      "outcome": record["outcome"],
                      "merged_commit": record.get("merged_commit"),
                      "rounds": record["rounds"],
                      "keys": "fresh" if fresh else "derived"}
        try:
            town.call("POST", "/messages", reply)
        except IdentityReuse:
            # The answer already accepted names another Fervo node: this
            # delivery contributed a second time. Say so instead of dying.
            log("town refused the response as identity reuse", mid,
                record["node"])
            try:
                town.ack(mid, fence, "failed",
                         {"reason": "identity_reuse: this delivery produced"
                                    " a different Fervo contribution than"
                                    " the response already accepted",
                          "fervo": fervo_note})
            except StaleFence:
                pass
            continue
        # Only a delivery the town re-offers after recording a processed
        # acknowledgement is flagged duplicate. Any other delivery means
        # no acknowledgement of this message was recorded yet, so this
        # one carries the application, whichever attempt it is.
        note = ({"duplicate": True, "fervo": fervo_note} if duplicate else
                {"applied": True, "total_cents": reply["body"]["total_cents"],
                 "runtime": "fervo-contributor", "fervo": fervo_note})
        try:
            town.ack(mid, fence, "processed", note)
            log("town ack", mid, "recorded")
        except StaleFence:
            # The Fervo work outlasted the Town lease. The town has put the
            # message back; its next delivery replays the same keys.
            log("town ack", mid, "stale fence; awaiting redelivery")
    return 0


if __name__ == "__main__":
    sys.exit(main())
