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


def launch_count(runtime: Path) -> int:
    invocations = (runtime / "fixture-counter.txt").read_text(encoding="ascii").splitlines()
    assert invocations and all(value.isascii() and value.isdigit() for value in invocations)
    return len(invocations)


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


def deeply_nested_json() -> bytes:
    raw = b"[" * 10_000 + b"0" + b"]" * 10_000
    assert len(raw) < 65_536
    return raw


def deeply_nested_value():
    value = 0
    for _ in range(10_000):
        value = [value]
    return value


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
        assert launch_count(runtime) == expected


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
    assert launch_count(runtime) == 1

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
    assert launch_count(runtime) == 1


@pytest.mark.parametrize(
    "unsafe_text",
    (
        "/etc/passwd",
        "token=town-secret-marker",
        "see(/etc/passwd)",
        "file:/root/private",
        "password=hunter2",
        "api_key=disguised-secret",
        "private_key: disguised-secret",
        "plausible but non-frozen review text",
    ),
)
def test_completed_journal_rejects_non_frozen_review_assessment(tmp_path, unsafe_text):
    """Only the task runner's exact deterministic review can be replayed."""
    module = api()
    _, env, runtime = call(module, tmp_path)
    journal_path = Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    project = journal["result_body"]["project_result"]
    project["receipt"]["review"]["assessment"] = unsafe_text
    project["receipt_integrity_hash"] = digest(project["receipt"])
    journal["project_result_digest"] = digest(project)
    journal["result_body_digest"] = digest(journal["result_body"])
    journal_path.write_bytes(canonical(journal))
    expect_failure(module, "state_conflict", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert launch_count(runtime) == 1


@pytest.mark.parametrize(
    "concerns",
    (
        [],
        ["see(/etc/passwd)"],
        ["Synthetic metrics are not performance evidence.", "extra"],
        "Synthetic metrics are not performance evidence.",
    ),
    ids=("empty", "disguised", "extra", "wrong-type"),
)
def test_completed_journal_rejects_non_frozen_review_concerns(tmp_path, concerns):
    """Concern text and list shape are exact, not a free-text secret channel."""
    module = api()
    _, env, runtime = call(module, tmp_path)
    journal_path = Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    project = journal["result_body"]["project_result"]
    project["receipt"]["review"]["concerns"] = concerns
    project["receipt_integrity_hash"] = digest(project["receipt"])
    journal["project_result_digest"] = digest(project)
    journal["result_body_digest"] = digest(journal["result_body"])
    journal_path.write_bytes(canonical(journal))
    expect_failure(module, "state_conflict", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert launch_count(runtime) == 1


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


def test_only_registered_request_identity_is_accepted(tmp_path):
    """A different printable Town identity must fail before any state exists."""
    module = api()
    env, runtime = configured(tmp_path)
    expect_failure(module, "invalid_request", lambda: module.process_request(
        request_id="research-2", request_body=canonical(request()), environment=env,
    ))
    assert not (runtime / "fixture-counter.txt").exists()
    assert not (Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").exists()


def test_deeply_nested_raw_request_fails_invalid_without_launch(tmp_path):
    module = api()
    env, runtime = configured(tmp_path)

    expect_failure(module, "invalid_request", lambda: module.process_request(
        request_id="research-1",
        request_body=deeply_nested_json(),
        environment=env,
    ))

    assert not (runtime / "fixture-counter.txt").exists()
    assert not (Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").exists()


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
    ("missing-root", "root-is-file", "missing-executable",
     "executable-outside-root", "non-executable"),
)
def test_named_invalid_roots_and_entrypoints_fail_before_state(tmp_path, damage):
    """Every frozen root/entry-point error must reject before state or launch."""
    module = api()
    env, runtime = configured(tmp_path)
    root = Path(env["SHARPESHOOTER_ROOT"])
    executable = Path(env["SHARPESHOOTER_TASK_EXECUTABLE"])
    if damage == "missing-root":
        env["SHARPESHOOTER_ROOT"] = str(tmp_path / "absent-root")
    elif damage == "root-is-file":
        root_file = tmp_path / "root-file"
        root_file.write_text("not a directory", encoding="ascii")
        env["SHARPESHOOTER_ROOT"] = str(root_file)
    elif damage == "missing-executable":
        env["SHARPESHOOTER_TASK_EXECUTABLE"] = str(root / "absent")
    elif damage == "executable-outside-root":
        outside = tmp_path / "outside-executable"
        outside.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        outside.chmod(0o700)
        env["SHARPESHOOTER_TASK_EXECUTABLE"] = str(outside)
    else:
        executable.chmod(0o600)
    expect_failure(module, "invalid_configuration", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert not (runtime / "fixture-counter.txt").exists()
    assert not (Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").exists()


def test_metacharacters_in_owned_paths_remain_literal_argv_not_shell(tmp_path):
    """Shell metacharacters in trusted paths must never become commands."""
    module = api()
    owned = tmp_path / "space ; dollar $(not-a-command)"
    outcome, env, runtime = call(module, owned)
    observation = json.loads((runtime / "fixture-observation.json").read_text())
    assert outcome["replayed"] is False
    assert observation["argv"][4] == env["SHARPESHOOTER_DB"]
    assert observation["argv"][6] == env["SHARPESHOOTER_CAPTURE"]
    assert not (tmp_path / "not-a-command").exists()


def test_ambient_provider_and_auth_environment_never_reaches_child(tmp_path):
    """The child environment is an allowlist, not ambient inheritance."""
    module = api()
    _outcome, env, runtime = call(module, tmp_path)
    observed = json.loads((runtime / "fixture-observation.json").read_text())["env"]
    for key in ("HOME", "CODEX_HOME", "HTTPS_PROXY", "OPENAI_API_KEY",
                "UNRELATED", "TOKEN", "TOWN_URL"):
        assert key in env and key not in observed


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
        ("non-object", "project_output_invalid"),
        ("stdout-oversize", "project_output_too_large"),
        ("stderr-oversize", "project_output_too_large"),
        ("both-oversize", "project_output_too_large"),
        ("capture-oversize", "project_output_too_large"),
        ("unsafe-capture", "project_output_invalid"),
        ("mismatch", "project_output_invalid"),
        ("bad-receipt", "project_output_invalid"),
        ("receipt-unknown-nested", "project_output_invalid"),
        ("receipt-wrong-type", "project_output_invalid"),
        ("receipt-spec-mismatch", "project_output_invalid"),
        ("unsafe-receipt-path", "project_output_invalid"),
        ("unsafe-receipt-secret", "project_output_invalid"),
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
    launches = launch_count(Path(env["SHARPESHOOTER_DB"]).parent)
    expect_failure(module, "interrupted", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert launch_count(Path(env["SHARPESHOOTER_DB"]).parent) == launches
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


def test_deeply_nested_project_output_fails_invalid_and_leaves_started(tmp_path):
    module = api()
    env, runtime = configured(tmp_path, "deep-json")

    expect_failure(module, "project_output_invalid", lambda: module.process_request(
        request_id="research-1",
        request_body=canonical(request()),
        environment=env,
    ))

    journal = json.loads(
        (Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").read_text()
    )
    assert journal["state"] == "started"
    assert launch_count(runtime) == 1


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
    "mode,error_code",
    (("descendant-normal", None), ("descendant-failure", "project_failed"), ("timeout", "project_timeout")),
)
def test_every_outcome_kills_stubborn_process_group_descendants(
    tmp_path, monkeypatch, mode, error_code,
):
    """Leader exit or SIGTERM resistance must not leave group members alive."""
    module = api()
    monkeypatch.setattr(module, "TERMINATION_GRACE_SECONDS", 0.1)
    if mode == "timeout":
        monkeypatch.setattr(module, "PROJECT_TIMEOUT_SECONDS", 0.5)
    env, runtime = configured(tmp_path, mode)
    if error_code is None:
        outcome = module.process_request(
            request_id="research-1", request_body=canonical(request()), environment=env,
        )
        assert outcome["replayed"] is False
    else:
        expect_failure(module, error_code, lambda: module.process_request(
            request_id="research-1", request_body=canonical(request()), environment=env,
        ))
    assert launch_count(runtime) == 1
    pid = int((runtime / "fixture-descendant.txt").read_text(encoding="ascii"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize(
    "journal_bytes",
    [
        b"{not-json",
        b"{}",
        b"x" * 131_073,
        canonical({
            "version": 1,
            "state": "started",
            "request_id": "research-1",
            "request_digest": digest(request()),
            "task_digest": TASK_DIGEST,
            "started_at": "2026-02-30T25:61:61Z",
        }),
    ],
    ids=("malformed", "unknown-shape", "oversized", "impossible-timestamp"),
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


def test_deeply_nested_existing_journal_fails_state_conflict_without_launch(
    tmp_path,
):
    module = api()
    env, runtime = configured(tmp_path)
    path = Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json"
    path.write_bytes(deeply_nested_json())

    expect_failure(module, "state_conflict", lambda: module.process_request(
        request_id="research-1",
        request_body=canonical(request()),
        environment=env,
    ))

    assert not (runtime / "fixture-counter.txt").exists()


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.update(state="unknown"),
        lambda value: value.update(version=2),
        lambda value: value.update(unexpected=True),
    ),
    ids=("unknown-state", "unknown-version", "extra-field"),
)
def test_named_unknown_journal_discriminators_fail_closed(tmp_path, mutation):
    """Unknown state, version, or field must never be treated as resumable."""
    module = api()
    env, runtime = configured(tmp_path)
    journal = {
        "version": 1, "state": "started", "request_id": "research-1",
        "request_digest": digest(request()), "task_digest": TASK_DIGEST,
        "started_at": "2026-11-09T00:00:00Z",
    }
    mutation(journal)
    (Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").write_bytes(
        canonical(journal))
    expect_failure(module, "state_conflict", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert not (runtime / "fixture-counter.txt").exists()


@pytest.mark.parametrize(
    "operation", ("temporary-open", "temporary-write", "temporary-fsync"),
)
def test_started_journal_temporary_io_failure_never_launches(
    tmp_path, monkeypatch, operation,
):
    """Failure before the durable started claim must leave no runnable state."""
    module = api()
    env, runtime = configured(tmp_path)

    def fail_io(*_args, **_kwargs):
        raise OSError(f"simulated {operation} failure")

    if operation == "temporary-open":
        original_open = module.os.open

        def fail_temp_open(path, *args, **kwargs):
            if isinstance(path, str) and path.startswith(
                    f".{module.JOURNAL_NAME}.tmp-"):
                raise OSError("simulated temporary open failure")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(module.os, "open", fail_temp_open)
    elif operation == "temporary-write":
        monkeypatch.setattr(module.os, "write", fail_io)
    else:
        monkeypatch.setattr(module.os, "fsync", fail_io)
    expect_failure(module, "state_conflict", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    state = Path(env["STATE_DIR"])
    assert not (state / "sharpeshooter-journal-v1.json").exists()
    assert not list(state.glob(".sharpeshooter-journal-v1.json.tmp-*"))
    assert not (runtime / "fixture-counter.txt").exists()


def test_completed_journal_replace_failure_leaves_started_and_never_relaunches(
    tmp_path, monkeypatch,
):
    """A failed completion write must stay ambiguous and fail closed."""
    module = api()
    env, runtime = configured(tmp_path)
    original_replace = module.os.replace
    replacements = 0

    def fail_completion_replace(source, destination, *args, **kwargs):
        nonlocal replacements
        replacements += 1
        if replacements == 1:
            raise OSError("simulated completion write failure")
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "replace", fail_completion_replace)
    expect_failure(module, "internal_error", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    monkeypatch.setattr(module.os, "replace", original_replace)
    journal = json.loads(
        (Path(env["STATE_DIR"]) / "sharpeshooter-journal-v1.json").read_text())
    assert journal["state"] == "started"
    assert launch_count(runtime) == 1
    expect_failure(module, "interrupted", lambda: module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    ))
    assert launch_count(runtime) == 1


def test_mailbox_rejects_unknown_message_kind_without_project_launch(
    tmp_path, monkeypatch,
):
    """A non-research Town message must be acknowledged rejected without work."""
    module = api()
    env, runtime = configured(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    observed = {}

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def join_auto(self, *_args, **_kwargs):
            pass

    def run_loop(_client, handler, *, until, on_ack_accepted):
        status, note, replies = handler({
            "kind": "quote_request", "message_id": "research-1",
            "body": request(), "from": "buyer",
        })
        observed.update(status=status, note=note, replies=replies)
        on_ack_accepted({}, note)
        assert until() is True

    import nandatown.client
    import nandatown.participants.base
    monkeypatch.setattr(nandatown.client, "TownClient", Client)
    monkeypatch.setattr(nandatown.participants.base, "run_loop", run_loop)
    assert module.main() == 4
    assert observed == {
        "status": "rejected", "note": {"reason": "unknown_kind"},
        "replies": [],
    }
    assert not (runtime / "fixture-counter.txt").exists()


def test_mailbox_rejects_deeply_nested_body_without_project_launch(
    tmp_path, monkeypatch,
):
    module = api()
    env, runtime = configured(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    observed = {}

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def join_auto(self, *_args, **_kwargs):
            pass

    def run_loop(_client, handler, *, until, on_ack_accepted):
        status, note, replies = handler({
            "kind": "research_request",
            "message_id": "research-1",
            "body": deeply_nested_value(),
            "from": "buyer",
        })
        observed.update(status=status, note=note, replies=replies)
        on_ack_accepted({}, note)
        assert until() is True

    import nandatown.client
    import nandatown.participants.base
    monkeypatch.setattr(nandatown.client, "TownClient", Client)
    monkeypatch.setattr(nandatown.participants.base, "run_loop", run_loop)

    assert module.main() == 4
    assert observed == {
        "status": "rejected", "note": {"reason": "invalid_request"},
        "replies": [],
    }
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


def test_journal_replace_stays_anchored_when_state_path_is_swapped(tmp_path, monkeypatch):
    """A parent rename/symlink swap must not redirect the completed journal."""
    module = api()
    env, runtime = configured(tmp_path)
    state = Path(env["STATE_DIR"])
    moved_state = tmp_path / "validated-state"
    outside = tmp_path / "outside-state"
    outside.mkdir(mode=0o700)
    original_replace = module.os.replace
    swapped = False

    def swap_then_replace(source, destination, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            state.rename(moved_state)
            state.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "replace", swap_then_replace)
    outcome = module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    )
    assert outcome["replayed"] is False
    assert swapped is True
    assert not (outside / "sharpeshooter-journal-v1.json").exists()
    journal = json.loads((moved_state / "sharpeshooter-journal-v1.json").read_text())
    assert journal["state"] == "completed"
    assert launch_count(runtime) == 1


def test_tmpdir_and_child_started_visibility_stay_anchored_after_parent_swap(
    tmp_path, monkeypatch,
):
    """A post-claim pathname swap must not redirect TMPDIR or split state roots."""
    module = api()
    env, runtime = configured(tmp_path)
    state = Path(env["STATE_DIR"])
    moved_state = tmp_path / "validated-state"
    outside = tmp_path / "outside-state"
    outside.mkdir(mode=0o700)
    original_claim = module._claim_started
    swapped = False

    def claim_then_swap(directory_descriptor, data):
        nonlocal swapped
        claimed = original_claim(directory_descriptor, data)
        assert claimed is True
        state.rename(moved_state)
        state.symlink_to(outside, target_is_directory=True)
        swapped = True
        return claimed

    monkeypatch.setattr(module, "_claim_started", claim_then_swap)
    outcome = module.process_request(
        request_id="research-1", request_body=canonical(request()), environment=env,
    )

    assert outcome["replayed"] is False
    assert swapped is True
    observation = json.loads((runtime / "fixture-observation.json").read_text())
    assert observation["started_visible"] is True
    assert observation["env"]["TMPDIR"] == str(moved_state / "tmp")
    assert (moved_state / "tmp").is_dir()
    assert json.loads((moved_state / "sharpeshooter-journal-v1.json").read_text())["state"] == "completed"
    assert not (outside / "tmp").exists()
    assert not (outside / "sharpeshooter-journal-v1.json").exists()
    assert launch_count(runtime) == 1


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
    assert launch_count(runtime) == 1


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
    assert launch_count(runtime) == 1
    assert len(outcomes) == 2
    fresh = [value for value in outcomes if value.get("replayed") is False]
    assert len(fresh) == 1 and fresh[0]["message_id"] == "research-1-result"
    other = next(value for value in outcomes if value is not fresh[0])
    assert (
        other.get("message_id") == "research-1-result" and other.get("replayed") is True
    ) or other.get("code") in {"interrupted", "state_conflict"}


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
