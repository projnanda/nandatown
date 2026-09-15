"""Full-run orchestration: one command, one run, one evidence bundle.

The runner starts a coordinator subprocess, spawns the buyer and seller
as separate subprocesses with their own state directories, restarts the
seller once if it crashes, finishes the run, evaluates the event log, and
writes the portable bundle. Bundled harnesses receive a narrow environment;
an operator-supplied ``cmd:`` harness is trusted code and retains the
operator's ambient environment. This is process lifecycle containment, not a
filesystem or network sandbox. Runner observations (crash, restart, exit) are
posted as attributed events; the runner never synthesizes participant
assertions.
"""

from __future__ import annotations

import math
import os
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any

import httpx

from . import __version__
from .bundle import write_bundle
from .evaluator import EVALUATOR_VERSION, RESPONSE_KIND, evaluate
from .records import RunRecord, TestProfile, TownEvent, fingerprint
from .profiles import PROFILES

SELLER_CRASH_EXIT = 3

# Every participant the runner starts, Town's stock agent or a --cmd
# agent under test, gets a DEADLINE carved out of the run's wait timeout:
# the seller's is the timeout minus SELLER_DEADLINE_MARGIN and the
# buyer's the timeout minus BUYER_DEADLINE_MARGIN, so the seller is still
# serving while the buyer waits and the runner outlives both. An agent
# that joins from outside (--wait) is given no DEADLINE.
SELLER_DEADLINE_MARGIN = 5.0
BUYER_DEADLINE_MARGIN = 15.0
# The least time a buyer, the participant with the shorter DEADLINE,
# needs to request, receive and check a quote: one default lease. The
# slowest stock profile, quote-crash-restart, takes about 3.5 s end to
# end. Any shorter wait timeout would starve a buyer the runner starts,
# and the run would be reported INCOMPLETE for Town's arithmetic rather
# than for the agent under test, so it is refused for either role.
MIN_BUYER_BUDGET = 5.0
MIN_WAIT_TIMEOUT = BUYER_DEADLINE_MARGIN + MIN_BUYER_BUDGET  # 20 s


def check_wait_timeout(wait_timeout: float,
                       name: str = "wait_timeout") -> None:
    """Refuse a wait timeout that cannot make a valid Track run.

    Raises ValueError naming MIN_WAIT_TIMEOUT. NaN and infinity are
    refused too: neither bounds the run.
    """
    if not math.isfinite(wait_timeout):
        raise ValueError(f"{name} {wait_timeout} is not a usable number of"
                         f" seconds; give at least {MIN_WAIT_TIMEOUT:g} s")
    if wait_timeout < MIN_WAIT_TIMEOUT:
        raise ValueError(
            f"{name} {wait_timeout:g} s is too short: a buyer Town starts"
            f" gets a DEADLINE of the timeout minus"
            f" {BUYER_DEADLINE_MARGIN:g} s, and a buyer needs at least"
            f" {MIN_BUYER_BUDGET:g} s to request and check a quote, so"
            f" give at least {MIN_WAIT_TIMEOUT:g} s")


_BUILTIN_ENV_KEYS = (
    "PATH", "PYTHONPATH", "PYTHONHOME",
    "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TMP", "TEMP",
    "SYSTEMROOT", "WINDIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)


class RunnerError(Exception):
    pass


class RunnerUsageError(RunnerError):
    """Invalid caller input that a command-line caller can report as usage."""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(http: httpx.Client, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if http.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise RunnerError("coordinator did not become healthy")


def _spawn_participant(command: list[str], url: str, run_id: str, name: str,
                       token: str, state_dir: str, fault: str,
                       deadline: str,
                       extra_env: dict[str, str] | None = None,
                       inherit_env: bool = False,
                       ) -> subprocess.Popen:
    os.makedirs(state_dir, exist_ok=True)
    env = (dict(os.environ) if inherit_env else
           {key: os.environ[key] for key in _BUILTIN_ENV_KEYS
            if key in os.environ})
    env.update({"TOWN_URL": url, "RUN_ID": run_id, "NAME": name,
                "TOKEN": token, "STATE_DIR": state_dir, "FAULT": fault,
                "DEADLINE": deadline})
    env.update(extra_env or {})
    return subprocess.Popen(command, env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=(os.name == "posix"))


@contextmanager
def _stop_signals_held():
    """Hold SIGINT and SIGTERM until the block ends.

    run_town starts each Town process and records it for cleanup inside
    this block, so a stop signal cannot unwind run_town between the two and
    leave that process running. A signal that arrives meanwhile is raised
    again, for the handler that was in place, when the block ends. An
    ignored signal stays ignored; outside the main thread, which alone can
    set handlers, the block runs unguarded.
    """
    held: list[int] = []
    saved: dict[int, Any] = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous = signal.getsignal(signum)
                if previous is signal.SIG_IGN or previous is None:
                    continue
                saved[signum] = previous
                signal.signal(signum, lambda s, _frame: held.append(s))
        yield
    finally:
        # Both signals are blocked while their handlers are put back, so one
        # that arrives meanwhile waits and then reaches its restored handler,
        # once, when the caller's mask returns. Unblocked, a SIGINT between
        # the two restores would unwind and leave the recording handler in
        # place for SIGTERM. The mask is per thread, so this covers a caller
        # without other threads, such as the CLI. No process starts while
        # the signals are blocked, so none inherits the blocked mask.
        # Blocking is what makes the restores atomic, but restoring is what
        # makes holding safe at all. So a failure to block still restores,
        # unblocked, and each handler is restored independently: one that
        # raises, or a signal arriving between two of them while they are
        # unblocked, must not leave the rest recording into a list this
        # block has finished with, which would swallow them for good. The
        # first failure is re-raised once every handler is back.
        mask = None
        try:
            try:
                if saved and hasattr(signal, "pthread_sigmask"):
                    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
                    signal.pthread_sigmask(signal.SIG_BLOCK, list(saved))
            finally:
                failure: BaseException | None = None
                for signum, previous in saved.items():
                    try:
                        signal.signal(signum, previous)
                    except BaseException as exc:  # noqa: BLE001
                        failure = failure or exc
                if failure is not None:
                    raise failure
        finally:
            if mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        for signum in dict.fromkeys(held):
            # If this handler raises, a second held signal is not raised
            # again; the caller is already unwinding, and its cleanup runs.
            # A stop signal that arrives during the blocked restores above
            # is delivered by that mask restore, before this loop, and a
            # handler that unwinds there drops the held ones the same way:
            # either path leaves the caller unwinding on an equivalent
            # signal, and run_town's own cleanup still runs.
            # A signal wakeup fd, such as an asyncio loop's, sees a held
            # signal twice, on arrival and here; the TUI runs run_town in
            # worker threads, where nothing is held.
            signal.raise_signal(signum)


def _stop_process(process: subprocess.Popen, grace: float = 0.5) -> int | None:
    """Settle a child and, on POSIX, every descendant in its process group.

    Other platforms use a direct-child fallback; Python has no portable
    descendant-process primitive.
    """
    if getattr(process, "_town_cleanup_complete", False):
        return process.returncode
    # Reap an already-exited group leader first. Darwin reports EPERM when
    # killpg targets a group containing only an unreaped (defunct) leader.
    process.poll()
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            # Do not address this numeric PGID again: after ESRCH it could
            # belong to an unrelated group before a defensive finally pass.
            process._town_cleanup_complete = True
            return process.wait(timeout=grace)
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()
    try:
        result = process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        result = process.poll()
    # SIGKILL has been issued to any remaining POSIX descendants. A zombie
    # descendant may await its own parent's reap, but needs no second signal.
    if result is not None:
        process._town_cleanup_complete = True
    return result


def _participant_extra_env(kind: str, explicit: dict[str, str],
                           model: str) -> dict[str, str]:
    """Select model configuration for one harness without broad inheritance."""
    env: dict[str, str] = {}
    if kind in ("llm", "cmd"):
        effective_model = explicit.get("TOWN_MODEL", model)
        env["TOWN_MODEL"] = effective_model
        # A mock harness has no reason to receive paid credentials. Trusted
        # commands inherit ambient variables in _spawn_participant regardless.
        if kind == "cmd" or not effective_model.startswith("mock:"):
            for key in ("TOWN_MODEL_URL", "TOWN_MODEL_KEY"):
                if key in os.environ:
                    env[key] = os.environ[key]
        if kind == "llm" and not effective_model.startswith("mock:"):
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                        "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
                if key in os.environ:
                    env[key] = os.environ[key]
    env.update(explicit)
    return env


def parse_harness(spec: str) -> dict[str, Any]:
    """A harness connector spec, the way an agent plugs into a run.

    scripted        the stock reference agent for the role
    llm             the model tool loop with the run's default model
    llm:MODEL       the model tool loop with this model
    cmd:COMMAND     your own agent process, any runtime, any language
    external        spawn nothing; hand out join credentials instead
    """
    import shlex

    if spec == "scripted":
        return {"kind": "scripted"}
    if spec == "llm":
        return {"kind": "llm", "model": None}
    if spec.startswith("llm:"):
        return {"kind": "llm", "model": spec[4:]}
    if spec.startswith("cmd:"):
        command = shlex.split(spec[4:])
        if not command:
            raise RunnerError("cmd: harness needs a command")
        return {"kind": "cmd", "command": command}
    if spec == "external":
        return {"kind": "external"}
    if spec.startswith("a2a:"):
        url = spec[4:]
        if not url:
            raise RunnerError("a2a: harness needs a URL")
        return {"kind": "a2a", "url": url}
    raise RunnerError(
        f"unknown harness {spec!r}; use scripted, llm, llm:MODEL,"
        " cmd:COMMAND, a2a:URL, or external")


def _participant_command(profile: TestProfile, role: str,
                         external: dict[str, list[str] | None] | None,
                         harnesses: dict[str, str] | None = None
                         ) -> tuple[list[str] | None, dict[str, str], str]:
    """Return the command, explicit environment, and harness kind."""
    if harnesses and role in harnesses:
        harness = parse_harness(harnesses[role])
    elif external and role in external:
        command = external[role]
        harness = ({"kind": "external"} if command is None
                   else {"kind": "cmd", "command": list(command)})
    else:
        runtime = profile.runtimes.get(role, "scripted")
        harness = {"kind": runtime if runtime == "llm" else "scripted",
                   "model": None}
    kind = harness["kind"]
    if kind == "external":
        return None, {}, kind
    if kind == "cmd":
        return harness["command"], {}, kind
    if kind == "llm":
        env = {"ROLE": role}
        if harness.get("model"):
            env["TOWN_MODEL"] = harness["model"]
        return ([sys.executable, "-m", "nandatown.participants.llm"],
                env, kind)
    if kind == "a2a":
        return ([sys.executable, "-m",
                 "nandatown.participants.a2a_bridge"],
                {"A2A_URL": harness["url"]}, kind)
    return ([sys.executable, "-m", f"nandatown.participants.{role}"],
            {}, kind)


def _participant_provenance(role: str, kind: str
                            ) -> tuple[str, dict[str, Any]]:
    """Describe who supplied a harness without inventing its release.

    Town can name the bundled module bytes it launched. An operator command,
    manually connected participant, or remote A2A endpoint does not supply an
    immutable participant release through the current connector contract, so
    the record says that directly. The A2A bridge is still recorded separately
    as the adapter Town did launch.
    """
    if kind in ("scripted", "llm"):
        module = role if kind == "scripted" else "llm"
        release = f"nandatown.participants.{module} {__version__}"
        return release, {
            "kind": kind,
            "identity_basis": "bundled NANDA Town harness",
            "release_basis": release,
        }

    external = {
        "cmd": (
            "external command; immutable release not recorded",
            "operator-supplied command (command not recorded)",
        ),
        "external": (
            "external participant; immutable release not recorded",
            "operator-connected participant (software identity not supplied)",
        ),
        "a2a": (
            "external A2A participant; immutable release not recorded",
            "operator-supplied A2A endpoint (URL not recorded)",
        ),
    }
    release, identity_basis = external[kind]
    provenance: dict[str, Any] = {
        "kind": kind,
        "identity_basis": identity_basis,
        "release_basis": None,
        "release_basis_note": "immutable external release not supplied",
    }
    if kind == "a2a":
        provenance["adapter_release"] = (
            f"nandatown.participants.a2a_bridge {__version__}")
    return release, provenance


def _redacted_harness_spec(kind: str, supplied: str | None = None) -> str:
    """Return connector metadata that is safe to put in run.json."""
    if kind == "cmd":
        return "cmd:<operator-supplied-command>"
    if kind == "a2a":
        return "a2a:<operator-supplied-endpoint>"
    if kind in ("scripted", "llm", "external"):
        return supplied or kind
    raise RunnerError(f"unknown resolved harness kind {kind!r}")


def _recorded_harnesses(
        profile: TestProfile,
        participant_kinds: dict[str, str],
        harnesses: dict[str, str] | None,
        external: dict[str, list[str] | None] | None) -> dict[str, str]:
    """Record effective overrides while omitting commands and endpoints."""
    recorded: dict[str, str] = {}
    for role in profile.roles:
        if harnesses and role in harnesses:
            supplied = harnesses[role]
        elif external and role in external:
            supplied = None
        else:
            continue
        recorded[role] = _redacted_harness_spec(
            participant_kinds[role], supplied)
    return recorded


def _rerun_metadata(
        profile: TestProfile,
        recorded_harnesses: dict[str, str],
        participant_kinds: dict[str, str],
        external: dict[str, list[str] | None] | None,
        harnesses: dict[str, str] | None,
        identity_dir: str | None,
        uses_llm: bool,
        model: str) -> tuple[str, dict[str, str]]:
    """Build a non-secret rerun recipe and list inputs Town omitted."""
    import shlex

    required = {
        role: {
            "cmd": "original command (not recorded)",
            "a2a": "original A2A endpoint (URL not recorded)",
            "external": (
                "external participant must reconnect with fresh credentials"),
        }[kind]
        for role, kind in participant_kinds.items()
        if kind in ("cmd", "a2a", "external")
    }

    # This is the public CLI path used by `test-agent --cmd/--wait`. Preserve
    # it when possible instead of converting the rerun into a stock Track run.
    if external and not harnesses and len(external) == 1 and not identity_dir:
        role, command = next(iter(external.items()))
        parts = ["nandatown", "test-agent", "--profile", profile.name,
                 "--role", role]
        if command is None:
            parts.append("--wait")
        else:
            parts.extend(["--cmd", "<operator-supplied-command>"])
        rerun = " ".join(shlex.quote(part) for part in parts)
        if uses_llm and model != "mock:v1":
            rerun = f"TOWN_MODEL={shlex.quote(model)} {rerun}"
        return rerun, required

    parts = ["nandatown", "run", profile.name]
    for role, spec in recorded_harnesses.items():
        parts.extend(["--agent", f"{role}={spec}"])
    if identity_dir:
        parts.append("--identity")
    if uses_llm and model != "mock:v1":
        parts.extend(["--model", model])
    return " ".join(shlex.quote(part) for part in parts), required


def _validate_role_overrides(
        profile: TestProfile,
        harnesses: dict[str, str] | None,
        external: dict[str, list[str] | None] | None) -> None:
    """Reject override keys that cannot select a profile participant."""
    for overrides in (harnesses, external):
        for role in (overrides or {}):
            if role not in profile.roles:
                supported = ", ".join(sorted(profile.roles))
                raise RunnerUsageError(
                    f"unknown role {role!r}; supported roles: {supported}")


def _grant_refused(events: list[dict[str, Any]]) -> str | None:
    """The first role that tried to join a pinned identity with a bare
    token, or None. Such a harness can never join, so waiting on is
    pointless."""
    for e in events:
        if e["kind"] == "grant_required":
            return e["subject"]
    return None


def _quiescent(profile: TestProfile, events: list[dict[str, Any]]) -> bool:
    """Has the seller side finished everything this profile expects?"""
    seller_acks = [e for e in events
                   if e["kind"] == "ack_recorded"
                   and e["observer"] == "seller"
                   and e["detail"].get("status") == "processed"]
    applied = [e for e in seller_acks if e["detail"]["note"].get("applied")]
    if profile.fault == "duplicate_delivery":
        # The duplicate this profile injects is the town's re-offer, and only
        # an acknowledgement under that offer's fence shows it was handled.
        # A seller that lost its lease acknowledges the redelivery as a
        # duplicate too, carrying the application with it, before the town
        # has offered anything: stopping there ends the run first.
        offered = {e["detail"].get("fence") for e in events
                   if e["kind"] == "duplicate_offered"} - {None}
        duplicates = [e for e in seller_acks
                      if e["detail"]["note"].get("duplicate")
                      and e["detail"].get("fence") in offered]
        return bool(applied) and bool(duplicates)
    return bool(applied)


def _buyer_settled_response(events: list[dict[str, Any]]) -> bool:
    """Has the buyer acknowledged a quote response it was sent?

    Any status except ``retryable`` settles the buyer's claim, so by then
    the buyer has recorded whatever it will assert about the response.
    An externally joined buyer has no process for the runner to watch;
    this is how the runner knows that buyer is finished.
    """
    responses = {e["subject"] for e in events
                 if e["kind"] == "message_accepted"
                 and e["detail"].get("kind") == RESPONSE_KIND}
    return any(e["kind"] == "ack_recorded"
               and e["observer"] == "buyer"
               and e["subject"] in responses
               and e["detail"].get("status") != "retryable"
               for e in events)


def _response_accepted(events: list[dict[str, Any]], buyer: str) -> bool:
    """Has the town accepted a quote response addressed to the buyer?

    From then on the response waits in the buyer's inbox: a seller that
    exits has finished its part, and claiming and judging the response
    is the buyer's. A response sent to anyone else never reaches that
    inbox, so it leaves the buyer nothing to finish.
    """
    return any(e["kind"] == "message_accepted"
               and e["detail"].get("kind") == RESPONSE_KIND
               and e["detail"].get("to") == buyer
               for e in events)


def run_town(profile_name: str, out_dir: str, port: int = 0,
             model: str | None = None,
             external: dict[str, list[str] | None] | None = None,
             harnesses: dict[str, str] | None = None,
             wait_timeout: float = 45.0,
             identity_dir: str | None = None,
             on_credentials=None) -> tuple[str, Any]:
    """Run one Track profile.

    harnesses maps a role to a connector spec (see parse_harness) and
    overrides the profile's runtimes. external is the lower-level form:
    a role mapped to a replacement command, or to None to spawn nothing
    and hand join credentials to on_credentials(role, env) so an
    outside agent can join. Command harnesses are trusted operator code and
    inherit the operator environment; bundled harnesses do not.
    """
    if profile_name not in PROFILES:
        raise RunnerError(f"unknown profile {profile_name!r};"
                          f" choose from {sorted(PROFILES)}")
    profile = PROFILES[profile_name]
    _validate_role_overrides(profile, harnesses, external)
    check_wait_timeout(wait_timeout)
    model = model or os.environ.get("TOWN_MODEL", "mock:v1")
    admin_token = secrets.token_hex(16)
    port = port or _free_port()
    url = f"http://127.0.0.1:{port}"

    os.makedirs(out_dir, exist_ok=True)
    scratch = os.path.join(out_dir, f".scratch-{secrets.token_hex(4)}")
    os.makedirs(scratch, exist_ok=True)
    db_path = os.path.join(scratch, "town.db")

    env = {key: os.environ[key] for key in _BUILTIN_ENV_KEYS
           if key in os.environ}
    env["TOWN_ADMIN_TOKEN"] = admin_token
    admin = httpx.Client(base_url=url, timeout=10.0,
                         headers={"X-Town-Admin": admin_token})
    procs: list[subprocess.Popen] = []
    bundle_dir: str | None = None
    try:
        with _stop_signals_held():
            coordinator = subprocess.Popen(
                [sys.executable, "-m", "nandatown.coordinator",
                 "--db", db_path, "--port", str(port)],
                env=env, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=(os.name == "posix"),
            )
            procs.append(coordinator)
        _wait_health(admin)
        keystore = None
        create_body: dict[str, Any] = {"profile": profile.model_dump()}
        if identity_dir:
            from .identity_portable import Keystore

            keystore = Keystore(identity_dir)
            create_body["identities"] = {
                role: {k: v for k, v in
                       keystore.new_identity(role).items()
                       if k in ("agent_id", "controller_public")}
                for role in profile.roles}
        created = admin.post("/runs", json=create_body)
        created.raise_for_status()
        run_id = created.json()["run_id"]
        tokens = created.json()["join_tokens"]
        if keystore is not None:
            import json as _json

            grants = {role: _json.dumps(keystore.make_grant(role, run_id))
                      for role in profile.roles}
        else:
            grants = {}

        def post_event(observer: str, kind: str, subject: str,
                       detail: dict | None = None) -> None:
            admin.post(f"/runs/{run_id}/events",
                       json={"observer": observer, "kind": kind,
                             "subject": subject, "detail": detail or {}})

        def get_events() -> list[dict[str, Any]]:
            return admin.get(f"/runs/{run_id}/events").json()["events"]

        seller_state = os.path.join(scratch, "seller")
        buyer_state = os.path.join(scratch, "buyer")
        seller_cmd, seller_env, seller_kind = _participant_command(
            profile, "seller", external, harnesses)
        buyer_cmd, buyer_env, buyer_kind = _participant_command(
            profile, "buyer", external, harnesses)

        # A per-role harness model outranks the run-level model.
        seller_env = _participant_extra_env(seller_kind, seller_env, model)
        buyer_env = _participant_extra_env(buyer_kind, buyer_env, model)
        if "seller" in grants:
            seller_env["TOWN_GRANT"] = grants["seller"]
        if "buyer" in grants:
            buyer_env["TOWN_GRANT"] = grants["buyer"]

        seller_deadline = str(wait_timeout - SELLER_DEADLINE_MARGIN)
        buyer_deadline = str(wait_timeout - BUYER_DEADLINE_MARGIN)

        def hand_off(role: str, state_dir: str) -> None:
            """Credentials for an agent that joins from outside.

            The same environment a spawned participant gets, less FAULT.
            An outside agent is the subject, and FAULT is how this town
            tells its own scripted participants to misbehave on cue; a
            subject is observed, not instructed to fail. DEADLINE is
            handed over, because how long this run will wait is a fact
            about the run and the outside agent has no other way to know
            it.

            A role pinned to a portable identity also receives its Run
            Grant, which is the only credential the town will accept
            from it.
            """
            if on_credentials is None:
                return
            os.makedirs(state_dir, exist_ok=True)
            env = {"TOWN_URL": url, "RUN_ID": run_id, "NAME": role,
                   "TOKEN": tokens[role], "STATE_DIR": state_dir,
                   "DEADLINE": (seller_deadline if role == "seller"
                                else buyer_deadline)}
            if role in grants:
                env["TOWN_GRANT"] = grants[role]
            on_credentials(role, env)

        def spawn_seller() -> subprocess.Popen | None:
            if seller_cmd is None:
                hand_off("seller", seller_state)
                return None
            with _stop_signals_held():
                p = _spawn_participant(seller_cmd, url, run_id, "seller",
                                       tokens["seller"], seller_state,
                                       profile.fault, seller_deadline,
                                       extra_env=seller_env,
                                       inherit_env=(seller_kind == "cmd"))
                procs.append(p)
            return p

        seller = spawn_seller()
        if buyer_cmd is None:
            hand_off("buyer", buyer_state)
            buyer = None
        else:
            with _stop_signals_held():
                buyer = _spawn_participant(buyer_cmd, url, run_id, "buyer",
                                           tokens["buyer"], buyer_state,
                                           profile.fault, buyer_deadline,
                                           extra_env=buyer_env,
                                           inherit_env=(buyer_kind == "cmd"))
                procs.append(buyer)

        restarted = False
        seller_done = False
        # The participant the evaluator judges as the buyer.
        buyer_name = next((n for n, r in profile.roles.items()
                           if r == "buyer"), "buyer")
        refused_role: str | None = None
        deadline = time.time() + wait_timeout
        while time.time() < deadline:
            if buyer is not None and buyer.poll() is not None:
                break
            if buyer is None:
                # Seller-side completion is not the end of an external
                # buyer's turn: it still has to claim and judge the reply.
                # A seller that has exited is finished whatever its
                # acknowledgements say, and waiting for a note it can no
                # longer send just spends the deadline: the protocol asks
                # a seller to acknowledge, not to say "applied".
                events = get_events()
                if ((seller_done or _quiescent(profile, events))
                        and _buyer_settled_response(events)):
                    break
            if grants:
                # Reuse this iteration's fetch when the buyer check made one.
                refused_role = _grant_refused(
                    events if buyer is None else get_events())
                if refused_role:
                    post_event("runner", "harness_refused_grant",
                               refused_role,
                               {"reason": "joined with a bare token while"
                                          " pinned to a portable identity;"
                                          " this harness must present"
                                          " TOWN_GRANT"})
                    break
            if seller is not None and not seller_done:
                rc = seller.poll()
                if rc is not None:
                    _stop_process(seller)
                    if rc == SELLER_CRASH_EXIT and not restarted:
                        post_event("runner", "participant_crashed",
                                   "seller", {"exit_code": rc})
                        seller = spawn_seller()
                        post_event("runner", "participant_restarted",
                                   "seller")
                        restarted = True
                    else:
                        post_event("runner", "participant_exited",
                                   "seller", {"exit_code": rc})
                        if not _response_accepted(get_events(), buyer_name):
                            break
                        # The seller left after its quote response to the
                        # buyer was accepted, so its part is over, but the
                        # buyer still has to claim and judge that response.
                        # It gets the rest of the same deadline to do so,
                        # whether it is a process this runner watches or an
                        # outside agent it only sees acknowledge.
                        seller_done = True
            time.sleep(0.1)
        if buyer is not None:
            buyer_exit = _stop_process(buyer)
            post_event("runner", "participant_exited", "buyer",
                       {"exit_code": buyer_exit})

        # This settling time is the seller's, so a seller that has already
        # exited needs none of it: waiting on a note it can no longer send
        # only delays the bundle.
        quiet_deadline = time.time() + (0.0 if refused_role else 8.0)
        while time.time() < quiet_deadline:
            if seller_done or _quiescent(profile, get_events()):
                break
            time.sleep(0.2)

        if seller is not None:
            _stop_process(seller)
        finished = admin.post(f"/runs/{run_id}/finish")
        finished.raise_for_status()

        raw_events = get_events()
        events = [TownEvent.model_validate(e) for e in raw_events]
        intents = admin.get(f"/runs/{run_id}/intents").json()["intents"]
        participant_kinds = {"buyer": buyer_kind, "seller": seller_kind}
        participant_provenance: dict[str, dict[str, Any]] = {}
        directory = []
        for name, role in profile.roles.items():
            release, provenance = _participant_provenance(
                role, participant_kinds[name])
            participant_provenance[name] = provenance
            directory.append({
                "name": name,
                "role": role,
                "capabilities": profile.capabilities.get(name, []),
                "runtime": participant_kinds[name],
                "release": release,
            })
        created_at = next((e.at for e in events if e.kind == "run_created"),
                          time.time())
        from .skills import skill_source
        skill_releases = [
            {"kind": "skill", "name": name, "version": "1",
             "content_fingerprint": fingerprint(skill_source(name))}
            for name in ("town-protocol", "quote.read", "quote.request")
        ]
        uses_llm = "llm" in participant_kinds.values()
        recorded_harnesses = _recorded_harnesses(
            profile, participant_kinds, harnesses, external)
        config: dict[str, Any] = {"port": port,
                                  "restarted_seller": restarted,
                                  "runtimes": participant_kinds,
                                  "participant_provenance":
                                  participant_provenance,
                                  "skill_releases": skill_releases}
        if recorded_harnesses:
            config["harnesses"] = recorded_harnesses
        rerun, rerun_required_inputs = _rerun_metadata(
            profile, recorded_harnesses, participant_kinds, external,
            harnesses, identity_dir, uses_llm, model)
        config["rerun_command"] = rerun
        if rerun_required_inputs:
            config["rerun_required_inputs"] = rerun_required_inputs
        if uses_llm:
            config["model"] = model
            if not model.startswith("mock:"):
                config["model_note"] = ("hosted model recorded as an"
                                        " observed mutable dependency;"
                                        " it can change under a pinned"
                                        " release")
        run_record = RunRecord(
            run_id=run_id,
            profile_name=profile.name,
            profile_fingerprint=fingerprint(profile.model_dump()),
            created_at=created_at,
            participants=directory,
            releases={
                "nandatown": __version__,
                "evaluator": EVALUATOR_VERSION,
                "python": sys.version.split()[0],
            },
            config=config,
        )
        result = evaluate(profile, run_id, events)
        bundle_dir = os.path.join(out_dir, run_id)
        write_bundle(bundle_dir, profile, run_record, intents, events, result)
        from .bundle import attest_bundle
        attest_bundle(bundle_dir)
        return bundle_dir, result
    finally:
        for p in procs:
            _stop_process(p)
        admin.close()
        # Keep the operational state (town.db, journals) inspectable
        # inside the bundle once the processes that owned it are gone.
        if bundle_dir and os.path.isdir(bundle_dir):
            shutil.move(scratch, os.path.join(bundle_dir, "state"))
