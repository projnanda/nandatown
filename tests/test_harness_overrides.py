import json
import os
import shlex
import subprocess
import sys
import time

import pytest

from nandatown import __version__
import nandatown.runner as runner_module
from nandatown.bundle import load_bundle
from nandatown.cli import main
from nandatown.runner import RunnerError, parse_harness, run_town
from nandatown.sim.runner import run_lab

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "byoa_seller.py")


def test_parse_harness_specs():
    assert parse_harness("scripted") == {"kind": "scripted"}
    assert parse_harness("llm") == {"kind": "llm", "model": None}
    assert parse_harness("llm:qwen2.5") == {"kind": "llm",
                                            "model": "qwen2.5"}
    assert parse_harness('cmd:python my_agent.py --fast') == {
        "kind": "cmd", "command": ["python", "my_agent.py", "--fast"]}
    assert parse_harness("external") == {"kind": "external"}
    with pytest.raises(RunnerError):
        parse_harness("telepathy")
    with pytest.raises(RunnerError):
        parse_harness("cmd:")


@pytest.fixture
def reject_startup(monkeypatch):
    def fail_if_started():
        raise AssertionError("invalid role reached port allocation")

    monkeypatch.setattr(runner_module, "_free_port", fail_if_started)


@pytest.mark.parametrize(("override_name", "overrides", "role"), [
    ("harnesses", {"seler": "cmd:/does/not/exist"}, "seler"),
    ("harnesses", {"": "scripted"}, ""),
    ("external", {"seler": None}, "seler"),
    ("external", {"": None}, ""),
    ("harnesses", {"seller": "scripted", "seler": "external"},
     "seler"),
    ("external", {"seller": None, "seler": None}, "seler"),
])
def test_run_town_rejects_unknown_override_roles_before_startup(
        tmp_path, reject_startup, override_name, overrides, role):
    out_dir = tmp_path / "not-created"

    with pytest.raises(
            RunnerError,
            match=rf"unknown role {role!r}; supported roles: buyer, seller"):
        run_town("quote-clean", str(out_dir), **{override_name: overrides})

    assert not out_dir.exists()


@pytest.mark.parametrize(("agent", "role"), [
    ("seler=cmd:/does/not/exist", "seler"),
    ("=scripted", ""),
])
def test_cli_reports_unknown_harness_role_as_usage_error(
        tmp_path, capsys, reject_startup, agent, role):
    out_dir = tmp_path / "not-created"

    assert main(["run", "quote-clean", "--agent",
                 agent, "--out", str(out_dir)]) == 2

    assert f"unknown role {role!r}; supported roles: buyer, seller" in (
        capsys.readouterr().out)
    assert not out_dir.exists()


def test_cmd_harness_runs_external_agent(tmp_path):
    secret = "command-secret-must-not-enter-evidence"
    spec = "cmd:" + " ".join(shlex.quote(p)
                             for p in [sys.executable, EXAMPLE, secret])
    bundle_dir, result = run_town("quote-clean", str(tmp_path),
                                  harnesses={"seller": spec})
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    bundle = load_bundle(bundle_dir)
    run = bundle["run"]
    seller = next(p for p in run.participants if p["name"] == "seller")
    buyer = next(p for p in run.participants if p["name"] == "buyer")
    assert buyer["runtime"] == "scripted"
    assert buyer["release"] == (
        f"nandatown.participants.buyer {__version__}")
    assert seller["runtime"] == "cmd"
    assert seller["release"] == (
        "external command; immutable release not recorded")
    assert run.config["runtimes"]["seller"] == "cmd"
    assert run.config["harnesses"] == {
        "seller": "cmd:<operator-supplied-command>"}
    assert run.config["participant_provenance"]["seller"] == {
        "kind": "cmd",
        "identity_basis": "operator-supplied command (command not recorded)",
        "release_basis": None,
        "release_basis_note": "immutable external release not supplied",
    }
    assert run.config["rerun_required_inputs"] == {
        "seller": "original command (not recorded)"}
    assert "<operator-supplied-command>" in run.config["rerun_command"]
    serialized_run = json.dumps(run.model_dump())
    assert secret not in serialized_run
    assert EXAMPLE not in serialized_run


def test_wait_handoff_records_external_participant_and_reconnect_rerun(
        tmp_path):
    processes: list[subprocess.Popen] = []

    def connect(role, env):
        assert role == "seller"
        processes.append(subprocess.Popen(
            [sys.executable, EXAMPLE], env={**os.environ, **env},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    try:
        bundle_dir, result = run_town(
            "quote-clean", str(tmp_path), external={"seller": None},
            on_credentials=connect)
    finally:
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)

    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    run = load_bundle(bundle_dir)["run"]
    seller = next(p for p in run.participants if p["name"] == "seller")
    assert seller["runtime"] == "external"
    assert seller["release"] == (
        "external participant; immutable release not recorded")
    assert run.config["harnesses"] == {"seller": "external"}
    assert run.config["participant_provenance"]["seller"] == {
        "kind": "external",
        "identity_basis": (
            "operator-connected participant (software identity not supplied)"),
        "release_basis": None,
        "release_basis_note": "immutable external release not supplied",
    }
    assert run.config["rerun_command"] == (
        "nandatown test-agent --profile quote-clean --role seller --wait")
    assert run.config["rerun_required_inputs"] == {
        "seller": "external participant must reconnect with fresh credentials"}


# A town-joining buyer that deliberates before it acknowledges the quote
# response. argv[1] is the think time in seconds; argv[2] is "assert" to
# acknowledge with its correctness note, or "silent" to never acknowledge.
DELIBERATE_BUYER = """\
import os, sys, time
from nandatown.client import TownClient

think, mode = float(sys.argv[1]), sys.argv[2]
client = TownClient(os.environ["TOWN_URL"], os.environ["RUN_ID"])
client.join_auto(os.environ["NAME"], os.environ["TOKEN"], None)
task = client.run_context["task"]
seller = next(p["name"] for p in client.participants()
              if "quote.read" in p["capabilities"])
client.send(message_id="q-1", to=seller, kind="quote_request",
            body={key: task[key]
                  for key in ("sku", "quantity", "unit_price_cents")})
claim, deadline = None, time.time() + 30
while claim is None and time.time() < deadline:
    client.notify(wait=0.2)
    claim = client.claim()
time.sleep(think)
if mode == "assert":
    total = claim["body"]["total_cents"]
    client.ack(claim["message_id"], claim["fence"], "processed",
               {"correct": total == task["expected_total_cents"],
                "total_cents": total})
"""


def _run_with_buyer(tmp_path, connection, think, mode, wait_timeout):
    """Run quote-clean with the deliberate buyer as the external subject,
    joined through the --wait handoff or started as a --cmd command."""
    script = tmp_path / "deliberate_buyer.py"
    script.write_text(DELIBERATE_BUYER)
    command = [sys.executable, str(script), str(think), mode]
    processes: list[subprocess.Popen] = []

    def connect(role, env):
        assert role == "buyer"
        processes.append(subprocess.Popen(
            command, env={**os.environ, **env},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    external = {"buyer": None if connection == "wait" else command}
    try:
        _, result = run_town("quote-clean", str(tmp_path / "runs"),
                             external=external, wait_timeout=wait_timeout,
                             on_credentials=connect)
    finally:
        for process in processes:
            try:
                # A silent buyer never finishes by itself.
                process.wait(timeout=5 if mode == "assert" else 0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    return result, processes


@pytest.mark.parametrize("connection", ["wait", "cmd"])
def test_external_buyer_is_judged_on_its_own_late_assertion(
        tmp_path, connection):
    # The stock seller acks within milliseconds; the buyer subject takes
    # longer to check the quote. The run must stay open for its verdict.
    result, processes = _run_with_buyer(tmp_path, connection, think=1.5,
                                        mode="assert", wait_timeout=30)

    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    correct = next(s for s in result.stages if s.name == "correct")
    assert correct.status == "passed", detail
    if connection == "wait":
        assert [p.returncode for p in processes] == [0]


def test_external_buyer_that_never_asserts_is_incomplete_within_timeout(
        tmp_path):
    started = time.monotonic()
    result, _ = _run_with_buyer(tmp_path, "wait", think=3600,
                                mode="silent", wait_timeout=20)
    elapsed = time.monotonic() - started

    stages = {s.name: s for s in result.stages}
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "incomplete", detail
    assert stages["response"].status == "passed", detail
    assert stages["correct"].status == "not_enough_evidence", detail
    assert stages["correct"].note == "the buyer made no correctness assertion"
    # The stock seller no longer ends the run early, so this is the whole
    # deadline plus settling, not the deadline less the seller's exit.
    assert elapsed < 20 + 12


# A seller subject that serves exactly one request and exits 0. With
# argv[1] "respond" it sends the quote response and acknowledges the
# request first; "misaddressed" does the same but sends the response to
# itself instead of the buyer; "bare" responds and acknowledges with only
# what town-protocol.md asks for, which does not include "applied"; with
# "silent" it claims the request and leaves.
ONE_SHOT_SELLER = """\
import os, sys, time
from nandatown.client import TownClient

client = TownClient(os.environ["TOWN_URL"], os.environ["RUN_ID"])
client.join_auto(os.environ["NAME"], os.environ["TOKEN"], None)
claim, deadline = None, time.time() + 30
while claim is None and time.time() < deadline:
    client.notify(wait=0.2)
    claim = client.claim()
if claim is not None and sys.argv[1] in ("respond", "misaddressed", "bare"):
    body = claim["body"]
    total = body["quantity"] * body["unit_price_cents"]
    to = claim["from"] if sys.argv[1] != "misaddressed" else os.environ["NAME"]
    client.send(message_id="r-1", to=to, kind="quote_response",
                body={"request_id": claim["message_id"],
                      "total_cents": total})
    note = ({"total_cents": total} if sys.argv[1] == "bare"
            else {"applied": True, "total_cents": total})
    client.ack(claim["message_id"], claim["fence"], "processed", note)
"""

# The stock buyer on a slow host: each claim starts a second late, so the
# buyer is still on its way to the reply when a quick seller exits.
SLOW_STOCK_BUYER = """\
import time
from nandatown.client import TownClient
from nandatown.participants import buyer

claim = TownClient.claim
TownClient.claim = lambda self: (time.sleep(1.0), claim(self))[1]
buyer.main()
"""


@pytest.fixture
def slow_stock_buyer(monkeypatch):
    spawn = runner_module._spawn_participant

    def spawn_slow_buyer(command, url, run_id, name, *args, **kwargs):
        if name == "buyer":
            assert command == [sys.executable, "-m",
                               "nandatown.participants.buyer"]
            command = [sys.executable, "-c", SLOW_STOCK_BUYER]
        return spawn(command, url, run_id, name, *args, **kwargs)

    monkeypatch.setattr(runner_module, "_spawn_participant", spawn_slow_buyer)


def _test_one_shot_seller(tmp_path, capsys, mode):
    script = tmp_path / "one_shot_seller.py"
    script.write_text(ONE_SHOT_SELLER)
    command = " ".join(shlex.quote(part)
                       for part in [sys.executable, str(script), mode])
    started = time.monotonic()
    code = main(["test-agent", "--role", "seller", "--cmd", command,
                 "--out", str(tmp_path / "runs")])
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out
    bundle_dir = out.rsplit("Evidence bundle: ", 1)[1].strip()
    bundle = load_bundle(bundle_dir)
    return code, bundle["result"], bundle["events"], elapsed


def _run_one_shot_seller_with_buyer(tmp_path, connection, wait_timeout=30,
                                    seller_mode="respond"):
    """Both sides are subjects: a one-shot seller command, and a buyer that
    deliberates, joined through the --wait handoff or run as a command."""
    seller_script = tmp_path / "one_shot_seller.py"
    seller_script.write_text(ONE_SHOT_SELLER)
    buyer_script = tmp_path / "deliberate_buyer.py"
    buyer_script.write_text(DELIBERATE_BUYER)
    seller_command = [sys.executable, str(seller_script), seller_mode]
    buyer_command = [sys.executable, str(buyer_script), "1.5", "assert"]
    processes: list[subprocess.Popen] = []

    def connect(role, env):
        assert role == "buyer"
        processes.append(subprocess.Popen(
            buyer_command, env={**os.environ, **env},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    external = {"seller": seller_command,
                "buyer": None if connection == "wait" else buyer_command}
    started = time.monotonic()
    try:
        _, result = run_town("quote-clean", str(tmp_path / "runs"),
                             external=external, wait_timeout=wait_timeout,
                             on_credentials=connect)
    finally:
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    return result, time.monotonic() - started


@pytest.mark.parametrize("connection", ["wait", "cmd"])
def test_one_shot_seller_does_not_cut_off_an_external_buyer(
        tmp_path, connection):
    # The seller answers and exits, which ends the seller's turn but not
    # the buyer's. An externally joined buyer has no process to watch, so
    # the run has to keep waiting for its acknowledgement exactly as it
    # would for a managed one, which is the control here.
    result, elapsed = _run_one_shot_seller_with_buyer(tmp_path, connection)

    detail = [(s.name, s.status, s.note) for s in result.stages]
    stages = {s.name: s for s in result.stages}
    assert result.verdict == "passed", detail
    assert stages["response"].status == "passed", detail
    assert stages["correct"].status == "passed", detail
    # Not truncated is half of it: the run must also end when the buyer
    # settles rather than sit out the 30 s deadline it was given.
    assert elapsed < 10, detail


@pytest.mark.parametrize("connection", ["wait", "cmd"])
def test_a_seller_that_acks_only_what_the_protocol_asks_ends_the_run(
        tmp_path, connection):
    """town-protocol.md documents a status and a note of observations.

    It never mentions "applied", so a seller written to it says nothing
    the seller-side quiescence check recognises. Once such a seller has
    exited there is nothing further to wait for, and the run must not
    spend the rest of its deadline waiting for a note that can no longer
    arrive.
    """
    result, elapsed = _run_one_shot_seller_with_buyer(
        tmp_path, connection, seller_mode="bare")

    detail = [(s.name, s.status, s.note) for s in result.stages]
    stages = {s.name: s for s in result.stages}
    assert stages["response"].status == "passed", detail
    assert stages["correct"].status == "passed", detail
    assert elapsed < 10, detail


def test_one_shot_seller_with_a_silent_external_buyer_ends_on_the_deadline(
        tmp_path):
    # Waiting for the buyer is still bounded: nothing acknowledges the
    # reply, so the run ends incomplete on its own timeout rather than
    # hanging, and the missing evidence is named as the buyer's.
    seller_script = tmp_path / "one_shot_seller.py"
    seller_script.write_text(ONE_SHOT_SELLER)
    buyer_script = tmp_path / "deliberate_buyer.py"
    buyer_script.write_text(DELIBERATE_BUYER)
    processes: list[subprocess.Popen] = []

    def connect(role, env):
        processes.append(subprocess.Popen(
            [sys.executable, str(buyer_script), "3600", "silent"],
            env={**os.environ, **env},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    started = time.monotonic()
    try:
        _, result = run_town(
            "quote-clean", str(tmp_path / "runs"),
            external={"seller": [sys.executable, str(seller_script),
                                 "respond"],
                      "buyer": None},
            wait_timeout=20, on_credentials=connect)
    finally:
        for process in processes:
            process.kill()
            process.wait(timeout=5)
    elapsed = time.monotonic() - started

    stages = {s.name: s for s in result.stages}
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "incomplete", detail
    assert stages["response"].status == "passed", detail
    assert stages["correct"].status == "not_enough_evidence", detail
    assert elapsed < 20 + 10


def test_misaddressed_one_shot_seller_ends_promptly_with_an_external_buyer(
        tmp_path):
    # Nothing ever reaches the buyer's inbox, so there is nothing for it to
    # finish and no reason to hold the run open for the full timeout.
    seller_script = tmp_path / "one_shot_seller.py"
    seller_script.write_text(ONE_SHOT_SELLER)
    buyer_script = tmp_path / "deliberate_buyer.py"
    buyer_script.write_text(DELIBERATE_BUYER)
    processes: list[subprocess.Popen] = []

    def connect(role, env):
        processes.append(subprocess.Popen(
            [sys.executable, str(buyer_script), "3600", "silent"],
            env={**os.environ, **env},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    started = time.monotonic()
    try:
        bundle_dir, result = run_town(
            "quote-clean", str(tmp_path / "runs"),
            external={"seller": [sys.executable, str(seller_script),
                                 "misaddressed"],
                      "buyer": None},
            wait_timeout=60, on_credentials=connect)
    finally:
        for process in processes:
            process.kill()
            process.wait(timeout=5)
    elapsed = time.monotonic() - started

    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "incomplete", detail
    # A response really was sent and accepted; it just went to the wrong
    # participant. Without this the test would pass on a seller that sent
    # nothing, which is a different case entirely.
    accepted = [e for e in load_bundle(bundle_dir)["events"]
                if e.kind == "message_accepted"
                and e.detail.get("kind") == "quote_response"]
    assert [e.detail["to"] for e in accepted] == ["seller"], accepted
    assert elapsed < 40, detail


def test_one_shot_seller_is_judged_after_the_stock_buyer_claims(
        tmp_path, capsys, slow_stock_buyer):
    # The subject answered, acknowledged and exited 0: its job is done.
    # Town's own buyer still has to claim and check the reply, and must not
    # be stopped on the way because the seller left first.
    code, result, events, _ = _test_one_shot_seller(tmp_path, capsys,
                                                    "respond")

    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert (code, result.verdict) == (0, "passed"), detail
    seller_exit = next(e for e in events if e.kind == "participant_exited"
                       and e.subject == "seller")
    buyer_claim = next(e for e in events if e.kind == "message_claimed"
                       and e.subject == "r-1")
    buyer_exit = next(e for e in events if e.kind == "participant_exited"
                      and e.subject == "buyer")
    assert seller_exit.detail == {"exit_code": 0}
    assert seller_exit.at < buyer_claim.at  # the race this guards
    assert buyer_exit.detail == {"exit_code": 0}  # finished on its own


def test_seller_that_exits_without_responding_still_ends_the_run(
        tmp_path, capsys):
    code, result, events, elapsed = _test_one_shot_seller(
        tmp_path, capsys, "silent")

    stages = {s.name: s for s in result.stages}
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert (code, result.verdict) == (1, "incomplete"), detail
    assert stages["response"].status == "not_enough_evidence", detail
    assert any(e.kind == "participant_exited" and e.subject == "seller"
               and e.detail == {"exit_code": 0} for e in events)
    # Ended by the seller's exit (plus the settle wait), not by the
    # stock buyer's 45 s deadline or the 60 s timeout.
    assert elapsed < 30


def test_seller_whose_response_is_not_for_the_buyer_still_ends_the_run(
        tmp_path, capsys):
    # The seller answered, acknowledged and exited 0, but sent its quote
    # response to itself. Nothing waits in the buyer's inbox, so Town's
    # buyer has nothing left to finish and the seller's exit ends the run.
    code, result, events, elapsed = _test_one_shot_seller(
        tmp_path, capsys, "misaddressed")

    stages = {s.name: s for s in result.stages}
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert (code, result.verdict) == (1, "incomplete"), detail
    assert stages["response"].status == "not_enough_evidence", detail
    accepted = next(e for e in events if e.kind == "message_accepted"
                    and e.subject == "r-1")
    assert accepted.detail["to"] == "seller"
    assert any(e.kind == "participant_exited" and e.subject == "seller"
               and e.detail == {"exit_code": 0} for e in events)
    # Not held until the stock buyer gives up at its 45 s deadline.
    assert elapsed < 15


def test_llm_harness_overrides_scripted_profile(tmp_path):
    bundle_dir, result = run_town("quote-clean", str(tmp_path),
                                  harnesses={"seller": "llm:mock:alt"})
    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    bundle = load_bundle(bundle_dir)
    assert bundle["run"].config["model"] == "mock:v1"
    assert bundle["run"].config["harnesses"]["seller"] == "llm:mock:alt"
    seller = next(p for p in bundle["run"].participants
                  if p["name"] == "seller")
    assert seller["runtime"] == "llm"
    assert seller["release"] == f"nandatown.participants.llm {__version__}"


def test_layer_override_reproduces_weak_auth_failure(tmp_path):
    bundle_dir, result = run_lab("capability_spoofing", str(tmp_path),
                                 layer_overrides={"auth": "plain.v1"})
    stages = {s.name: s.status for s in result.stages}
    assert result.verdict == "failed", stages
    assert stages["containment"] == "failed"
    bundle = load_bundle(bundle_dir)
    assert bundle["profile"].layers["auth"] == "plain.v1"


def test_plugin_flag_loads_scaffolded_plugin(tmp_path):
    from nandatown.new import scaffold

    path = scaffold("plugin", "memory", "scratch.v1", str(tmp_path))
    run_lab("voting", str(tmp_path / "runs"), plugins=[path])
    from nandatown.layers import resolve
    assert resolve("memory", "scratch.v1").plugin_id == "scratch.v1"


def test_cli_flag_scoping(tmp_path, capsys):
    assert main(["run", "voting", "--agent", "seller=llm",
                 "--out", str(tmp_path)]) == 2
    assert "Track profiles" in capsys.readouterr().out
    assert main(["run", "quote-clean", "--layer", "auth=plain.v1",
                 "--out", str(tmp_path)]) == 2
    assert "Lab scenarios" in capsys.readouterr().out
    assert main(["run", "capability_spoofing", "--layer",
                 "auth=plain.v1", "--out", str(tmp_path)]) == 1
    assert "FAILED" in capsys.readouterr().out


_VALIDATOR_PLUGIN = '''
from nandatown.records import StageResult
from nandatown.sim.validators import validator


@validator("counts_votes.v1")
def counts_votes(spec, trace):
    return [StageResult(name="events_seen", status="passed",
                        note=f"{len(trace.events)} events")]
'''


def _bundle_with_plugin_validator(tmp_path):
    """Run the bundled voting scenario judged by a validator that only a
    plugin file defines, then forget the validator, as a fresh process
    verifying the bundle would start without it."""
    import yaml

    from nandatown.sim.scenario import load_bundled
    from nandatown.sim.validators import VALIDATORS

    plugin = tmp_path / "counts_votes.py"
    plugin.write_text(_VALIDATOR_PLUGIN, encoding="utf-8")
    doc = load_bundled("voting").model_dump(mode="json")
    doc["validator"] = "counts_votes.v1"
    doc["plugin_files"] = ["counts_votes.py"]
    scenario = tmp_path / "voting_custom.yaml"
    scenario.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle_dir, result = run_lab(str(scenario), str(tmp_path / "runs"))
    assert "events_seen" in [s.name for s in result.stages]
    VALIDATORS.pop("counts_votes.v1")
    return bundle_dir, str(plugin)


def test_verify_names_a_validator_it_cannot_replay(tmp_path, capsys):
    bundle_dir, _ = _bundle_with_plugin_validator(tmp_path)
    assert main(["verify", bundle_dir]) == 1
    out = capsys.readouterr().out
    assert "validator 'counts_votes.v1' is not registered here" in out
    assert "--plugin" in out
    assert "replay mismatch" not in out


def test_verify_plugin_flag_replays_a_plugin_validator(tmp_path, capsys):
    bundle_dir, plugin = _bundle_with_plugin_validator(tmp_path)
    assert main(["verify", bundle_dir, "--plugin", plugin]) == 0
    assert "bundle verified" in capsys.readouterr().out


def test_verify_plugin_flag_reports_an_unloadable_file(tmp_path, capsys):
    bundle_dir, _ = _bundle_with_plugin_validator(tmp_path)
    missing = str(tmp_path / "absent.py")
    assert main(["verify", bundle_dir, "--plugin", missing]) == 2
    assert "cannot load plugin file" in capsys.readouterr().out
