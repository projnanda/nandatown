#!/usr/bin/env python3
"""Bounded subprocess and replay core for the SharpeShooter Track adapter.

Milestone 1 intentionally exposes only the byte-oriented core.  The Town
mailbox loop is added with the native research profile in Milestone 2.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
import time
from typing import Any, Mapping


MAX_TASK_BYTES = 65_536
MAX_PROJECT_RESULT_BYTES = 65_536
MAX_RECEIPT_BYTES = 32_768
MAX_STDOUT_BYTES = 131_072
MAX_STDERR_BYTES = 16_384
MAX_CAPTURE_BYTES = 32_768
MAX_TOWN_RESULT_BYTES = 98_304
MAX_JOURNAL_BYTES = 131_072
PROJECT_TIMEOUT_SECONDS = 30
TERMINATION_GRACE_SECONDS = 2

REGISTERED_TASK_DIGEST = (
    "sha256:cb03de74c7a8e08414d4d30cd84ad5ed1ef0b73bef3cf980095615e20d480a9a"
)
JOURNAL_NAME = "sharpeshooter-journal-v1.json"
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
SLUG = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
RFC3339_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
PRIVATE_PATH = re.compile(r"/(?:Users|home|private|tmp|var)/", re.IGNORECASE)
SECRET_TEXT = re.compile(
    r"(?:ss_[0-9a-f]{12}_[A-Za-z0-9_-]{32,}|sk-[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16}|\bBearer\s+[A-Za-z0-9._~+/-]+=*|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    re.IGNORECASE,
)

LIMITATIONS = [
    "The task is bounded and structured; it is not unrestricted prose research.",
    "Task content controls provenance and tested specifications, but the deterministic evaluator does not understand the economic thesis.",
    "Synthetic metrics do not validate strategy performance.",
    "Town proves transport and attributed requester checks; SharpeShooter proves its workflow/receipt; cross-system evidence correlates them.",
    "A receipt hash is tamper-evidence, not an independently signed truth attestation.",
    "The run does not prove real-agent reasoning, A2A/MCP conformance, hostile-code sandboxing, production readiness, or arbitrary interoperability.",
    "A `started` journal fails closed after interruption; v1 does not promise automatic project-level resumption.",
]

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

TOWN_INPUTS = ("TOWN_URL", "RUN_ID", "NAME", "TOKEN", "STATE_DIR", "DEADLINE")
OPERATOR_INPUTS = (
    "SHARPESHOOTER_ROOT",
    "SHARPESHOOTER_TASK_EXECUTABLE",
    "SHARPESHOOTER_APPROVED_TASK_DIGEST",
    "SHARPESHOOTER_DB",
    "SHARPESHOOTER_CAPTURE",
)

PROJECT_FIELDS = {
    "schema_version", "client_task_id", "task_digest", "title", "hypothesis",
    "sources_digest", "agent_runtime", "idea_id", "family_id",
    "candidate_results", "selected_candidate_id", "selected_is_strategy_hash",
    "oos_run_id", "oos_strategy_hash", "family_oos_trial_number",
    "budget_before", "budget_after", "author_agent_id", "reviewer_agent_id",
    "review_id", "review_recommendation", "receipt_id", "receipt",
    "receipt_integrity_hash", "receipt_verification",
    "receipt_consumed_by_scout", "operator_approval", "operator_verdict",
    "synthetic_metrics", "limitations",
}
CANDIDATE_FIELDS = {"candidate_id", "is_run_id", "strategy_hash"}
RECEIPT_FIELDS = {
    "schema_version", "receipt_id", "issued_at", "idea", "strategy",
    "evaluation", "review", "provenance",
}
STARTED_FIELDS = {
    "version", "state", "request_id", "request_digest", "task_digest",
    "started_at",
}
COMPLETED_FIELDS = STARTED_FIELDS | {
    "result_message_id", "result_body", "result_body_digest",
    "project_result_digest", "capture_digest", "replay_facts",
}


class AdapterFailure(RuntimeError):
    """A fixed, redacted adapter failure safe to project to Town."""

    def __init__(self, code: str):
        message = ERRORS[code]
        self.payload = {
            "schema_version": "sharpeshooter.adapter-error.v1",
            "code": code,
            "message": message,
        }
        super().__init__(message)


def _fail(code: str) -> None:
    raise AdapterFailure(code)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    data = value if isinstance(value, bytes) else _canonical(value)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_json(raw: bytes, limit: int) -> Any:
    if not isinstance(raw, bytes) or len(raw) > limit or raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("invalid JSON")
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder(
        object_pairs_hook=_pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite")),
    )
    value, end = decoder.raw_decode(text)
    if text[end:].strip():
        raise ValueError("trailing JSON")
    return value


def _absolute_without_symlinks(path: Path, *, require_exists: bool) -> Path:
    if not path.is_absolute():
        raise ValueError("path is not absolute")
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            details = current.lstat()
        except FileNotFoundError:
            if require_exists:
                raise ValueError("path does not exist") from None
            break
        if stat.S_ISLNK(details.st_mode):
            raise ValueError("path contains a symlink")
    return absolute


def _owned_directory(path: Path) -> None:
    details = path.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_mode & 0o022
    ):
        raise ValueError("directory is unsafe")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class _Config:
    root: Path
    executable: Path
    approved_digest: str
    db: Path
    capture: Path
    runtime: Path
    state: Path


def _configuration(environment: Mapping[str, str]) -> _Config:
    try:
        if any(not isinstance(environment[name], str) or not environment[name] for name in (*TOWN_INPUTS, *OPERATOR_INPUTS)):
            raise ValueError("missing configuration")
        root = _absolute_without_symlinks(Path(environment["SHARPESHOOTER_ROOT"]), require_exists=True)
        executable = _absolute_without_symlinks(
            Path(environment["SHARPESHOOTER_TASK_EXECUTABLE"]), require_exists=True
        )
        db = _absolute_without_symlinks(Path(environment["SHARPESHOOTER_DB"]), require_exists=False)
        capture = _absolute_without_symlinks(
            Path(environment["SHARPESHOOTER_CAPTURE"]), require_exists=False
        )
        state = _absolute_without_symlinks(Path(environment["STATE_DIR"]), require_exists=True)
        _owned_directory(root)
        _owned_directory(state)
        executable_details = executable.lstat()
        if (
            not _inside(executable, root)
            or not stat.S_ISREG(executable_details.st_mode)
            or executable_details.st_uid != os.geteuid()
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError("executable is unsafe")
        if db.name != "sharpeshooter.sqlite3" or capture.name != "capture.txt":
            raise ValueError("runtime leaves are invalid")
        runtime = _absolute_without_symlinks(db.parent, require_exists=True)
        capture_parent = _absolute_without_symlinks(capture.parent, require_exists=True)
        if runtime != capture_parent:
            raise ValueError("runtime parents differ")
        _owned_directory(runtime)
        if root == runtime or root in runtime.parents or runtime in root.parents:
            raise ValueError("runtime overlaps project")
        return _Config(
            root=root,
            executable=executable,
            approved_digest=environment["SHARPESHOOTER_APPROVED_TASK_DIGEST"],
            db=db,
            capture=capture,
            runtime=runtime,
            state=state,
        )
    except (KeyError, OSError, ValueError, TypeError):
        _fail("invalid_configuration")


def _validate_request(request_id: str, raw: bytes, approved: str) -> tuple[dict, bytes, str, str]:
    try:
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 120
            or not request_id.isascii()
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in request_id)
        ):
            raise ValueError("invalid request id")
        request = _parse_json(raw, MAX_TASK_BYTES)
        if (
            not isinstance(request, dict)
            or set(request) != {"schema_version", "task", "task_digest"}
            or request["schema_version"] != "sharpeshooter.research-request.v1"
            or not isinstance(request["task"], dict)
            or not isinstance(request["task_digest"], str)
        ):
            raise ValueError("invalid request")
        task_bytes = _canonical(request["task"])
        if raw != _canonical(request) or len(task_bytes) > MAX_TASK_BYTES:
            raise ValueError("oversized task")
        computed = _digest(task_bytes)
    except (UnicodeError, ValueError, TypeError, OverflowError):
        _fail("invalid_request")
    if request["task_digest"] != computed or approved != computed or computed != REGISTERED_TASK_DIGEST:
        _fail("task_digest_mismatch")
    return request, task_bytes, computed, _digest(request)


def _fresh_runtime(config: _Config) -> None:
    try:
        for path in (
            config.db,
            Path(os.fspath(config.db) + "-wal"),
            Path(os.fspath(config.db) + "-shm"),
            config.capture,
        ):
            if path.exists() or path.is_symlink():
                raise ValueError("runtime is not fresh")
    except (OSError, ValueError):
        _fail("invalid_configuration")


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _temp_path(journal: Path) -> Path:
    return journal.with_name(
        f".{journal.name}.tmp-{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns()}"
    )


def _write_temp(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _claim_started(journal: Path, data: bytes) -> bool:
    temporary = _temp_path(journal)
    try:
        _write_temp(temporary, data)
        try:
            os.link(temporary, journal, follow_symlinks=False)
        except FileExistsError:
            return False
        _fsync_directory(journal.parent)
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _replace_completed(journal: Path, data: bytes) -> None:
    temporary = _temp_path(journal)
    try:
        _write_temp(temporary, data)
        os.replace(temporary, journal)
        _fsync_directory(journal.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _journal_value(path: Path) -> dict | None:
    try:
        if path.is_symlink():
            raise ValueError("journal is a symlink")
        if not path.exists():
            return None
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_size > MAX_JOURNAL_BYTES:
            raise ValueError("journal is unsafe")
        with path.open("rb") as source:
            raw = source.read(MAX_JOURNAL_BYTES + 1)
        value = _parse_json(raw, MAX_JOURNAL_BYTES)
        if raw != _canonical(value) or not isinstance(value, dict):
            raise ValueError("journal is not canonical")
        return value
    except (OSError, UnicodeError, ValueError, TypeError):
        _fail("state_conflict")


def _strict_string(value: Any, pattern: re.Pattern | None = None) -> bool:
    return isinstance(value, str) and (pattern is None or pattern.fullmatch(value) is not None)


def _validate_candidate_results(value: Any, task: dict) -> list[dict]:
    if not isinstance(value, list) or len(value) != len(task.get("candidates", [])):
        raise ValueError("candidate results differ")
    expected_ids = [candidate.get("candidate_id") for candidate in task["candidates"]]
    for index, item in enumerate(value):
        if (
            not isinstance(item, dict)
            or set(item) != CANDIDATE_FIELDS
            or item["candidate_id"] != expected_ids[index]
            or not _strict_string(item["candidate_id"], SLUG)
            or not _strict_string(item["is_run_id"], IDENTIFIER)
            or not _strict_string(item["strategy_hash"], SHA256)
            or item["strategy_hash"] != _digest(task["candidates"][index]["strategy_spec"])
        ):
            raise ValueError("candidate result is invalid")
    return value


def _walk_safe(value: Any, key: str = "") -> None:
    denied_keys = {
        "token", "access_token", "refresh_token", "secret", "credential",
        "credentials", "authorization", "path", "db", "database", "db_path",
        "database_path", "capture", "capture_path", "project_root", "state_dir",
        "journal", "executable",
        "command", "cwd", "env", "environment", "stdout", "stderr", "log",
        "logs", "claim_token",
    }
    if isinstance(value, dict):
        for child_key, child in value.items():
            if not isinstance(child_key, str) or child_key.lower() in denied_keys:
                raise ValueError("unsafe result field")
            _walk_safe(child, child_key)
    elif isinstance(value, list):
        for child in value:
            _walk_safe(child, key)
    elif isinstance(value, str):
        if SECRET_TEXT.search(value) or PRIVATE_PATH.search(value):
            raise ValueError("unsafe result text")
    elif value is None or type(value) in (bool, int, float):
        if isinstance(value, float) and not (float("-inf") < value < float("inf")):
            raise ValueError("nonfinite result")
    else:
        raise ValueError("unsafe result type")


def _validate_project_result(value: Any, task: dict, task_digest: str, raw: bytes) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != PROJECT_FIELDS
        or len(raw) > MAX_PROJECT_RESULT_BYTES
        or value["schema_version"] != "sharpeshooter.research-result.v1"
        or value["client_task_id"] != task.get("client_task_id")
        or value["task_digest"] != task_digest
        or value["title"] != task.get("idea", {}).get("title")
        or value["hypothesis"] != task.get("idea", {}).get("hypothesis")
        or value["sources_digest"] != _digest(task.get("idea", {}).get("sources"))
        or value["agent_runtime"] != "fake"
        or not _strict_string(value["idea_id"], IDENTIFIER)
        or not _strict_string(value["family_id"], IDENTIFIER)
    ):
        raise ValueError("project result is invalid")
    _validate_candidate_results(value["candidate_results"], task)
    selected_id = task.get("selected_candidate_id")
    selected = next(
        (candidate for candidate in task.get("candidates", []) if candidate.get("candidate_id") == selected_id),
        None,
    )
    selected_hash = _digest(selected["strategy_spec"]) if selected else ""
    if (
        value["selected_candidate_id"] != selected_id
        or value["selected_is_strategy_hash"] != selected_hash
        or value["oos_strategy_hash"] != selected_hash
        or not _strict_string(value["oos_run_id"], IDENTIFIER)
        or type(value["family_oos_trial_number"]) is not int
        or value["family_oos_trial_number"] != 1
        or type(value["budget_before"]) is not int
        or value["budget_before"] != 0
        or type(value["budget_after"]) is not int
        or value["budget_after"] != 1
        or not _strict_string(value["author_agent_id"], IDENTIFIER)
        or not _strict_string(value["reviewer_agent_id"], IDENTIFIER)
        or value["author_agent_id"] == value["reviewer_agent_id"]
        or not _strict_string(value["review_id"], IDENTIFIER)
        or value["review_recommendation"] != "accept"
        or not _strict_string(value["receipt_id"], IDENTIFIER)
        or value["receipt_verification"] != "VERIFIED"
        or value["receipt_consumed_by_scout"] is not True
        or value["operator_approval"] != "approved"
        or value["operator_verdict"] != "accept"
        or value["synthetic_metrics"] is not True
        or value["limitations"] != LIMITATIONS
    ):
        raise ValueError("project controls are invalid")
    receipt = value["receipt"]
    if (
        not isinstance(receipt, dict)
        or set(receipt) != RECEIPT_FIELDS
        or receipt.get("receipt_id") != value["receipt_id"]
        or receipt.get("schema_version") != "1.0"
        or not _strict_string(value["receipt_integrity_hash"], SHA256)
    ):
        raise ValueError("receipt is invalid")
    idea = receipt.get("idea")
    strategy = receipt.get("strategy")
    evaluation = receipt.get("evaluation")
    review = receipt.get("review")
    if (
        not isinstance(idea, dict)
        or idea.get("idea_id") != value["idea_id"]
        or idea.get("family_id") != value["family_id"]
        or idea.get("author_agent_id") != value["author_agent_id"]
        or not isinstance(strategy, dict)
        or strategy.get("strategy_hash") != value["selected_is_strategy_hash"]
        or not isinstance(evaluation, dict)
        or evaluation.get("run_id") != value["oos_run_id"]
        or evaluation.get("split") != "oos"
        or type(evaluation.get("family_oos_trial_number")) is not int
        or evaluation.get("family_oos_trial_number") != 1
        or evaluation.get("synthetic_metrics") is not True
        or not isinstance(review, dict)
        or review.get("review_id") != value["review_id"]
        or review.get("reviewer_agent_id") != value["reviewer_agent_id"]
        or review.get("recommendation") != "accept"
    ):
        raise ValueError("receipt correlation is invalid")
    receipt_bytes = _canonical(receipt)
    if len(receipt_bytes) > MAX_RECEIPT_BYTES or _digest(receipt_bytes) != value["receipt_integrity_hash"]:
        raise ValueError("receipt integrity is invalid")
    _walk_safe(value)
    return value


def _validate_capture(path: Path, task_digest: str) -> bytes:
    try:
        if path.is_symlink() or not path.exists():
            raise ValueError("capture is absent")
        details = path.lstat()
        if details.st_size > MAX_CAPTURE_BYTES:
            _fail("project_output_too_large")
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid():
            raise ValueError("capture is unsafe")
        with path.open("rb") as source:
            raw = source.read(MAX_CAPTURE_BYTES + 1)
        if len(raw) > MAX_CAPTURE_BYTES:
            _fail("project_output_too_large")
        text = raw.decode("utf-8")
        if (
            "\x00" in text
            or SECRET_TEXT.search(text)
            or PRIVATE_PATH.search(text)
            or re.search(r"(?i)(?:token|credential|authorization|secret)\s*[:=]", text)
        ):
            raise ValueError("capture contains unsafe text")
        if any(ord(character) < 0x20 and character not in "\n\r\t" for character in text):
            raise ValueError("capture contains controls")
        return raw
    except AdapterFailure:
        raise
    except (OSError, UnicodeError, ValueError):
        _fail("project_output_invalid")


def _read_pipe(stream, limit: int, target: list[bytes], overflow: threading.Event) -> None:
    data = bytearray()
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > limit:
                overflow.set()
                break
    finally:
        target.append(bytes(data))
        try:
            stream.close()
        except OSError:
            pass


def _stop_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    process.wait()


def _run_child(config: _Config, task_bytes: bytes, tmpdir: Path) -> tuple[bytes, bytes]:
    argv = [
        os.fspath(config.executable),
        "run", "--task", "-",
        "--db", os.fspath(config.db),
        "--capture", os.fspath(config.capture),
        "--approved-task-digest", config.approved_digest,
        "--confirm-oos", "--verdict", "accept",
    ]
    child_environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": os.fspath(tmpdir),
    }
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=config.root,
            env=child_environment,
            shell=False,
            start_new_session=(os.name == "posix"),
            bufsize=0,
        )
    except OSError:
        _fail("project_failed")
    overflow = threading.Event()
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    stdout_reader = threading.Thread(
        target=_read_pipe, args=(process.stdout, MAX_STDOUT_BYTES, stdout_parts, overflow), daemon=True
    )
    stderr_reader = threading.Thread(
        target=_read_pipe, args=(process.stderr, MAX_STDERR_BYTES, stderr_parts, overflow), daemon=True
    )
    stdout_reader.start()
    stderr_reader.start()
    try:
        try:
            process.stdin.write(task_bytes)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        deadline = time.monotonic() + PROJECT_TIMEOUT_SECONDS
        timed_out = False
        while process.poll() is None:
            if overflow.is_set():
                _stop_group(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _stop_group(process)
                break
            time.sleep(0.01)
        process.wait()
    finally:
        if process.poll() is None:
            _stop_group(process)
        stdout_reader.join(timeout=TERMINATION_GRACE_SECONDS + 1)
        stderr_reader.join(timeout=TERMINATION_GRACE_SECONDS + 1)
    if timed_out:
        _fail("project_timeout")
    if overflow.is_set():
        _fail("project_output_too_large")
    stdout = stdout_parts[0] if stdout_parts else b""
    stderr = stderr_parts[0] if stderr_parts else b""
    if len(stdout) > MAX_STDOUT_BYTES or len(stderr) > MAX_STDERR_BYTES:
        _fail("project_output_too_large")
    if process.returncode != 0:
        _fail("project_failed")
    return stdout, stderr


def _existing_outcome(
    value: dict,
    *,
    request_id: str,
    request_digest: str,
    task_digest: str,
    task: dict,
) -> dict:
    fields = set(value)
    if fields not in (STARTED_FIELDS, COMPLETED_FIELDS):
        _fail("state_conflict")
    if (
        type(value.get("version")) is not int
        or value.get("version") != 1
        or value.get("request_id") != request_id
        or value.get("request_digest") != request_digest
        or value.get("task_digest") != task_digest
        or not _strict_string(value.get("started_at"), RFC3339_Z)
    ):
        _fail("state_conflict")
    if value.get("state") == "started" and fields == STARTED_FIELDS:
        _fail("interrupted")
    if value.get("state") != "completed" or fields != COMPLETED_FIELDS:
        _fail("state_conflict")
    try:
        message_id = value["result_message_id"]
        if (
            not isinstance(message_id, str)
            or message_id != f"{request_id}-result"
            or not 1 <= len(message_id) <= 128
            or not message_id.isascii()
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in message_id)
        ):
            raise ValueError("invalid result message id")
        body = value["result_body"]
        _validate_town_result(body, request_id, request_digest, task, task_digest)
        if (
            value["result_body_digest"] != _digest(body)
            or value["project_result_digest"] != _digest(body["project_result"])
            or not _strict_string(value["capture_digest"], SHA256)
            or value["replay_facts"] != {"project_invocations": 1, "response_count": 1}
        ):
            raise ValueError("invalid completed journal")
    except (KeyError, TypeError, ValueError):
        _fail("state_conflict")
    return {"message_id": message_id, "body": body, "replayed": True}


def _validate_town_result(
    body: Any,
    request_id: str,
    request_digest: str,
    task: dict,
    task_digest: str,
) -> dict:
    if (
        not isinstance(body, dict)
        or set(body) != {"schema_version", "request_id", "request_digest", "project_result"}
        or body["schema_version"] != "sharpeshooter.town-research-result.v1"
        or body["request_id"] != request_id
        or body["request_digest"] != request_digest
    ):
        raise ValueError("Town result is invalid")
    projected = _canonical(body)
    if len(projected) > MAX_TOWN_RESULT_BYTES:
        raise ValueError("Town result is oversized")
    _validate_project_result(
        body["project_result"], task, task_digest, _canonical(body["project_result"])
    )
    return body


def process_request(
    *,
    request_id: str,
    request_body: bytes,
    environment: Mapping[str, str],
) -> dict:
    """Run or replay one validated logical research request."""
    config = _configuration(environment)
    request, task_bytes, task_digest, request_digest = _validate_request(
        request_id, request_body, config.approved_digest
    )
    journal_path = config.state / JOURNAL_NAME
    existing = _journal_value(journal_path)
    if existing is not None:
        return _existing_outcome(
            existing,
            request_id=request_id,
            request_digest=request_digest,
            task_digest=task_digest,
            task=request["task"],
        )
    _fresh_runtime(config)
    started = {
        "version": 1,
        "state": "started",
        "request_id": request_id,
        "request_digest": request_digest,
        "task_digest": task_digest,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    try:
        claimed = _claim_started(journal_path, _canonical(started))
    except (OSError, ValueError):
        _fail("state_conflict")
    if not claimed:
        raced = _journal_value(journal_path)
        if raced is None:
            _fail("state_conflict")
        return _existing_outcome(
            raced,
            request_id=request_id,
            request_digest=request_digest,
            task_digest=task_digest,
            task=request["task"],
        )
    tmpdir = config.state / "tmp"
    try:
        if tmpdir.exists() or tmpdir.is_symlink():
            _absolute_without_symlinks(tmpdir, require_exists=True)
            _owned_directory(tmpdir)
        else:
            tmpdir.mkdir(mode=0o700)
            _owned_directory(tmpdir)
    except (OSError, ValueError):
        _fail("internal_error")
    stdout, _stderr = _run_child(config, task_bytes, tmpdir)
    try:
        project_raw = stdout[:-1] if stdout.endswith(b"\n") else stdout
        if len(project_raw) > MAX_PROJECT_RESULT_BYTES:
            _fail("project_output_too_large")
        project = _parse_json(project_raw, MAX_PROJECT_RESULT_BYTES)
        if stdout not in (project_raw, project_raw + b"\n") or project_raw != _canonical(project):
            raise ValueError("project output is not canonical")
        if (
            isinstance(project, dict)
            and "receipt" in project
            and len(_canonical(project["receipt"])) > MAX_RECEIPT_BYTES
        ):
            _fail("project_output_too_large")
        _validate_project_result(project, request["task"], task_digest, project_raw)
    except AdapterFailure:
        raise
    except (UnicodeError, ValueError, TypeError, KeyError, OverflowError):
        _fail("project_output_invalid")
    capture = _validate_capture(config.capture, task_digest)
    body = {
        "schema_version": "sharpeshooter.town-research-result.v1",
        "request_id": request_id,
        "request_digest": request_digest,
        "project_result": project,
    }
    try:
        _validate_town_result(body, request_id, request_digest, request["task"], task_digest)
        message_id = f"{request_id}-result"
        completed = {
            **started,
            "state": "completed",
            "result_message_id": message_id,
            "result_body": body,
            "result_body_digest": _digest(body),
            "project_result_digest": _digest(project),
            "capture_digest": _digest(capture),
            "replay_facts": {"project_invocations": 1, "response_count": 1},
        }
        completed_bytes = _canonical(completed)
        if len(completed_bytes) > MAX_JOURNAL_BYTES:
            _fail("project_output_too_large")
        _replace_completed(journal_path, completed_bytes)
    except AdapterFailure:
        raise
    except (OSError, ValueError, TypeError):
        _fail("internal_error")
    return {"message_id": message_id, "body": body, "replayed": False}


if __name__ == "__main__":
    raise SystemExit("Milestone 2 adds the Town mailbox loop; import process_request for now.")
