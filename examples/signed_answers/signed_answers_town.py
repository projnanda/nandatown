"""Town Lab roles and validator for Signed Answers from News Outlets.

A news outlet runs its own A2A agent. A reader's agent asks it about the
outlet's reporting and gets a signed source package back. This file puts
that exchange inside Town:

    sa_reader  --Town registry lookup-->  finds the outlet's card
    sa_reader  --signed Town message-->   sa_outlet
    sa_outlet  --A2A JSON-RPC over HTTP-> the outlet's agent (outside Town)
    sa_outlet  --signed Town message-->   sa_reader
    sa_reader  checks the package signature under the key on the Town card

The outlet's agent is not in this repository. Its address and the
subscriber credential come from the environment only, so neither is
written into a scenario, a bundle or this file:

    SIGNED_ANSWERS_URL    base URL of the outlet's agent
    SIGNED_ANSWERS_TOKEN  subscriber credential the gateway presents

Package content travels in the `envelope` field. The scenarios redact
that field, so the outlet's text stays out of the evidence bundle; the
bundle keeps the package id and the result of each check.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import html
import json
import os
import re
import unicodedata
import urllib.parse
import uuid
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import Trace, validator

CAPABILITY = "news.source_package"
DEFAULT_URL = "http://127.0.0.1:8970"  # examples/signed_answers/stub_outlet.py
COMPLETED = "TASK_STATE_COMPLETED"
TIMEOUT = 300.0
MAX_ENVELOPE_BYTES = 2_000_000
# Only these values from the outlet are written to Town events. Anything
# else the outlet sends is replaced, so its text cannot reach a bundle.
A2A_STATES = {"TASK_STATE_COMPLETED", "TASK_STATE_INPUT_REQUIRED",
              "TASK_STATE_FAILED"}
RESULTS = {"match", "no_match"}
PACKAGE_ID = re.compile(r"df://sha256-[0-9a-f]{64}")
# A broken or hostile reply must end as a recorded state, not a crashed run.
BAD_REPLY = (ValueError, KeyError, TypeError, AttributeError, RecursionError)


def safe_package_id(value: Any) -> str | None:
    return value if isinstance(value, str) and PACKAGE_ID.fullmatch(value) else None

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ED25519_MULTICODEC = b"\xed\x01"


# -- the outlet's wire format, restated here so Town imports no project code --


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def content_id(package: dict[str, Any]) -> str:
    return "df://sha256-" + hashlib.sha256(canonical(package)).hexdigest()


def _public_from_did(did: str) -> Ed25519PublicKey:
    if not did.startswith("did:key:z"):
        raise ValueError("not a did:key")
    number = 0
    for char in did.removeprefix("did:key:z"):
        number = number * 58 + _B58.index(char)
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big")
    if not decoded.startswith(_ED25519_MULTICODEC) or len(decoded) != 34:
        raise ValueError("not an Ed25519 did:key")
    return Ed25519PublicKey.from_public_bytes(decoded[2:])


def check_envelope(envelope: Any, expected_key: str) -> dict[str, bool]:
    """Three separate facts about a package, none inferred from another."""
    out = {"content_id_matches": False, "signature_valid": False,
           "publisher_key_matches": False}
    if not isinstance(envelope, dict):
        return out
    package = envelope.get("package")
    if not isinstance(package, dict):
        return out
    try:
        signed_bytes = canonical(package)
    except BAD_REPLY:  # includes text that cannot be encoded as UTF-8
        return out
    out["content_id_matches"] = (
        "df://sha256-" + hashlib.sha256(signed_bytes).hexdigest()
        == envelope.get("package_id"))
    publisher = package.get("publisher")
    out["publisher_key_matches"] = (isinstance(publisher, dict)
                                    and publisher.get("agent_key") == expected_key)
    signature = envelope.get("signature")
    if (not isinstance(signature, dict) or signature.get("alg") != "Ed25519"
            or signature.get("key") != expected_key):
        return out
    value = str(signature.get("value", ""))
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        _public_from_did(expected_key).verify(raw, signed_bytes)
    except (InvalidSignature, ValueError):
        return out
    out["signature_valid"] = True
    return out


MAX_STORY_BYTES = 4_000_000
_INLINE_TAG = re.compile(
    r"</?(?:a|span|em|strong|i|b|u|sup|sub|abbr|cite|code|mark|small|time|font)"
    r"\b[^>]*>", re.I)
_QUOTES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"',
                         "\u201d": '"', "\u00a0": " "})


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(text)).translate(_QUOTES)
    text = re.sub(r"\s+", " ", text)
    return re.sub(r" ([,.;:!?)])", r"\1", text).strip()


def _page_text(page: str) -> str:
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    page = _INLINE_TAG.sub("", page)  # inline tags join words, others split
    return _normalize(re.sub(r"<[^>]+>", " ", page))


def _same_site(story_url: str, publisher_url: str) -> bool:
    """The story must sit on the publisher's own site, over https (plain
    http only on loopback, for the offline stand-in)."""
    story = urllib.parse.urlparse(story_url)
    publisher = urllib.parse.urlparse(publisher_url)
    host = lambda u: (u.hostname or "").removeprefix("www.")
    if not host(story) or host(story) != host(publisher):
        return False
    return story.scheme == "https" or (
        story.scheme == "http" and story.hostname == "127.0.0.1")


def sentences_in_published_story(package: dict[str, Any]) -> int | None:
    """How many of the package's sentences appear, word for word, in the
    story as the publisher's site serves it now. None when that could not
    be checked. This is the first step of the project's own verifier."""
    try:
        story_url = package["story"]["url"]
        publisher_url = package["publisher"]["url"]
        if not (isinstance(story_url, str) and isinstance(publisher_url, str)
                and _same_site(story_url, publisher_url)):
            return None
        response = httpx.get(story_url, timeout=30.0, follow_redirects=True,
                             headers={"User-Agent": "TownReaderCheck/0.1"})
        if (response.status_code != 200
                or len(response.content) > MAX_STORY_BYTES):
            return None
        page = _page_text(response.text)
        return sum(1 for sentence in package["sentences"]
                   if _normalize(sentence["text"]) in page)
    except (httpx.HTTPError, *BAD_REPLY):
        return None


def tamper(envelope: dict[str, Any]) -> dict[str, Any]:
    """Change one character of the first sentence and leave the rest alone."""
    altered = copy.deepcopy(envelope)
    package = altered.get("package")
    if not isinstance(package, dict):
        altered["package"] = {"result": "altered"}
        return altered
    sentences = package.get("sentences")
    first = sentences[0] if isinstance(sentences, list) and sentences else None
    if isinstance(first, dict) and isinstance(first.get("text"), str):
        text = first["text"]
        swapped, count = re.subn(r"\d", lambda m: str((int(m.group()) + 1) % 10),
                                 text, count=1)
        first["text"] = swapped if count else text + " "
    else:
        package["result"] = "altered"
    return altered


class Outlet:
    """The three calls a caller makes to the outlet's agent."""

    def __init__(self, base_url: str, token: str | None):
        self.base = base_url.rstrip("/")
        self.token = token

    def _headers(self, with_token: bool) -> dict[str, str]:
        headers = {"A2A-Version": "1.0"}
        if with_token and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def card(self) -> dict[str, Any]:
        response = httpx.get(f"{self.base}/.well-known/agent-card.json",
                             timeout=30.0)
        response.raise_for_status()
        return response.json()

    def ask(self, text: str, *, message_id: str, context_id: str,
            with_token: bool) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0", "id": message_id, "method": "SendMessage",
            "params": {"message": {
                "role": "ROLE_USER", "messageId": message_id,
                "contextId": context_id,
                "parts": [{"text": text, "mediaType": "text/plain"}]}},
        }
        try:
            response = httpx.post(f"{self.base}/a2a", json=body,
                                  headers=self._headers(with_token),
                                  timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            return {"state": "UNREACHABLE", "text": type(exc).__name__}
        if response.status_code != 200:
            return {"state": f"HTTP_{response.status_code}", "text": ""}
        try:
            payload = response.json()
            if "error" in payload:
                return {"state": "RPC_ERROR",
                        "text": str(payload["error"].get("code", ""))}
            task = payload["result"]["task"]
            parts = task["status"]["message"]["parts"]
            state = task["status"]["state"]
            return {"state": state if state in A2A_STATES else "OTHER_STATE",
                    "text": "\n".join(str(p.get("text", "")) for p in parts)}
        except BAD_REPLY:
            return {"state": "MALFORMED_REPLY", "text": ""}

    def package(self, package_key: str, *,
                with_token: bool) -> dict[str, Any] | None:
        try:
            response = httpx.get(f"{self.base}/packages/{package_key}",
                                 headers=self._headers(with_token),
                                 timeout=60.0)
            if (response.status_code != 200
                    or len(response.content) > MAX_ENVELOPE_BYTES):
                return None
            envelope = response.json()
        except (httpx.HTTPError, *BAD_REPLY):
            return None
        return envelope if isinstance(envelope, dict) else None

    def package_keys(self, reply_text: str) -> list[str]:
        """Package links in a reply, only those on the outlet's own address."""
        pattern = re.escape(self.base) + r"/packages/(sha256-[0-9a-f]{64})"
        return re.findall(pattern, reply_text)


def _outlet_from_env() -> Outlet:
    return Outlet(os.environ.get("SIGNED_ANSWERS_URL") or DEFAULT_URL,
                  os.environ.get("SIGNED_ANSWERS_TOKEN") or None)


# -- roles ---------------------------------------------------------------


@role("sa_outlet")
class OutletGateway(SimAgent):
    """The outlet's presence in Town. It forwards, it does not answer.

    config:
      subscribers: Town agents whose requests carry the subscriber
        credential. Town's auth layer has already verified the sender, so
        the gateway maps a verified Town name to the credential and no
        reader ever holds it.
      remember_requests: answer a redelivered Town message from the
        gateway's own record instead of asking the outlet again.
      tamper: alter the package before passing it on (a dishonest relay).
    """

    def on_start(self):
        self.outlet = _outlet_from_env()
        self.answered: dict[str, dict[str, Any]] = {}
        self.outlet_key: str | None = None
        # Town numbers messages m-1, m-2, ... afresh in every run. The outlet
        # keeps first replies by message identity, so each run gets its own
        # prefix: a redelivery inside a run is the same message to the outlet,
        # and a message in the next run is a new one.
        self.run_tag = uuid.uuid4().hex[:12]
        try:
            card = self.outlet.card()
            key = card["signing"]["key"]
            _public_from_did(key)  # a card without a usable key is no card
        except (httpx.HTTPError, *BAD_REPLY):
            self.api.observe("outlet_unreachable", self.name,
                             {"stage": "agent_card"})
            return  # fail closed: no card, no listing
        self.outlet_key = key
        self.api.observe("outlet_card_read", self.name, {"signing_key": key})
        self.api.register([CAPABILITY], {
            "signing_key": key,
            "protocol": "A2A JSON-RPC 1.0",
        })

    def handle_source_request(self, msg):
        body = msg["body"]
        message_id = msg["message_id"]
        request_id = str(body.get("request_id", ""))
        if self.config.get("remember_requests") and message_id in self.answered:
            self.api.observe("redelivery_absorbed", request_id,
                             {"message_id": message_id})
            self.api.reply(msg, "source_answer", self.answered[message_id])
            return
        with_token = msg["sender"] in self.config.get("subscribers", [])
        reply = self.outlet.ask(str(body.get("question", "")),
                                message_id=f"{self.run_tag}-{message_id}",
                                context_id=f"{self.run_tag}-{msg['conversation']}",
                                with_token=with_token)
        keys = self.outlet.package_keys(reply["text"])
        # The package is fetched with the same credential the question
        # carried: a stranger's request never borrows the subscriber's.
        envelope = (self.outlet.package(keys[0], with_token=with_token)
                    if keys else None)
        if envelope is not None:
            try:  # Town signs what the gateway sends, so it must encode
                canonical(envelope)
            except BAD_REPLY:
                envelope, reply = None, {"state": "MALFORMED_REPLY", "text": ""}
        if envelope is not None and self.config.get("tamper"):
            # Record that the package was good before it was changed, so a
            # rejection downstream is known to come from the change.
            before = check_envelope(envelope, self.outlet_key or "")
            self.api.observe("package_altered", request_id,
                             {"valid_before_change": all(before.values())})
            envelope = tamper(envelope)
        answer = {
            "request_id": request_id,
            "state": reply["state"],
            "package_id": safe_package_id((envelope or {}).get("package_id")),
            "envelope": envelope,
        }
        self.api.observe("outlet_called", request_id, {
            "message_id": message_id,
            "credential_presented": with_token and bool(self.outlet.token),
            "state": reply["state"],
            "package_links": len(keys),
            "package_fetched": envelope is not None,
        })
        self.answered[message_id] = answer
        self.api.reply(msg, "source_answer", answer)


@role("sa_reader")
class Reader(SimAgent):
    """A reader's agent: find the outlet, ask once, check what comes back."""

    def on_start(self):
        self.outlet_name: str | None = None
        self.outlet_key: str | None = None
        self.seen: dict[str, list[str | None]] = {}
        self.api.later(1.0, self.ask)

    def ask(self):
        cards = self.api.lookup(CAPABILITY)
        if not cards:
            self.api.observe("no_outlet_found", self.name,
                             {"capability": CAPABILITY})
            return
        card = cards[0]
        key = (card.get("facts") or {}).get("signing_key")
        if not isinstance(key, str):
            self.api.observe("no_outlet_found", self.name,
                             {"capability": CAPABILITY,
                              "reason": "card has no signing key"})
            return
        self.outlet_name, self.outlet_key = card["name"], key
        self.api.observe("outlet_selected", card["name"],
                         {"signing_key": key})
        self.api.send(card["name"], "source_request", {
            "request_id": f"{self.name}-q1",
            "question": (os.environ.get("SIGNED_ANSWERS_QUESTION")
                         or self.config["question"]),
        })

    def handle_source_answer(self, msg):
        body = msg["body"]
        request_id = str(body.get("request_id", ""))
        if msg["sender"] != self.outlet_name or self.outlet_key is None:
            self.api.observe("answer_ignored", request_id,
                             {"from": msg["sender"],
                              "reason": "not the outlet this reader asked"})
            return
        earlier = self.seen.setdefault(request_id, [])
        envelope = body.get("envelope")
        if not isinstance(envelope, dict):
            earlier.append(None)
            state = body.get("state")
            self.api.observe("no_package", request_id, {
                "state": state if isinstance(state, str) and re.fullmatch(
                    r"[A-Z0-9_]{1,40}", state) else "OTHER_STATE",
                "answer_number": len(earlier),
            })
            return
        checks = check_envelope(envelope, self.outlet_key)
        package = envelope.get("package")
        package = package if isinstance(package, dict) else {}
        sentences = package.get("sentences")
        sentences = sentences if isinstance(sentences, list) else []
        package_id = safe_package_id(envelope.get("package_id"))
        result = package.get("result")
        state = body.get("state")
        earlier.append(package_id)
        # Only a package that already verified is worth comparing with the
        # published story, and only its signed story address is fetched.
        in_story = (sentences_in_published_story(package)
                    if all(checks.values()) else None)
        self.api.observe("package_checked", request_id, {
            **checks,
            "accepted": all(checks.values()),
            "state": state if state in A2A_STATES else "OTHER_STATE",
            "package_id": package_id,
            "signing_key": self.outlet_key,
            "result": (result if isinstance(result, str)
                       and result in RESULTS else "other"),
            "sentences": len(sentences),
            "story_compared": in_story is not None,
            "sentences_in_published_story": in_story,
            "correction_attached": bool(package.get("correction")),
            "answer_number": len(earlier),
            "same_as_first_answer": package_id == earlier[0],
        })


# -- validator -----------------------------------------------------------


def _stage(name: str, ok: bool, evidence: list[str], fail_note: str,
           pass_note: str = "") -> StageResult:
    if not evidence:
        return StageResult(name=name, status="not_enough_evidence",
                           note=fail_note)
    return StageResult(name=name, status="passed" if ok else "failed",
                       evidence=evidence[:8],
                       note=pass_note if ok else fail_note)


@validator("signed_answers")
def signed_answers(spec, trace: Trace) -> list[StageResult]:
    """Everything here is read from Town events, so a bundle replays."""
    outlets = [a for a in spec.agents if a.role == "sa_outlet"]
    readers = [a.name for a in spec.agents if a.role == "sa_reader"]
    # One outlet per scenario: with several, these would need to be per outlet.
    subscribers = {name for a in outlets
                   for name in a.config.get("subscribers", [])}
    tampering = any(a.config.get("tamper") for a in outlets)
    duplicated = any(f.action == "duplicate" and f.kind == "source_request"
                     for f in spec.faults)
    outlet_names = {a.name for a in outlets}
    stages = []

    listed = [e for e in trace.find("card_registered", verified=True)
              if e.subject in outlet_names]
    stages.append(_stage(
        "outlet_listed", bool(listed), [e.event_id for e in listed]
        or trace.ids("outlet_unreachable") or trace.ids("run_finished"),
        "the outlet's card must be in the Town registry, verified"))

    selected = [e for e in trace.find("outlet_selected")
                if e.observer in readers and e.subject in outlet_names]
    found_by = {e.observer for e in selected}
    stages.append(_stage(
        "outlet_discovered", found_by == set(readers),
        [e.event_id for e in selected] or trace.ids("no_outlet_found")
        or trace.ids("run_finished"),
        "every reader must find the outlet through the registry"))

    def checked(reader: str):
        return [e for e in trace.find("package_checked", observer=reader)]

    for reader in readers:
        packages = checked(reader)
        refusals = trace.find("no_package", observer=reader)
        if reader not in subscribers:
            answered = [e for e in refusals
                        if e.detail.get("state") == COMPLETED]
            stages.append(_stage(
                f"no_package_without_credential[{reader}]",
                bool(answered) and len(answered) == len(refusals)
                and not packages,
                [e.event_id for e in refusals + packages],
                "a reader with no subscriber credential must get a completed"
                " answer and no package"))
            continue
        if tampering:
            changed = trace.find("package_altered", valid_before_change=True)
            rejected = [e for e in packages
                        if not e.detail.get("accepted")
                        and not e.detail.get("signature_valid")]
            stages.append(_stage(
                f"altered_package_rejected[{reader}]",
                bool(changed) and bool(rejected)
                and len(rejected) == len(packages),
                [e.event_id for e in changed + packages + refusals],
                "the package must be valid before the gateway changes it,"
                " and the reader must then reject it on the outlet's"
                " signature"))
            continue
        # A package only counts when the outlet completed the task and the
        # package says it matched a story: a signed "no match" is not an
        # answer to the question.
        accepted = [e for e in packages if e.detail.get("accepted")
                    and e.detail.get("state") == COMPLETED
                    and e.detail.get("result") == "match"
                    and e.detail.get("sentences", 0) > 0]
        stages.append(_stage(
            f"package_verified[{reader}]",
            bool(accepted) and len(accepted) == len(packages) + len(refusals),
            [e.event_id for e in packages + refusals],
            "every answer to a subscriber's reader must be a completed task"
            " with a matching package of at least one sentence, whose id and"
            " signature check out under the key on the outlet's Town card"))
        compared = [e for e in accepted if e.detail.get("story_compared")]
        stages.append(_stage(
            f"sentences_match_published_story[{reader}]",
            bool(compared) and all(
                e.detail.get("sentences_in_published_story")
                == e.detail.get("sentences") for e in compared),
            [e.event_id for e in compared],
            "every sentence in the package must appear word for word in the"
            " story on the publisher's own site"
            if compared else "the published story could not be compared"))
        if duplicated:
            answers = packages + refusals
            ids = {e.detail.get("package_id") for e in packages}
            redelivered = trace.find("message_duplicated", kind="source_request")
            one_request = len({e.subject for e in answers}) == 1
            same = (len(ids) == 1 and None not in ids and not refusals
                    and bool(redelivered) and one_request)
            evidence = ([e.event_id for e in redelivered + answers]
                        if len(answers) > 1 else [])
            stages.append(_stage(
                f"one_package_per_request[{reader}]", same, evidence,
                f"one request, delivered twice, produced {len(answers)}"
                f" answers: {len(ids)} different package ids and"
                f" {len(refusals)} without a package"
                if len(answers) > 1 else "no second answer was observed",
                "the redelivered request got the same package"))
    return stages
