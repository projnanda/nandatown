"""Behavioral contract for the bounded SharpeShooter subprocess adapter."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "examples" / "sharpeshooter_adapter.py"
TASK_FILE = ROOT / "tests" / "fixtures" / "sharpeshooter_research_task_v1.json"
FIXTURE_SOURCE = ROOT / "tests" / "fixtures" / "sharpeshooter_task_fixture.py"
TASK_DIGEST = "sha256:cb03de74c7a8e08414d4d30cd84ad5ed1ef0b73bef3cf980095615e20d480a9a"
ERRORS = {
    "invalid_configuration": "Adapter configuration is invalid.",
    "invalid_request": "Research request is invalid.",
    "task_digest_mismatch": "Research task digest is not approved.",
    "state_conflict": "Adapter state conflicts with this request.",
    "interrupted": "A prior workflow started but did not complete.",
    "project_timeout": "SharpeShooter workflow timed out.",
    "project_failed": "SharpeShooter workflow failed.",
    "project_output_invalid": "SharpeShooter returned an invalid result.",
    "project_output_too_large": "SharpeShooter output exceeded its limit.",
    "internal_error": "Adapter failed closed.",
}


def canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value) -> str:
    data = value if isinstance(value, bytes) else canonical(value)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def task() -> dict:
    return json.loads(TASK_FILE.read_text(encoding="utf-8"))


def request(value: dict | None = None) -> dict:
    value = task() if value is None else value
    return {
        "schema_version": "sharpeshooter.research-request.v1",
        "task": value,
        "task_digest": digest(value),
    }


def api():
    if not ADAPTER.exists():
        pytest.fail("SharpeShooter adapter core is not implemented")
    spec = importlib.util.spec_from_file_location("sharpeshooter_adapter", ADAPTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("AdapterFailure", "process_request"):
        if not hasattr(module, name):
            pytest.fail(f"SharpeShooter adapter core lacks {name}")
    return module


def install_fixture(root: Path, mode: str = "ok") -> Path:
    root.mkdir(mode=0o700)
    executable = root / f"sharpeshooter-task-{mode}"
    interpreter = getattr(sys, "_base_executable", sys.executable)
    assert "\n" not in interpreter and " " not in interpreter
    executable.write_text(
        f"#!{interpreter}\n" + FIXTURE_SOURCE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def configured(tmp_path: Path, mode: str = "ok") -> tuple[dict[str, str], Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "project"
    executable = install_fixture(project, mode)
    runtime = tmp_path / f"runtime-{mode}"
    runtime.mkdir(mode=0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    env = {
        "TOWN_URL": "http://127.0.0.1:1",
        "RUN_ID": "run-1",
        "NAME": "seller",
        "TOKEN": "town-secret-marker",
        "STATE_DIR": str(state),
        "DEADLINE": "1893456000",
        "SHARPESHOOTER_ROOT": str(project),
        "SHARPESHOOTER_TASK_EXECUTABLE": str(executable),
        "SHARPESHOOTER_APPROVED_TASK_DIGEST": TASK_DIGEST,
        "SHARPESHOOTER_DB": str(runtime / "sharpeshooter.sqlite3"),
        "SHARPESHOOTER_CAPTURE": str(runtime / "capture.txt"),
        "HOME": "/fixture/home-secret",
        "CODEX_HOME": "/fixture/codex-secret",
        "HTTPS_PROXY": "http://proxy-secret.invalid",
        "OPENAI_API_KEY": "provider-secret-marker",
        "UNRELATED": "ambient-secret-marker",
    }
    return env, runtime


def call(module, tmp_path: Path, *, mode: str = "ok", body: dict | None = None,
         request_id: str = "research-1"):
    env, runtime = configured(tmp_path, mode)
    outcome = module.process_request(
        request_id=request_id,
        request_body=canonical(request() if body is None else body),
        environment=env,
    )
    return outcome, env, runtime


def expect_failure(module, code: str, function):
    with pytest.raises(module.AdapterFailure) as caught:
        function()
    payload = caught.value.payload
    assert payload == {
        "schema_version": "sharpeshooter.adapter-error.v1",
        "code": code,
        "message": ERRORS[code],
    }
    assert payload["message"].isascii() and len(payload["message"]) <= 160
    return payload


def test_fixture_command_owns_counter_and_emits_a_strict_project_result(tmp_path):
    """A second fixture launch must accumulate independently of adapter state."""
    env, runtime = configured(tmp_path)
    task_value = task()
    (runtime / "fixture-task.json").write_bytes(canonical(task_value))
    argv = [
        env["SHARPESHOOTER_TASK_EXECUTABLE"], "run", "--task", "-",
        "--db", env["SHARPESHOOTER_DB"], "--capture", env["SHARPESHOOTER_CAPTURE"],
        "--approved-task-digest", TASK_DIGEST, "--confirm-oos", "--verdict", "accept",
    ]
    import subprocess
    child_env = {
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp_path / "state"),
    }
    for expected in (1, 2):
        completed = subprocess.run(
            argv, input=canonical(task_value), capture_output=True,
            cwd=env["SHARPESHOOTER_ROOT"], env=child_env, check=True,
        )
        result = json.loads(completed.stdout)
        assert result["task_digest"] == TASK_DIGEST
        assert result["receipt_integrity_hash"] == digest(result["receipt"])
        assert int((runtime / "fixture-counter.txt").read_text()) == expected


def test_happy_path_uses_fixed_process_boundary_and_safe_completed_journal(tmp_path):
    """Shell interpolation, ambient env, or unsafe journaling must fail."""
    module = api()
    outcome, env, runtime = call(module, tmp_path)

    assert outcome["replayed"] is False
    assert outcome["message_id"] == "research-1-result"
    body = outcome["body"]
    assert set(body) == {"schema_version", "request_id", "request_digest", "project_result"}
    assert body["schema_version"] == "sharpeshooter.town-research-result.v1"
    assert body["request_id"] == "research-1"
    assert body["request_digest"] == digest(request())
    assert body["project_result"]["task_digest"] == TASK_DIGEST
    assert int((runtime / "fixture-counter.txt").read_text()) == 1

    observation = json.loads((runtime / "fixture-observation.json").read_text())
    assert observation["argv"] == [
        "run", "--task", "-", "--db", env["SHARPESHOOTER_DB"],
        "--capture", env["SHARPESHOOTER_CAPTURE"],
        "--approved-task-digest", TASK_DIGEST, "--confirm-oos", "--verdict", "accept",
    ]
    assert observation["cwd"] == env["SHARPESHOOTER_ROOT"]
    observed_environment = observation["env"]
    if sys.platform == "darwin":
        # macOS injects this process-bootstrap key after execve; it is not
        # present in the explicit environment supplied by the adapter.
        assert observed_environment.pop("__CF_USER_TEXT_ENCODING").startswith("0x")
    assert observed_environment == {
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(Path(env["STATE_DIR"]) / "tmp"),
    }
    assert observation["stdin_sha256"] == TASK_DIGEST
    assert observation["started_visible"] is True
    journal = json.loads((Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").read_text())
    assert set(journal) == {
        "version", "state", "request_id", "request_digest", "task_digest",
        "started_at", "result_message_id", "result_body", "result_body_digest",
        "project_result_digest", "capture_digest", "replay_facts",
    }
    assert journal["state"] == "completed"
    assert journal["result_body"] == body
    assert journal["replay_facts"] == {"project_invocations": 1, "response_count": 1}
    serialized = canonical(journal)
    for marker in (b"town-secret-marker", b"provider-secret-marker", b"fixture/home-secret"):
        assert marker not in serialized


def test_matching_completed_state_replays_identically_without_launch(tmp_path):
    """Duplicate delivery must not charge another project invocation."""
    module = api()
    first, env, runtime = call(module, tmp_path)
    journal_path = Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json"
    before = journal_path.read_bytes()
    second = module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    )
    assert second == {**first, "replayed": True}
    assert canonical(second["body"]) == canonical(first["body"])
    assert journal_path.read_bytes() == before
    assert int((runtime / "fixture-counter.txt").read_text()) == 1


def test_matching_started_state_fails_interrupted_without_launch(tmp_path):
    """An ambiguous prior launch must never be retried automatically."""
    module = api()
    env, runtime = configured(tmp_path)
    journal = {
        "version": 1, "state": "started", "request_id": "research-1",
        "request_digest": digest(request()), "task_digest": TASK_DIGEST,
        "started_at": "2026-11-09T00:00:00Z",
    }
    (Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").write_bytes(canonical(journal))
    expect_failure(module, "interrupted", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert not (runtime / "fixture-counter.txt").exists()


@pytest.mark.parametrize(
    "damage",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value.update({"schema_version": "wrong"}),
        lambda value: value["task"].update({"unexpected": True}),
        lambda value: value.update({"task_digest": "sha256:" + "0" * 64}),
    ],
    ids=("request-extra", "request-version", "task-extra", "supplied-digest-mismatch"),
)
def test_invalid_request_or_digest_never_launches(tmp_path, damage):
    """Trusting a supplied digest or loose envelope must launch forbidden work."""
    module = api()
    env, runtime = configured(tmp_path)
    body = request()
    damage(body)
    code = "task_digest_mismatch" if body.get("task_digest") != digest(body.get("task")) else "invalid_request"
    expect_failure(module, code, lambda: module.process_request(
        request_id="research-1", request_body=canonical(body), environment=env,
    ))
    assert not (runtime / "fixture-counter.txt").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda env, tmp: env.update({"SHARPESHOOTER_TASK_EXECUTABLE": "relative"}),
        lambda env, tmp: env.update({"SHARPESHOOTER_DB": str(Path(env["SHARPESHOOTER_DB"]).with_name("wrong.db"))}),
        lambda env, tmp: env.update({"SHARPESHOOTER_CAPTURE": str(Path(env["SHARPESHOOTER_CAPTURE"]).with_name("wrong.txt"))}),
        lambda env, tmp: env.update({"SHARPESHOOTER_CAPTURE": str(tmp / "other" / "capture.txt")}),
        lambda env, tmp: env.update({"SHARPESHOOTER_APPROVED_TASK_DIGEST": "sha256:" + "0" * 64}),
    ],
    ids=("relative-executable", "wrong-db-leaf", "wrong-capture-leaf", "different-runtime", "wrong-approval"),
)
def test_invalid_configuration_or_approval_never_launches(tmp_path, mutate):
    """Untrusted paths or approval state must fail before a child starts."""
    module = api()
    env, runtime = configured(tmp_path)
    mutate(env, tmp_path)
    code = "task_digest_mismatch" if env["SHARPESHOOTER_APPROVED_TASK_DIGEST"] != TASK_DIGEST else "invalid_configuration"
    expect_failure(module, code, lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert not (runtime / "fixture-counter.txt").exists()
    assert not (Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").exists()
    assert not (Path(env["STATE_DIR"]) / "tmp").exists()


@pytest.mark.parametrize(
    "damage",
    ("executable-symlink", "project-symlink", "runtime-symlink", "state-symlink",
     "unsafe-state-mode", "runtime-overlap", "existing-db", "existing-wal",
     "existing-shm", "existing-capture"),
)
def test_path_symlink_ownership_overlap_and_freshness_fail_before_state(tmp_path, damage):
    """Every unsafe component or preexisting runtime artifact must prevent launch."""
    module = api()
    env, runtime = configured(tmp_path)
    state = Path(env["STATE_DIR"])
    project = Path(env["SHARPESHOOTER_ROOT"])
    executable = Path(env["SHARPESHOOTER_TASK_EXECUTABLE"])
    if damage == "executable-symlink":
        link = project / "linked-executable"
        link.symlink_to(executable)
        env["SHARPESHOOTER_TASK_EXECUTABLE"] = str(link)
    elif damage == "project-symlink":
        link = tmp_path / "linked-project"
        link.symlink_to(project, target_is_directory=True)
        env["SHARPESHOOTER_ROOT"] = str(link)
        env["SHARPESHOOTER_TASK_EXECUTABLE"] = str(link / executable.name)
    elif damage == "runtime-symlink":
        linked = tmp_path / "linked-runtime"
        linked.symlink_to(runtime, target_is_directory=True)
        env["SHARPESHOOTER_DB"] = str(linked / "sharpeshooter.sqlite3")
        env["SHARPESHOOTER_CAPTURE"] = str(linked / "capture.txt")
    elif damage == "state-symlink":
        real_state = tmp_path / "real-state"
        real_state.mkdir(mode=0o700)
        state.rmdir()
        state.symlink_to(real_state, target_is_directory=True)
    elif damage == "unsafe-state-mode":
        state.chmod(0o777)
    elif damage == "runtime-overlap":
        env["SHARPESHOOTER_DB"] = str(project / "sharpeshooter.sqlite3")
        env["SHARPESHOOTER_CAPTURE"] = str(project / "capture.txt")
    else:
        suffix = {
            "existing-db": "", "existing-wal": "-wal", "existing-shm": "-shm",
            "existing-capture": None,
        }[damage]
        path = Path(env["SHARPESHOOTER_CAPTURE"]) if suffix is None else Path(env["SHARPESHOOTER_DB"] + suffix)
        path.write_bytes(b"sentinel")
    expect_failure(module, "invalid_configuration", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert not (runtime / "fixture-counter.txt").exists()
    assert not (state / "sharpeshooter-journal-v1.json").exists()
    assert not (state / "tmp").exists()


@pytest.mark.parametrize(
    "mode,code",
    [
        ("nonzero", "project_failed"),
        ("malformed", "project_output_invalid"),
        ("stdout-oversize", "project_output_too_large"),
        ("stderr-oversize", "project_output_too_large"),
        ("both-oversize", "project_output_too_large"),
        ("capture-oversize", "project_output_too_large"),
        ("unsafe-capture", "project_output_invalid"),
        ("mismatch", "project_output_invalid"),
        ("bad-receipt", "project_output_invalid"),
        ("unknown-result", "project_output_invalid"),
    ],
)
def test_child_failures_are_bounded_redacted_and_never_complete(tmp_path, mode, code):
    """Child-controlled bytes must never become a Town success or error detail."""
    module = api()
    env, _ = configured(tmp_path, mode)
    payload = expect_failure(module, code, lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    serialized = canonical(payload)
    for marker in (b"fixture-secret", b"town-secret", b"provider-secret", str(tmp_path).encode()):
        assert marker not in serialized
    journal = json.loads((Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").read_text())
    assert journal["state"] == "started"
    launches = int((Path(env["SHARPESHOOTER_DB"]).parent / "fixture-counter.txt").read_text())
    expect_failure(module, "interrupted", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert int((Path(env["SHARPESHOOTER_DB"]).parent / "fixture-counter.txt").read_text()) == launches
    if mode == "malformed":
        for extra_mode, extra_code in (
            ("project-oversize", "project_output_too_large"),
            ("stdout-limit", "project_output_too_large"),
            ("receipt-oversize", "project_output_too_large"),
        ):
            extra_env, _ = configured(tmp_path / extra_mode, extra_mode)
            expect_failure(module, extra_code, lambda extra_env=extra_env: module.process_request(
                request_id="research-1", request_body=canonical(request()), environment=extra_env,
            ))
    if mode == "bad-receipt":
        extra_env, _ = configured(tmp_path / "receipt-correlation", "bad-receipt-correlation")
        expect_failure(module, "project_output_invalid", lambda: module.process_request(
            request_id="research-1", request_body=canonical(request()), environment=extra_env,
        ))


def test_timeout_terminates_and_reaps_the_child_process_group(tmp_path, monkeypatch):
    """A timeout must not leave the fixture or its descendant running."""
    module = api()
    monkeypatch.setattr(module, "PROJECT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(module, "TERMINATION_GRACE_SECONDS", 0.1)
    env, runtime = configured(tmp_path, "timeout")
    expect_failure(module, "project_timeout", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    deadline = time.time() + 2
    descendant_file = runtime / "fixture-descendant.txt"
    while not descendant_file.exists() and time.time() < deadline:
        time.sleep(0.01)
    if descendant_file.exists():
        pid = int(descendant_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.parametrize(
    "journal_bytes",
    [b"{not-json", b"{}", b"x" * 131_073],
    ids=("malformed", "unknown-shape", "oversized"),
)
def test_unsafe_existing_journal_fails_closed_without_launch(tmp_path, journal_bytes):
    """Malformed or oversized replay state must never be ignored or replaced."""
    module = api()
    env, runtime = configured(tmp_path)
    path = Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json"
    path.write_bytes(journal_bytes)
    expect_failure(module, "state_conflict", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert not (runtime / "fixture-counter.txt").exists()


def test_journal_symlink_fails_closed_without_touching_target(tmp_path):
    """Following a journal symlink would escape the operator-owned state root."""
    module = api()
    env, runtime = configured(tmp_path)
    target = tmp_path / "outside.json"
    target.write_text("sentinel", encoding="ascii")
    (Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").symlink_to(target)
    expect_failure(module, "state_conflict", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert target.read_text() == "sentinel"
    assert not (runtime / "fixture-counter.txt").exists()


def test_reused_identity_with_changed_content_rejects_without_launch(tmp_path):
    """One Town identity cannot be rebound to another logical request."""
    module = api()
    first, env, runtime = call(module, tmp_path)
    changed = request()
    changed["task"]["idea"]["title"] = "Changed title"
    changed["task_digest"] = digest(changed["task"])
    expect_failure(module, "task_digest_mismatch", lambda: module.process_request(
        request_id="research-1", request_body=canonical(changed), environment=env,
    ))
    assert int((runtime / "fixture-counter.txt").read_text()) == 1


def test_duplicate_raw_keys_and_oversized_request_fail_before_state(tmp_path):
    """Ambiguous or over-limit wire input must not be decoded permissively."""
    module = api()
    env, runtime = configured(tmp_path)
    valid = canonical(request())
    ambiguous = valid.replace(
        b'{"schema_version":',
        b'{"schema_version":"sharpeshooter.research-request.v1","schema_version":',
        1,
    )
    exact_limit = valid + b" " * (65_536 - len(valid))
    for raw in (ambiguous, valid + b"\n", exact_limit, exact_limit + b" "):
        expect_failure(module, "invalid_request", lambda raw=raw: module.process_request(
            request_id="research-1", request_body=raw, environment=env,
        ))
        assert not (runtime / "fixture-counter.txt").exists()
        assert not (Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").exists()


def test_absent_state_race_launches_the_fixture_exactly_once(tmp_path):
    """Atomic state creation must serialize two first deliveries."""
    module = api()
    env, runtime = configured(tmp_path)
    barrier = threading.Barrier(2)
    outcomes = []

    def invoke():
        barrier.wait(timeout=5)
        try:
            outcomes.append(module.process_request(
                request_id="research-1", request_body=canonical(request()), environment=env,
            ))
        except module.AdapterFailure as error:
            outcomes.append(error.payload)

    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert int((runtime / "fixture-counter.txt").read_text()) == 1
    assert sum(value.get("message_id") == "research-1-result" for value in outcomes) >= 1
    assert all(
        value.get("message_id") == "research-1-result"
        or value.get("code") in {"interrupted", "state_conflict"}
        for value in outcomes
    )


def test_raw_task_and_journal_constants_match_the_frozen_contract():
    """Boundary drift must be explicit rather than silently widening exposure."""
    module = api()
    assert {
        "MAX_TASK_BYTES": 65_536,
        "MAX_PROJECT_RESULT_BYTES": 65_536,
        "MAX_STDOUT_BYTES": 131_072,
        "MAX_STDERR_BYTES": 16_384,
        "MAX_CAPTURE_BYTES": 32_768,
        "MAX_TOWN_RESULT_BYTES": 98_304,
        "MAX_JOURNAL_BYTES": 131_072,
        "PROJECT_TIMEOUT_SECONDS": 30,
        "TERMINATION_GRACE_SECONDS": 2,
    } == {name: getattr(module, name) for name in (
        "MAX_TASK_BYTES", "MAX_PROJECT_RESULT_BYTES", "MAX_STDOUT_BYTES",
        "MAX_STDERR_BYTES", "MAX_CAPTURE_BYTES", "MAX_TOWN_RESULT_BYTES",
        "MAX_JOURNAL_BYTES", "PROJECT_TIMEOUT_SECONDS", "TERMINATION_GRACE_SECONDS",
    )}
