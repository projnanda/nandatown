"""Credentials recognised in an endpoint URL: used, never recorded.

An operator can reach an endpoint that wants basic authentication by
writing the credentials into its URL, as in ``http://user:secret@host``;
httpx sends them. The endpoint is tested exactly as written. What Town
prints and records is the same URL with its user information replaced by
a label:

- ``<credentials 1a2b3c4d>`` in evidence Town records and in Pulse history:
  a keyed digest of the credentials httpx would send, so a report can tell
  two sets of credentials for one host apart. The key is a random secret
  kept in the Town home and never recorded, so the label reveals nothing
  about a password, however short, to anyone who holds a bundle or report.
- ``<credentials withheld>`` in anything meant to travel, such as a
  receipt, when displaying evidence recorded before labelling existed, and
  whenever the key cannot be used.

Credentials are found where httpx finds them, in the URL's own authority,
not by searching text for things that look like URLs. A run registers the
operator's locator, and only the exact credentials it carries are replaced
in what the run records: an agent's own text is changed only where it
repeats those exact credentials between "://" and "@", as a display of old
evidence also withholds them. Only user information is recognised: a secret in a query
string or a header is not. Nor, when the URL still parses, is a password
holding an unencoded "/", "?" or "#", which httpx reads as the end of the
host: the rest of it is used, printed and recorded as written, because it
cannot be told from an "@" in a path. at_after_host lets a caller point
that out without repeating the URL. A URL that no longer parses is not
called, and everything before its last "@" is withheld.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import tempfile
import time
import warnings
from typing import Any, Callable
from urllib.parse import unquote

import httpx

WITHHELD = "<credentials withheld>"
KEY_FILENAME = "url-credentials.key"
_KEY_BYTES = 32
# A scheme may follow other text: leading whitespace, a harness prefix such
# as "a2a:", or a mistyped "name:" where "name=" was meant.
_SCHEME = re.compile(r"(?i)(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]*://")
_LABEL_USERINFO = re.compile(r"^<credentials (?:[0-9a-f]{8}|withheld)>$")


class CredentialKeyError(ValueError):
    """The Town home's labelling key exists but cannot be used."""


def town_home() -> str:
    return os.environ.get("NANDATOWN_HOME",
                          os.path.expanduser("~/.nandatown"))


def _parses(url: str) -> httpx.URL | None:
    try:
        parsed = httpx.URL(url)
        parsed.host  # a punycode host decodes, and can raise, only here
        return parsed
    except (httpx.InvalidURL, UnicodeError):
        return None


def _split(url: object) -> tuple[str, str, str] | None:
    """(scheme and "://", user information, "@" and the rest), or None.

    The authority runs to the first "/", "?" or "#", and the user
    information is everything before its last "@": httpx's own rule, so a
    password holding a quote, a bracket or a space is found where httpx
    finds it. A URL httpx cannot parse may hold credentials past such a
    character, which is exactly what made it unparseable; there, everything
    before the last "@" counts, because a URL that cannot be used is better
    hidden too widely than not enough.
    """
    if not isinstance(url, str):
        return None
    scheme = _SCHEME.search(url)
    if scheme is None:
        return None
    prefix = url[:scheme.end()]
    remainder = url[scheme.end():]
    ends = [i for i in (remainder.find(c) for c in "/?#") if i >= 0]
    at = remainder[:min(ends, default=len(remainder))].rfind("@")
    parsed = _parses(url[scheme.start():].strip())
    if parsed is not None:
        if at < 0 or not (parsed.username or parsed.password):
            return None  # httpx sends no credentials for this URL
    elif at < 0:
        at = remainder.rfind("@")
        if at <= 0:
            return None
    userinfo = remainder[:at]
    if not userinfo:
        return None
    return prefix, userinfo, remainder[at:]


def has_credentials(url: object) -> bool:
    """Whether url carries credentials that are not already a label."""
    split = _split(url)
    return split is not None and not _LABEL_USERINFO.match(split[1])


def withhold(url: str) -> str:
    """url with its credentials, raw or labelled, replaced by WITHHELD."""
    split = _split(url)
    if split is None:
        return url
    prefix, _userinfo, rest = split
    return f"{prefix}{WITHHELD}{rest}"


AT_AFTER_HOST_NOTE = (
    "If part of it is a password or token, percent-encode any '/', '?' or"
    " '#' in it (as %2F, %3F, %23): unencoded, httpx reads them as the end"
    " of the host, Town cannot recognise the credentials, and what follows"
    " is used, printed and recorded as written.")


def at_after_host(url: object) -> bool:
    """Whether url has an "@" after the end of its authority.

    httpx ends a URL's authority at the first "/", "?" or "#". A password
    holding one of those, written unencoded, therefore turns into a host,
    a path and an "@" that ends nothing, and Town, which finds credentials
    where httpx does, cannot tell that from an ordinary "@" in a path such
    as "/users/a@b". When the password also holds an "@" before that
    character, httpx does find credentials, only the wrong ones: the part
    before the "@", with the rest read as host and path. Callers can warn
    without repeating the URL.
    """
    if not isinstance(url, str):
        return False
    scheme = _SCHEME.search(url)
    if scheme is None or _parses(url[scheme.start():].strip()) is None:
        return False
    remainder = url[scheme.end():]
    ends = [i for i in (remainder.find(c) for c in "/?#") if i >= 0]
    return bool(ends) and "@" in remainder[min(ends):]


def safe_message(url: object, message: str) -> str:
    """message, safe to print beside a URL that may carry credentials.

    httpx's own error for a URL it cannot parse can quote part of it, and
    for a password holding "#", "/" or "?" that part is the password
    itself ("Invalid port: 'Hash'"). Such a message is not repeated; any
    other message has the URL's exact credentials withheld.
    """
    if not has_credentials(url):
        return message
    scheme = _SCHEME.search(url)
    if _parses(url[scheme.start():].strip()) is None:
        return "the URL cannot be parsed; its credentials are not shown"
    scrubber = Scrubber(Labeller(withhold_only=True))
    scrubber.register(url)
    return scrubber(message)


def local_key(home: str | None = None) -> bytes:
    """The Town home's labelling key, created on first use.

    Published only once complete, so two processes creating it at once
    agree on one key instead of each labelling with its own.
    """
    directory = home or town_home()
    path = os.path.join(directory, KEY_FILENAME)
    key = _read_key(path)
    if key is not None:
        return key
    os.makedirs(directory, exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix=".url-credentials-", dir=directory)
    try:
        # mkstemp creates the file readable by its owner alone, and fdopen
        # writes every byte or raises.
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(_KEY_BYTES))
            f.flush()
            os.fsync(f.fileno())
        try:
            # Publishes the complete file or nothing, and never overwrites.
            os.link(staged, path)
        except FileExistsError:
            pass
        except OSError:
            _publish_without_links(staged, path)
    finally:
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass
    key = _read_key(path)
    if key is None:
        raise CredentialKeyError(f"{path} could not be created")
    return key


def _publish_without_links(staged: str, path: str) -> None:
    """Publish a complete key where hard links are not supported.

    An exclusive create claims the path, so only one process publishes,
    and the complete key then replaces that empty claim in one step, so no
    reader ever sees part of a key. A reader that finds the claim empty
    waits for the replacement.
    """
    try:
        claim = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(claim)
    deadline = time.monotonic() + _PUBLISH_WAIT_SECONDS
    while True:
        try:
            os.replace(staged, path)
            return
        except PermissionError:
            # Windows refuses to replace a file another process has open,
            # as a reader waiting for this key briefly does.
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


_PUBLISH_WAIT_SECONDS = 5.0
_ABANDONED_SECONDS = 120.0


def _read_key(path: str) -> bytes | None:
    """The key at path; None if there is none yet.

    A key is only ever published whole, so a new empty file is another
    process's claim on the path, still being published, and is waited for.
    An empty file dated well before any publication could still be under
    way was abandoned, and any other length is a damaged key: saying so at
    once is better than waiting, or labelling with it. The margin allows
    for a file server whose clock differs from this machine's.
    """
    deadline = time.monotonic() + _PUBLISH_WAIT_SECONDS
    while True:
        try:
            with open(path, "rb") as f:
                key = f.read()
                age = time.time() - os.fstat(f.fileno()).st_mtime
        except FileNotFoundError:
            return None
        if len(key) == _KEY_BYTES:
            return key
        if key or age > _ABANDONED_SECONDS or time.monotonic() > deadline:
            raise CredentialKeyError(
                f"{path} is not a {_KEY_BYTES}-byte key; remove it to create"
                " a new one, which changes every label from here on")
        time.sleep(0.02)


class Labeller:
    """Labels the credentials in one URL at a time.

    The key is loaded only when a URL actually carries credentials, and if
    it cannot be loaded or created the label is WITHHELD: that loses the
    ability to tell credentials apart, never the credentials themselves.
    """

    def __init__(self, key: bytes | Callable[[], bytes] | None = None,
                 withhold_only: bool = False):
        self._key = key
        # Display that must never create a key, such as a report or a
        # message about a URL, withholds instead of labelling.
        self._failed = withhold_only

    def _resolved_key(self) -> bytes | None:
        if self._failed:
            return None
        try:
            if self._key is None:
                self._key = local_key()
            elif callable(self._key):
                self._key = self._key()
        except (OSError, CredentialKeyError) as exc:
            self._failed = True
            # Withholding keeps the credentials out of the record, but it
            # also stops two sets of them for one host being told apart,
            # and what is recorded meanwhile stays withheld: say so.
            warnings.warn(
                "URL credentials are recorded as <credentials withheld>:"
                f" the labelling key cannot be used ({exc})",
                RuntimeWarning, stacklevel=3)
            return None
        return self._key

    def label_for(self, url: str) -> str | None:
        """The label for url's credentials, or None if it carries none."""
        split = _split(url)
        if split is None:
            return None
        userinfo = split[1]
        if _LABEL_USERINFO.match(userinfo):
            return userinfo
        scheme = _SCHEME.search(split[0])
        parsed = _parses(url[scheme.start():].strip())
        if parsed is not None:
            # What httpx sends: "tok@h" and "tok:@h" are the same credentials.
            credentials = f"{parsed.username}\x00{parsed.password}"
        else:
            name, _colon, password = unquote(userinfo).partition(":")
            credentials = f"{name}\x00{password}"
        key = self._resolved_key()
        if key is None:
            return WITHHELD
        digest = hmac.new(key, credentials.encode("utf-8", "surrogatepass"),
                          hashlib.sha256).hexdigest()[:8]
        return f"<credentials {digest}>"

    def label(self, url: str) -> str:
        """url with its credentials replaced by their label."""
        split = _split(url)
        label = self.label_for(url)
        if split is None or label is None:
            return url
        prefix, _userinfo, rest = split
        return f"{prefix}{label}{rest}"


class Scrubber:
    """Replaces the credentials of registered locators in recorded text.

    Only the exact credentials a registered URL carries are replaced, in
    every spelling they can take: as the operator wrote them, as httpx
    normalises them, percent-decoded, and as shell quoting spells a quote
    inside them in a recorded command. A replacement needs the "://" and
    the "@" around them, as in a URL, so an agent's "contact admin@host"
    is left alone even when "admin" is the operator's user name, while a
    URL quoted in an error message or a rerun command is not.
    """

    def __init__(self, label: Labeller | None = None):
        self.labeller = label or Labeller()
        self._pairs: list[tuple[str, str]] = []

    def register(self, url: object) -> None:
        if not has_credentials(url):
            return
        prefix, userinfo, _rest = _split(url)
        label = self.labeller.label_for(url)
        encoded = {userinfo}
        scheme = _SCHEME.search(prefix)
        parsed = _parses(url[scheme.start():].strip())
        if parsed is not None:
            encoded.add(parsed.userinfo.decode("ascii", "replace"))
        # httpx sends "tok" and "tok:" as the same credentials. Only where
        # a ":" is the separator: decoded, "alice%3A" is a user "alice:".
        encoded |= {s + ":" for s in encoded if ":" not in s}
        encoded |= {s[:-1] for s in encoded
                    if s.endswith(":") and s.count(":") == 1}
        spellings = encoded | {unquote(userinfo)}
        # shlex.quote writes a quote inside single quotes as '"'"'.
        spellings |= {s.replace("'", "'\"'\"'") for s in spellings if "'" in s}
        for spelling in spellings:
            pair = (f"://{spelling}@", f"://{label}@")
            if spelling and pair not in self._pairs:
                self._pairs.append(pair)
        self._pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    def __call__(self, text: str) -> str:
        for secret, label in self._pairs:
            text = text.replace(secret, label)
        return text

    def __bool__(self) -> bool:
        return bool(self._pairs)


def scrub(value: Any, text: Callable[[str], str]) -> Any:
    """Apply text to every string in a JSON-shaped value, keys included.

    Iterative, so an agent's output nested deeper than Python's recursion
    limit is copied like any other rather than failing Town.
    """
    def copy(item: Any) -> tuple[Any, Any]:
        if isinstance(item, str):
            return text(item), None
        if isinstance(item, dict):
            return {}, item
        if isinstance(item, list):
            return [], item
        return item, None

    result, source = copy(value)
    pending = [] if source is None else [(result, source)]
    while pending:
        target, source = pending.pop()
        if isinstance(source, dict):
            for key, item in source.items():
                shown, inner = copy(item)
                target[text(key) if isinstance(key, str) else key] = shown
                if inner is not None:
                    pending.append((shown, inner))
        else:
            for item in source:
                shown, inner = copy(item)
                target.append(shown)
                if inner is not None:
                    pending.append((shown, inner))
    return result
