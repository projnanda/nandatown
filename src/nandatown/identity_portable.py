"""Portable identity: controller keys, the town registry, run grants.

The design from the doc, built: a long-lived controller key that never
becomes a model tool and never enters a participant's environment; a
registry mapping a portable agent id to its controller's public key
(the local file registry is the town's testnet registry; resolvers are
pluggable, including an eth_call resolver whose contract and selector
are configuration); and a Run Grant, a signed, time-limited permission
letting one disposable session key act in one run with named
permissions. The agent-facing experience stays the same while the
source of authority becomes portable.
"""

from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import re
import secrets
import stat
import time
import warnings
from collections.abc import Iterator
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .records import canonical_json, fingerprint

DEFAULT_PERMISSIONS = ["join", "claim", "send", "ack"]
GRANT_TTL_SECONDS = 3600.0
OPERATOR_NAME = "town-operator"


def default_keystore_dir() -> str:
    home = os.environ.get("NANDATOWN_HOME",
                          os.path.expanduser("~/.nandatown"))
    return os.path.join(home, "identity")


class IdentityError(Exception):
    pass


class _UnwrittenKey(IdentityError):
    """A key file that is empty: claimed, and not yet written."""


def _finite_timestamp(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IdentityError(f"{label} must be a timestamp")
    try:
        timestamp = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise IdentityError(f"{label} must be a timestamp") from exc
    if not math.isfinite(timestamp):
        raise IdentityError(f"{label} must be a timestamp")
    return timestamp


def _sign(private_hex: str, payload: Any) -> str:
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return key.sign(canonical_json(payload).encode()).hex()


def verify_signature(public_hex: str, payload: Any,
                     signature_hex: str) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        key.verify(bytes.fromhex(signature_hex),
                   canonical_json(payload).encode())
        return True
    except (InvalidSignature, ValueError):
        return False


# A file Town stages, and nothing else: a controller key is always named
# "<name>.controller.key", so no identity's key can match.
_STAGED = re.compile(r"\.nandatown-staged-[0-9a-f]{32}\.tmp")
# A staged file this old was left by a writer that died: staging and
# publishing take moments, and the margin allows for a file server's clock.
_STAGED_ABANDONED_SECONDS = 600.0
# How long a reader waits for a key another writer has claimed to be written.
_KEY_WAIT_SECONDS = 5.0


@contextlib.contextmanager
def _directory_lock(directory: str) -> Iterator[None]:
    """An exclusive lock on directory, held through its .lock file.

    Taking it also removes staged files that writers which died left
    behind: they can hold a private key.
    """
    fd = os.open(os.path.join(directory, ".lock"),
                 os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError as exc:
                    # LK_LOCK gives up after ten seconds; keep waiting.
                    if exc.errno not in (errno.EDEADLK, errno.EACCES):
                        raise
            try:
                _remove_staged(directory)
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                if exc.errno not in (errno.ENOLCK, errno.ENOTSUP,
                                     errno.EOPNOTSUPP):
                    raise
                warnings.warn(
                    f"{directory} cannot be locked ({exc}): a controller key"
                    " is still never replaced, but runs that create"
                    " identities in it at the same time can lose each"
                    " other's registry entries",
                    RuntimeWarning, stacklevel=5)
            else:
                _remove_staged(directory)
            yield
    finally:
        os.close(fd)


def _staged_name() -> str:
    return f".nandatown-staged-{secrets.token_hex(16)}.tmp"


def _remove_staged(directory: str) -> None:
    """Remove the abandoned files Town staged in directory, and only those:
    regular files named as Town names them, old enough that no writer can
    still be using one, including a writer that could not take the lock."""
    now = time.time()
    for entry in os.listdir(directory):
        if not _STAGED.fullmatch(entry):
            continue
        path = os.path.join(directory, entry)
        with contextlib.suppress(FileNotFoundError):
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) \
                    and now - info.st_mtime > _STAGED_ABANDONED_SECONDS:
                os.unlink(path)


def _claim(path: str) -> tuple[int, int] | None:
    """Create path empty, only if nothing is there. Returns the claim's
    device and inode, or None if something was already there."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    return info.st_dev, info.st_ino


def _withdraw_claim(path: str, claim: tuple[int, int]) -> None:
    """Remove path only if it is still our own empty claim: a key that
    replaced it, ours or another writer's, is never removed."""
    with contextlib.suppress(FileNotFoundError):
        info = os.lstat(path)
        if (info.st_dev, info.st_ino) == claim and info.st_size == 0:
            os.unlink(path)


def _replace(staged: str, path: str) -> None:
    """os.replace, retried while Windows refuses because path is open."""
    deadline = time.monotonic() + 5.0
    while True:
        try:
            os.replace(staged, path)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def _agent_id(public_hex: str) -> str:
    return "did:town:" + fingerprint(public_hex).removeprefix("sha256:")[:24]


class Keystore:
    """Controller keys on disk, plus the town's testnet registry.

    Several runs can share one keystore, as they share a Town home. So a
    controller key, once written, is never replaced; a new key and its
    registry entry are added under a lock; and a file is only ever
    replaced whole, so a reader never sees one half written.
    """

    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.registry_path = os.path.join(directory, "registry.json")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the keystore's lock, and the lock of the directory its
        registry really lives in, if a symlink puts it elsewhere.

        Directories are told apart and ordered by device and inode, not by
        how a path spells them, so one directory is never locked twice and
        two keystores sharing a registry cannot wait on each other. The
        system releases the locks if we die.
        """
        own = os.stat(self.directory)
        directories = {(own.st_dev, own.st_ino): self.directory}
        registry_dir = os.path.dirname(os.path.realpath(self.registry_path))
        shared = os.stat(registry_dir)
        directories.setdefault((shared.st_dev, shared.st_ino), registry_dir)
        with contextlib.ExitStack() as stack:
            for _identity, directory in sorted(directories.items()):
                try:
                    stack.enter_context(_directory_lock(directory))
                except PermissionError as exc:
                    if directory is self.directory:
                        raise
                    warnings.warn(
                        f"the registry's directory {directory} cannot be"
                        f" locked ({exc}): keystores sharing that registry"
                        " can lose each other's entries if they create"
                        " identities at the same time",
                        RuntimeWarning, stacklevel=3)
            yield

    def _staged(self, text: str, mode: int, directory: str) -> str:
        """A new file in directory holding text, written whole."""
        staged = os.path.join(directory, _staged_name())
        fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(staged)
            raise
        return staged

    def _registry(self) -> dict[str, Any]:
        if os.path.exists(self.registry_path):
            with open(self.registry_path) as f:
                return json.load(f)
        return {}

    def _write_registry(self, registry: dict[str, Any]) -> None:
        """Replace the registry whole, so no reader sees it half written.

        The file keeps its permissions, created under the umask as open()
        would create it, and a symlinked registry is updated where the
        link points.
        """
        target = os.path.realpath(self.registry_path)
        text = json.dumps(registry, indent=2, sort_keys=True)
        for attempt in range(5):
            staged = self._staged(text, 0o666, os.path.dirname(target))
            try:
                with contextlib.suppress(FileNotFoundError):
                    mode = stat.S_IMODE(os.stat(target).st_mode)
                    os.chmod(staged, mode)
                _replace(staged, target)
                return
            except FileNotFoundError:
                # Where this process could not take a shared registry's
                # lock, a process that did can remove the staged file as a
                # leftover. Stage it again.
                if os.path.exists(staged) or attempt == 4:
                    raise
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(staged)
                raise

    def _key_path(self, name: str) -> str:
        return os.path.join(self.directory, f"{name}.controller.key")

    def _identity_from_key(self, name: str) -> dict[str, Any] | None:
        """The identity the controller key on disk signs as, if any."""
        path = self._key_path(name)
        if not os.path.exists(path):
            return None
        text = self._controller_private(name)
        if not text:
            raise _UnwrittenKey(
                f"{path} is empty: a Town creating this identity has claimed"
                " it, or stopped before writing it. If no Town process is"
                " running, delete the file to create a new identity")
        try:
            private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(text))
        except ValueError as exc:
            raise IdentityError(
                f"{path} does not hold a controller key: {exc}") from exc
        public_hex = private.public_key().public_bytes_raw().hex()
        return {"name": name, "agent_id": _agent_id(public_hex),
                "controller_public": public_hex}

    def _registered(self, identity: dict[str, Any],
                    registry: dict[str, Any]) -> bool:
        """Whether registry lists identity; an error if under another name."""
        entry = registry.get(identity["agent_id"])
        if entry is None:
            return False
        if entry.get("name") != identity["name"]:
            raise IdentityError(
                f"the controller key for {identity['name']!r} is registered"
                f" as {entry.get('name')!r}")
        return True

    def new_identity(self, name: str) -> dict[str, Any]:
        try:
            existing = self._identity_from_key(name)
        except IdentityError:
            existing = None  # decided under the lock
        if existing is not None and self._registered(existing,
                                                     self._registry()):
            return existing
        with self._locked():
            identity = self._complete_identity(name)
            if identity is None:
                identity = self._publish_key(name)
            # A key without its entry, left by a process that stopped
            # between the two writes, is registered here.
            registry = self._registry()
            if not self._registered(identity, registry):
                registry[identity["agent_id"]] = {
                    "name": name,
                    "controller_public": identity["controller_public"],
                    "registered_at": time.time()}
                self._write_registry(registry)
            return identity

    def _complete_identity(self, name: str) -> dict[str, Any] | None:
        """The identity of name's key, waiting a moment if the key is only
        claimed so far, by a writer that may not hold the lock."""
        deadline = time.monotonic() + _KEY_WAIT_SECONDS
        while True:
            try:
                return self._identity_from_key(name)
            except _UnwrittenKey:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def _publish_key(self, name: str) -> dict[str, Any]:
        """A new controller key for name, or the one another writer won.

        The key is written whole under a staging name and then linked into
        place, which fails rather than replace a key already there. Where
        hard links are unavailable, the path is first claimed with an
        exclusive create, which likewise fails if a key is there, and the
        whole key then replaces that claim. Either way no key is ever
        replaced, whether or not any writer holds the lock, and a reader
        sees an empty claim or a whole key, never part of one.
        """
        private = Ed25519PrivateKey.generate()
        path = self._key_path(name)
        staged = self._staged(private.private_bytes_raw().hex() + "\n",
                              0o600, self.directory)
        try:
            try:
                os.link(staged, path)
            except FileExistsError:
                pass
            except OSError:
                claim = _claim(path)
                if claim is not None:
                    try:
                        _replace(staged, path)
                    except OSError:
                        _withdraw_claim(path, claim)
                        raise
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(staged)
        identity = self._complete_identity(name)
        if identity is None:
            raise IdentityError(f"no controller key stored for {name!r}")
        return identity

    def identity(self, name: str) -> dict[str, Any]:
        # A local key decides, provided the registry lists it under this
        # name: a registry can name one agent more than once, as an earlier
        # race between runs could leave it.
        local = self._identity_from_key(name)
        if local is not None:
            if self._registered(local, self._registry()):
                return local
            raise IdentityError(
                f"the controller key for {name!r} is not registered in"
                f" {self.directory}")
        for agent_id, entry in self._registry().items():
            if entry["name"] == name:
                return {"name": name, "agent_id": agent_id,
                        "controller_public": entry["controller_public"]}
        raise IdentityError(f"no identity {name!r} in {self.directory}")

    def identities(self) -> list[dict[str, Any]]:
        return [
            {"name": entry["name"], "agent_id": agent_id,
             "controller_public": entry["controller_public"]}
            for agent_id, entry in sorted(self._registry().items())
        ]

    def _controller_private(self, name: str) -> str:
        path = self._key_path(name)
        if not os.path.exists(path):
            raise IdentityError(f"no controller key for {name!r}")
        with open(path) as f:
            return f.read().strip()

    def sign(self, name: str, payload: Any) -> str:
        return _sign(self._controller_private(name), payload)

    def make_grant(self, name: str, run_id: str,
                   permissions: list[str] | None = None,
                   ttl: float = GRANT_TTL_SECONDS,
                   now: float | None = None) -> dict[str, Any]:
        """One disposable session key, authorized for one run.

        The controller key signs and stays here; only the session
        private key leaves, and it is worthless outside this run."""
        identity = self.identity(name)
        session = Ed25519PrivateKey.generate()
        now = time.time() if now is None else now
        grant = {
            "agent_id": identity["agent_id"],
            "run_id": run_id,
            "session_public": session.public_key().public_bytes_raw().hex(),
            "permissions": sorted(DEFAULT_PERMISSIONS if permissions is None
                                  else permissions),
            "issued_at": now,
            "expires_at": now + ttl,
        }
        signature = _sign(self._controller_private(name), grant)
        return {"grant": grant, "grant_signature": signature,
                "session_private":
                    session.private_bytes_raw().hex()}


def session_proof(session_private_hex: str, run_id: str,
                  name: str) -> str:
    return _sign(session_private_hex,
                 {"purpose": "join", "run_id": run_id, "name": name})


def verify_grant(grant: dict[str, Any], grant_signature: str,
                 controller_public: str, run_id: str, name: str,
                 proof: str, now: float | None = None) -> None:
    """Raises IdentityError unless the whole chain holds: the pinned
    controller signed this grant, for this run, unexpired, and the
    joiner holds the grant's session key."""
    now = time.time() if now is None else now
    _finite_timestamp(now, "verifier clock")
    if grant.get("run_id") != run_id:
        raise IdentityError("grant names a different run")
    permissions = grant.get("permissions")
    if not isinstance(permissions, list) \
            or not all(isinstance(p, str) for p in permissions):
        raise IdentityError("grant permissions must be a list of names")
    _finite_timestamp(grant.get("issued_at"), "grant issued_at")
    expires_at = _finite_timestamp(grant.get("expires_at"),
                                   "grant expires_at")
    if now > expires_at:
        raise IdentityError("grant expired")
    if not verify_signature(controller_public, grant, grant_signature):
        raise IdentityError("grant signature does not verify against"
                            " the pinned controller key")
    if not verify_signature(
            grant["session_public"],
            {"purpose": "join", "run_id": run_id, "name": name}, proof):
        raise IdentityError("session proof does not verify against the"
                            " grant's session key")


# -- registry resolvers ------------------------------------------------


def resolve_file(registry_path: str, agent_id: str) -> str:
    with open(registry_path) as f:
        registry = json.load(f)
    if agent_id not in registry:
        raise IdentityError(f"{agent_id} not in {registry_path}")
    return registry[agent_id]["controller_public"]


def resolve_eth(rpc_url: str, contract: str, selector: str,
                agent_id: str, http=None) -> str:
    """Resolve a controller key from a chain registry via eth_call.

    The contract address and function selector are configuration: the
    registry's semantics live in the deployed contract, and this
    resolver only performs the read. The argument is the 32-byte hash
    of the agent id; the return is ABI-encoded dynamic bytes holding
    the controller public key."""
    import hashlib

    import httpx

    client = http or httpx.Client(timeout=15.0)
    argument = hashlib.sha256(agent_id.encode()).hexdigest()
    data = "0x" + selector.removeprefix("0x") + argument
    response = client.post(rpc_url, json={
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": contract, "data": data}, "latest"]})
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise IdentityError(f"eth_call failed: {payload['error']}")
    raw = bytes.fromhex(payload["result"].removeprefix("0x"))
    if len(raw) < 96:
        raise IdentityError("eth_call returned no key")
    length = int.from_bytes(raw[32:64], "big")
    key = raw[64:64 + length]
    if not key:
        raise IdentityError("registry holds no controller key for"
                            f" {agent_id}")
    return key.hex()
