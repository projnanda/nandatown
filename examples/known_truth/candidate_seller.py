#!/usr/bin/env python3
"""The seller for the known-truth qualification-gate example.

Standard-library Python, no nandatown import. Reads TOWN_URL, RUN_ID,
NAME, TOKEN, DEADLINE from the environment.

    candidate_seller.py --mode scripted            (default)
    candidate_seller.py --mode live --candidate ID [--yes]

Both modes: if the seller receives a `qualification_refused` message it
acknowledges it (the note records the reason, so it lands in Town's
evidence) and exits 0 at once, so a refused run ends early.

SCRIPTED MODE IS MECHANICS ONLY. No model runs. It copies the mailbox
behavior of examples/byoa_seller.py so the gate can be tested for free in
CI. It is not evidence about any agent.

LIVE MODE hands the seller role to an outside agent through Town's own MCP
adapter. It runs the project's candidate agent (a model tool loop that
CALLS A PAID MODEL API) against `nandatown mcp serve`, with Town's bundled
seller skill text as its instructions. Needs two environment variables:
KNOWNTRUTH_PROJECT_DIR (the project folder) and PROJECT_PYTHON (the Python
that can run it). It asks for a typed `yes` on a terminal unless --yes is
given. Under `nandatown run` the participant's output is hidden and stdin
is not a prompt, so live mode there requires --yes: choose that on purpose.
"""

import argparse
import os
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _town_http as town  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(HERE, "..", "..", "src", "nandatown", "skills",
                         "builtin")
SKILL_FILES = ("quote.read.md", "town-protocol.md")
REFUSAL_KIND = "qualification_refused"


def ack(claim, status, note):
    town.call("POST", "/inbox/ack",
              {"message_id": claim["message_id"], "fence": claim["fence"],
               "status": status, "note": note})


def ack_refusal(claim, runtime):
    body = claim["body"]
    note = {"qualification_refused": True, "reason": body.get("reason"),
            "candidate_id": body.get("candidate_id"), "runtime": runtime}
    if "failed_checks" in body:
        note["failed_checks"] = body["failed_checks"]
    ack(claim, "processed", note)


def run_scripted(deadline):
    processed = {}
    while time.time() < deadline:
        town.call("GET", "/inbox/notify?wait=0.4")
        claim = town.call("POST", "/inbox/claim")
        if claim is None:
            continue
        mid = claim["message_id"]
        if claim["kind"] == REFUSAL_KIND:
            ack_refusal(claim, "known-truth-scripted")
            return 0
        if claim["kind"] != "quote_request":
            ack(claim, "rejected", {"reason": "unknown kind"})
            continue
        if mid in processed:
            town.call("POST", "/messages", processed[mid])
            ack(claim, "processed", {"duplicate": True})
            continue
        body = claim["body"]
        total = body["quantity"] * body["unit_price_cents"]
        reply = {"message_id": "r-" + mid.removeprefix("q-"),
                 "to": claim["from"], "kind": "quote_response",
                 "body": {"request_id": mid, "total_cents": total}}
        town.call("POST", "/messages", reply)
        processed[mid] = reply
        ack(claim, "processed",
            {"applied": True, "total_cents": total,
             "runtime": "known-truth-scripted"})
    return 0


def confirm_live(candidate, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("live mode calls a paid model API and needs a typed 'yes' on a"
              " terminal; pass --yes to confirm on purpose.", file=sys.stderr)
        return False
    with open("/dev/tty", "w") as tty:
        tty.write(f"Run candidate {candidate!r} against a paid model API?"
                  " Type yes to continue: ")
    return sys.stdin.readline().strip().lower() == "yes"


def peek_for_refusal(deadline):
    """Claim the first message. A refusal is acknowledged (return True).
    Anything else is handed back with a 'retryable' acknowledgement so the
    outside agent claims it normally."""
    wait_until = min(deadline, time.time() + 10.0)
    while time.time() < wait_until:
        town.call("GET", "/inbox/notify?wait=0.4")
        claim = town.call("POST", "/inbox/claim")
        if claim is None:
            continue
        if claim["kind"] == REFUSAL_KIND:
            ack_refusal(claim, "known-truth-live-wrapper")
            return True
        ack(claim, "retryable", {"released_to": "outside agent"})
        return False
    return False


def nandatown_executable():
    beside = os.path.join(os.path.dirname(sys.executable), "nandatown")
    return beside if os.path.exists(beside) else (
        shutil.which("nandatown") or "nandatown")


# Added after the first live run. With Town's skill text alone, the model agent
# answered the one request correctly and then ended its turn, so nobody was left
# to claim Town's redelivery and `duplicate_recognized` stayed inconclusive.
SESSION_RULES = """## Session rules for this run

You are a long-running mailbox participant, not a one-shot assistant.

- Do not end your turn after the first request. After every acknowledgement,
  call `town_notify` and then `town_claim` again.
- The town may deliver the same request more than once. Keep a list, in this
  session, of every request id you have answered and the exact response you
  sent for it.
- If a claimed message has a request id you already answered, do not compute
  or send a different answer. Send the identical response again (same
  message_id, same body), then acknowledge it with status `processed` and a
  note that contains `"duplicate": true`.
- Stop only after four `town_claim` calls in a row return nothing, counted
  after your first `processed` acknowledgement.
"""


def run_child_in_own_group(command):
    """Run the project agent in its own process group and clean it up on SIGTERM.

    The town stops a participant by signalling its process group. The agent
    starts helpers of its own (the MCP server), so it gets a separate group:
    when the town stops this wrapper, the wrapper stops that group, waits for
    it, and only then exits. Nothing is left behind in the wrapper's group.
    """
    child = subprocess.Popen(command, start_new_session=True)

    def stop(_signum, _frame):
        for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(child.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                child.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return child.wait()


def run_live(candidate, deadline):
    project = os.environ["KNOWNTRUTH_PROJECT_DIR"]
    python = os.environ["PROJECT_PYTHON"]
    if peek_for_refusal(deadline):
        return 0
    mcp_cmd = shlex.join([
        nandatown_executable(), "mcp", "serve", "--url", town.TOWN,
        "--run", town.RUN, "--name", os.environ["NAME"],
        "--token", os.environ["TOKEN"]])
    with tempfile.TemporaryDirectory() as tmp:
        instructions = os.path.join(tmp, "seller_instructions.md")
        with open(instructions, "w", encoding="utf-8") as out:
            for name in SKILL_FILES:
                with open(os.path.join(SKILL_DIR, name),
                          encoding="utf-8") as src:
                    out.write(src.read() + "\n\n")
            out.write(SESSION_RULES)
        command = [python,
                   os.path.join(project, "src", "town", "town_candidate.py"),
                   "--candidate", candidate, "--mcp-cmd", mcp_cmd,
                   "--instructions-file", instructions, "--yes"]
        return run_child_in_own_group(command)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["scripted", "live"],
                        default="scripted")
    parser.add_argument("--candidate", default="unspecified")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    deadline = time.time() + float(os.environ.get("DEADLINE", "45"))
    if args.mode == "live":
        if not (os.environ.get("KNOWNTRUTH_PROJECT_DIR")
                and os.environ.get("PROJECT_PYTHON")):
            print("live mode needs KNOWNTRUTH_PROJECT_DIR and PROJECT_PYTHON"
                  " in the environment.", file=sys.stderr)
            return 2
        if not confirm_live(args.candidate, args.yes):
            return 3
        town.join(os.environ["NAME"], os.environ["TOKEN"])
        return run_live(args.candidate, deadline)
    town.join(os.environ["NAME"], os.environ["TOKEN"])
    return run_scripted(deadline)


if __name__ == "__main__":
    sys.exit(main())
