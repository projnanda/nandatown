#!/usr/bin/env python3
"""A Track seller whose memory is a git.v1 store instead of a journal.

It joins Town's HTTP mailbox like any bring-your-own-agent participant,
claims quote requests, answers each one, and acknowledges. What it knows
about work it already did lives in a gitmem store (one markdown file per
fact, every change a git commit) under its STATE_DIR:

    STATE_DIR/memory/facts/seller/self/f-0001.md   ...

Run it through Town:

    GITMEM_SRC=/path/to/nandatown-memory-mvp/memory_mvp \\
    nandatown test-agent --role seller --profile quote-amnesia-restart \\
        --cmd "python examples/gitmem/gitmem_seller.py"

The crash. Under crash_amnesia (and crash_amnesia_fresh_ids) Town's own
seller answers the first claim, wipes its journal and exits before
acknowledging. This seller acts out the same crash point (answer, exit 3
before the ack) but keeps its memory, because its memory is what is under
test. The runner restarts it once and redelivers the request.

The hand edit. GITMEM_EDIT stands in for a person editing the memory
files with a text editor while the seller is down. It runs once, on the
restart, before the store is opened, and touches the files directly,
never through the store:

    none     nothing is edited
    total    the remembered total is changed from 3990 to 3999
    delete   the remembered quote is deleted

What the seller does about it is GITMEM_TRUST:

    verify   (default) ask the store for facts changed outside it, put
             each one back to its last committed version, and record the
             repair as a fact of its own
    trust    believe whatever the files say, as a plain markdown memory
             would

The ack note carries a `gitmem` object (outside edits found, whether they
were restored, whether a resend was refused), so what the memory saw
reaches Town's evidence bundle and not only this process's stderr.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from pathlib import Path

CRASH_EXIT_CODE = 3
HOLDER = "seller"
HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "memory_schema.json"
LOG: Path | None = None


def log(message: str) -> None:
    """The runner discards a participant's stderr, so keep a log in
    STATE_DIR where the bundle's state/ directory preserves it."""
    sys.stderr.write(f"gitmem_seller: {message}\n")
    if LOG is not None:
        with open(LOG, "a") as handle:
            handle.write(f"{time.time():.3f} {message}\n")


class _Stop:
    """Town stops a participant with SIGTERM, then SIGKILL 0.5 s later.
    A memory write is `git add` then `git commit`; stopping between the
    two leaves a staged batch with no commit. So a SIGTERM that arrives
    mid-write is held until the write finishes."""

    writing = 0
    pending = False

    @classmethod
    def handler(cls, signum, frame):
        if cls.writing:
            cls.pending = True
            return
        raise SystemExit(0)

    @classmethod
    def after_write(cls):
        if cls.pending and not cls.writing:
            log("stopping after finishing the memory write")
            raise SystemExit(0)


def _import_gitmem():
    src = os.environ.get("GITMEM_SRC")
    if src:
        sys.path.insert(0, src)
    try:
        from gitmem import Contract, RejectedWrite, Store  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "gitmem_seller: cannot import gitmem. Set GITMEM_SRC to the"
            " memory_mvp directory of a nandatown-memory-mvp checkout"
            " (see examples/gitmem/README.md).\n")
        sys.exit(2)
    return Contract, RejectedWrite, Store


Contract, RejectedWrite, Store = _import_gitmem()


def _detach_git() -> None:
    """Run the store's git commands outside this process's group.

    Town stops a participant with killpg(SIGTERM), which reaches every
    process in its group, including a `git add` or `git commit` the store
    is running at that moment. The git child dies, the write is left
    half-made, and the files then look like an outside edit. In their own
    session the git children finish; this process holds the SIGTERM until
    the write is done (see _Stop)."""
    import subprocess

    import gitmem.store as store_module

    class _Detached:
        def __getattr__(self, name):
            return getattr(subprocess, name)

        @staticmethod
        def run(*args, **kwargs):
            if os.name == "posix":
                kwargs.setdefault("start_new_session", True)
            return subprocess.run(*args, **kwargs)

    store_module.subprocess = _Detached()


_detach_git()

import httpx  # noqa: E402  (after the path setup above, Town's own dep)

from nandatown.client import StaleFenceError, TownClient  # noqa: E402


# -- the hand edit -----------------------------------------------------------

def hand_edit(memory_root: Path, mode: str) -> list[str]:
    """Edit the seller's memory files the way a person would: open the
    file, change the text, save. No store API, no commit."""
    touched: list[str] = []
    facts = memory_root / "facts" / HOLDER / "self"
    if mode == "none" or not facts.exists():
        return touched
    for path in sorted(facts.glob("f-*.md")):
        text = path.read_text()
        if '\nstatus: "active"' not in text:
            continue
        key = re.search(r'^key: "([^"]+)"', text, re.M)
        key = key.group(1) if key else ""
        if mode == "total" and key.endswith(".reply"):
            edited = text.replace("3990", "3999")
            if edited != text:
                path.write_text(edited)
                touched.append(str(path.relative_to(memory_root)))
        elif mode == "total" and key.endswith(".total_cents"):
            edited = re.sub(r"\n3990\n?$", "\n3999\n", text)
            if edited != text:
                path.write_text(edited)
                touched.append(str(path.relative_to(memory_root)))
        elif mode == "delete" and (key.endswith(".reply")
                                   or key.endswith(".total_cents")):
            path.unlink()
            touched.append(str(path.relative_to(memory_root)))
    return touched


# -- memory ------------------------------------------------------------------

class Memory:
    """The seller's side of the store: the self lane, through the contract."""

    def __init__(self, root: Path, agents: set[str]):
        self.store = Store(root)
        self.store.init()
        self.contract = Contract(str(SCHEMA))
        self.agents = agents

    def write(self, ops: list[dict], title: str, when: str,
              source: str) -> str | None:
        checked = []
        for op in ops:
            op = {"lane": "self", "holder": HOLDER, "asserter": HOLDER, **op}
            try:
                checked.append(self.contract.check(op, self.agents))
            except RejectedWrite as exc:
                log(f"write refused: {exc}")
        _Stop.writing += 1
        try:
            changes = self.store.apply(checked, source=source, when=when)
            commit = self.store.commit(changes, source=source, title=title,
                                       when=when)
        finally:
            _Stop.writing -= 1
        log(f"memory commit {commit}: {title}")
        _Stop.after_write()
        return commit

    def get(self, key: str):
        fact = self.store.current(HOLDER, key, "self")
        return None if fact is None else fact.value

    def outside_edits(self) -> list[dict]:
        """Facts changed outside the store since its last commit.

        Only the working-tree column of `git status` counts. The store
        stages every file it writes before committing, so a change that
        is staged and otherwise clean is the store's own interrupted
        write, not somebody else's edit."""
        return outside_edits(self.store)

    def restore(self, paths: list[str]) -> None:
        self.store._git("checkout", "HEAD", "--", *paths)


def outside_edits(store) -> list[dict]:
    out = store._git("status", "--porcelain", "--untracked-files=all",
                     "--", "facts")
    found = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        index, tree, path = line[0], line[1], line[3:].strip()
        if tree == " ":
            continue  # staged by the store itself
        change = {"M": "modified", "D": "deleted", "?": "added"}.get(
            tree, "changed")
        found.append({"path": path, "change": change})
    return found


def remembered_reply(memory: Memory, request_id: str) -> dict | None:
    raw = memory.get(f"quote.{request_id}.reply")
    if raw is None:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None


# -- the participant ---------------------------------------------------------

def response_id(request_id: str) -> str:
    return "r-" + request_id.removeprefix("q-")


def run() -> int:
    env = os.environ
    state_dir = Path(env["STATE_DIR"])
    fault = env.get("FAULT", "none")
    edit_mode = env.get("GITMEM_EDIT", "none")
    trust = env.get("GITMEM_TRUST", "verify")
    if edit_mode not in ("none", "total", "delete"):
        log(f"unknown GITMEM_EDIT {edit_mode!r}")
        return 2
    if trust not in ("verify", "trust"):
        log(f"unknown GITMEM_TRUST {trust!r}")
        return 2

    global LOG
    LOG = state_dir / "gitmem_seller.log"
    signal.signal(signal.SIGTERM, _Stop.handler)
    memory_root = state_dir / "memory"
    crash_marker = state_dir / "crashed-once"
    edit_marker = state_dir / "hand-edited"
    restarted = crash_marker.exists()

    # The hand edit happens while the seller is down, so before anything
    # of the seller's touches the store.
    edited: list[str] = []
    if restarted and not edit_marker.exists():
        edited = hand_edit(memory_root, edit_mode)
        edit_marker.write_text(json.dumps(edited))
        log(f"hand edit {edit_mode!r} touched {edited}")

    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    agents = {p["name"] for p in client.participants()} | {HOLDER, "buyer"}
    memory = Memory(memory_root, agents)

    report: dict = {"policy": trust, "outside_edits": [], "restored": False}
    if trust == "verify":
        found = memory.outside_edits()
        report["outside_edits"] = found
        if found:
            memory.restore([f["path"] for f in found])
            report["restored"] = True
            memory.write(
                [{"op": "ADD", "key": "memory.outside_edits_count",
                  "value": len(found)},
                 {"op": "ADD", "key": "memory.outside_edits",
                  "value": "; ".join(f"{f['change']} {f['path']}"
                                     for f in found)}],
                title="restored facts edited outside the store",
                when="1.50", source=f"{env['RUN_ID']}#restart")
            log(f"restored {len(found)} fact(s) edited outside the store:"
                f" {found}")

    deadline = time.time() + float(env.get("DEADLINE", "45"))
    lease = float(client.run_context.get("lease_seconds", 5.0))

    while time.time() < deadline:
        client.notify(wait=0.3)
        claim = client.claim()
        if claim is None:
            continue
        mid, fence = claim["message_id"], claim["fence"]
        attempt = int(claim.get("attempt", 1))
        when = f"{attempt}.00"
        source = f"{env['RUN_ID']}#{mid}@attempt{attempt}"
        if claim["kind"] != "quote_request":
            client.ack(mid, fence, "rejected", {"reason": "unknown kind"})
            continue

        if fault == "crash_after_claim" and not crash_marker.exists():
            crash_marker.write_text(fence)
            time.sleep(lease + 0.5)
            try:
                client.ack(mid, fence, "processed",
                           {"applied": True, "after_crash_stall": True})
            except StaleFenceError:
                return CRASH_EXIT_CODE
            return 9  # the stale fence was wrongly accepted

        note: dict = {"gitmem": dict(report)}
        reply = remembered_reply(memory, mid)
        if reply is not None:
            # Already answered: resend the remembered reply under the same
            # identity. If memory still matches what the town accepted,
            # the town returns the original acceptance.
            note["duplicate"] = True
            try:
                client.send(**reply)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
                note["gitmem"]["resend_refused"] = True
                log(f"town refused the remembered reply to {mid} (409)")
                memory.write([{"op": "ADD",
                               "key": f"quote.{mid}.resend_refused",
                               "value": True}],
                             title=f"town refused resend of {mid}",
                             when=when, source=source)
            if not memory.get(f"quote.{mid}.reported"):
                note["applied"] = True
                note["total_cents"] = reply["body"]["total_cents"]
        else:
            body = claim["body"]
            total = body["quantity"] * body["unit_price_cents"]
            reply = {"message_id": response_id(mid), "to": claim["from"],
                     "kind": "quote_response",
                     "body": {"request_id": mid, "sku": body["sku"],
                              "quantity": body["quantity"],
                              "total_cents": total}}
            # The application and its memory commit together, before the
            # reply leaves, so a crash after sending still finds it.
            memory.write(
                [{"op": "ADD", "key": f"quote.{mid}.reply", "value": reply},
                 {"op": "ADD", "key": f"quote.{mid}.total_cents",
                  "value": total},
                 {"op": "ADD", "key": f"quote.{mid}.reported",
                  "value": False}],
                title=f"applied {mid}", when=when, source=source)
            client.send(**reply)
            note["applied"] = True
            note["total_cents"] = total
            if (fault in ("crash_amnesia", "crash_amnesia_fresh_ids")
                    and not crash_marker.exists()):
                # Town's crash point: answered, not acknowledged. Unlike
                # Town's seller, the memory is kept.
                crash_marker.write_text(fence)
                return CRASH_EXIT_CODE

        try:
            client.ack(mid, fence, "processed", note)
        except StaleFenceError:
            continue
        if note.get("applied"):
            memory.write([{"op": "UPDATE", "key": f"quote.{mid}.reported",
                           "value": True}],
                         title=f"town recorded {mid}", when=f"{attempt}.50",
                         source=source)
    return 0


if __name__ == "__main__":
    sys.exit(run())
