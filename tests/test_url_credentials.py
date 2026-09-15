"""Credentials written into an endpoint URL are used, and never recorded.

The agent under test really requires basic authentication here, so a
passing run proves the credentials reached the exact endpoint, and a
bundle, report, receipt or Pulse history that never contains them proves
they went nowhere else.
"""

import base64
import contextlib
import http.server
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from nandatown.a2a_adapter import build_agent_card, probe_endpoint
from nandatown.bundle import load_bundle, verify_bundle
from nandatown.replay import render_replay
from nandatown.cli import main
from nandatown.path_runner import run_path_test
from nandatown.pulse import (
    availability,
    export_records,
    render_pulse_report,
    run_pulse,
)
from nandatown.receipt import make_receipt, verify_receipt
from nandatown.report import render_report
from nandatown.url_credentials import (
    KEY_FILENAME,
    WITHHELD,
    CredentialKeyError,
    Labeller,
    Scrubber,
    at_after_host,
    has_credentials,
    local_key,
    safe_message,
    withhold,
)

USER, SECRET = "alice", "s3cret-pw"
FIXTURES = Path(__file__).parent / "fixtures"
OLD_BUNDLE = FIXTURES / "credential-url-path-bundle"
OLD_RECEIPT = FIXTURES / "credential-url-path-bundle.receipt.json"
OLD_SECRET = "fixture-s3cret"
LABELLED = re.compile(r"^http://<credentials [0-9a-f]{8}>@127\.0\.0\.1:\d+$")


@pytest.fixture(autouse=True)
def town_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("NANDATOWN_HOME", str(home))
    return home


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def running_auth_agent(password=SECRET, advertise=False):
    """A real localhost A2A agent that answers only alice:<password>."""
    port = free_port()
    root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen(
        [sys.executable, str(FIXTURES / "basic_auth_a2a_agent.py"),
         str(port), USER, password, *(["advertise"] if advertise else [])],
        env=dict(os.environ, PYTHONPATH=str(root / "src")),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"http://{base}/", trust_env=False, timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    try:
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def auth_agent():
    with running_auth_agent() as base:
        yield base


def files_containing(directory, needle):
    return sorted(str(p.relative_to(directory)) for p in Path(directory).rglob("*")
                  if p.is_file() and needle.encode() in p.read_bytes())


def bundle_of(capsys, argv):
    code = main(argv)
    out = capsys.readouterr().out
    return code, out, out.rsplit("Evidence bundle: ", 1)[1].split()[0]


# ---- recognising and labelling credentials ---------------------------------

LABEL = Labeller(b"k" * 32)


@pytest.mark.parametrize("url, host_part", [
    ("http://alice:s3cret-pw@127.0.0.1:9/x", "@127.0.0.1:9/x"),
    ("https://s3cret-pw@api.example/v1?x=1", "@api.example/v1?x=1"),
    ("http://alice:s3cret-pw@[::1]:9/", "@[::1]:9/"),
    ("HTTPS://alice:s3cret-pw@h", "@h"),
    # httpx allows these in user information and sends them.
    ("http://alice:Qu0te'Pw@127.0.0.1:9", "@127.0.0.1:9"),
    ('http://alice:Dq"Pw@127.0.0.1:9', "@127.0.0.1:9"),
    ("http://alice:An<gl>e@127.0.0.1:9", "@127.0.0.1:9"),
    ("http://alice:Sp ace@127.0.0.1:9", "@127.0.0.1:9"),
    # httpx cannot parse these, and what it cannot parse is the password.
    ("http://alice:Hash#Pw1@127.0.0.1:9", "@127.0.0.1:9"),
    ("http://alice:Slash/Pw2@127.0.0.1:9/x", "@127.0.0.1:9/x"),
])
def test_credentials_are_found_where_httpx_finds_them(url, host_part):
    labelled = LABEL.label(url)

    assert has_credentials(url)
    assert re.fullmatch(r"(?i)https?://<credentials [0-9a-f]{8}>"
                        + re.escape(host_part), labelled), labelled
    assert withhold(url).endswith(f"{WITHHELD}{host_part}")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9/x", "http://[::1]:9", "agent-name", None,
    "http://@h",           # httpx sends no credentials for this
    "http://h/users/a@b",  # an "@" in the path is not user information
    "http://<credentials 1a2b3c4d>@h",
])
def test_a_url_without_credentials_is_left_as_written(url):
    assert not has_credentials(url)
    if isinstance(url, str) and "credentials" not in url:
        assert LABEL.label(url) == url


def test_the_same_credentials_have_one_label_however_they_are_written():
    assert LABEL.label_for("http://alice:p@ss@h") == LABEL.label_for(
        "http://alice:p%40ss@h/elsewhere")
    # httpx sends "tok" with an empty password either way.
    assert LABEL.label_for("http://tok@h") == LABEL.label_for("http://tok:@h")
    assert LABEL.label_for("http://alice:one@h") != LABEL.label_for(
        "http://alice:two@h")
    assert LABEL.label(LABEL.label("http://alice:one@h")) == LABEL.label(
        "http://alice:one@h")


def test_withholding_covers_raw_and_labelled_credentials():
    raw = "http://alice:s3cret-pw@127.0.0.1:9"

    assert withhold(raw) == f"http://{WITHHELD}@127.0.0.1:9"
    assert withhold(LABEL.label(raw)) == f"http://{WITHHELD}@127.0.0.1:9"


def test_only_a_registered_locators_exact_credentials_are_replaced():
    scrubber = Scrubber(LABEL)
    scrubber.register('http://alice:Dq"Pw@127.0.0.1:9')

    # As written, and as httpx quotes it back in its own error messages.
    assert SECRET_FREE(scrubber('http://alice:Dq"Pw@127.0.0.1:9'))
    assert "<credentials " in scrubber(
        "for url 'http://alice:Dq%22Pw@127.0.0.1:9/x'")
    # An agent's own words are recorded as it said them.
    for text in ('the password Dq"Pw alone',
                 "see https://example.com,ops@example.org",
                 "contact admin@example.com"):
        assert scrubber(text) == text


def SECRET_FREE(text):
    return "Dq" not in text


def test_registering_a_locator_without_credentials_never_loads_the_key():
    def refuse():
        raise AssertionError("the key was loaded")

    scrubber = Scrubber(Labeller(refuse))
    scrubber.register("http://127.0.0.1:9")
    scrubber.register(None)

    assert not scrubber
    assert scrubber("anything at all") == "anything at all"


def test_a_key_that_cannot_be_used_withholds_and_says_so():
    def unwritable():
        raise PermissionError("read-only home")

    with pytest.warns(RuntimeWarning, match="withheld"):
        labelled = Labeller(unwritable).label("http://alice:pw@h")

    assert labelled == f"http://{WITHHELD}@h"


def test_a_message_that_could_quote_a_password_is_not_repeated():
    unparseable = "http://alice:Hash#Pw1@127.0.0.1:9"

    assert "Hash" not in safe_message(unparseable, "Invalid port: 'Hash'")
    assert safe_message("http://127.0.0.1:9", "Invalid port: 'x'") == (
        "Invalid port: 'x'")


@pytest.mark.parametrize("text, host_part", [
    (" http://alice:LeadPw1@127.0.0.1:9", "@127.0.0.1:9"),
    ("\thttp://alice:LeadPw1@127.0.0.1:9", "@127.0.0.1:9"),
    ("a2a:http://trk:TrackPw@127.0.0.1:9", "@127.0.0.1:9"),
    ("svc:http://alice:TypoPw@127.0.0.1:9", "@127.0.0.1:9"),
])
def test_credentials_after_leading_text_are_still_found(text, host_part):
    assert has_credentials(text)
    assert withhold(text).endswith(f"{WITHHELD}{host_part}")
    assert "Pw" not in LABEL.label(text)


def test_an_agents_email_is_not_the_operators_user_name():
    """A user name alone is often a plain word; only a URL spells it."""
    scrubber = Scrubber(LABEL)
    scrubber.register("http://admin@127.0.0.1:9")

    assert scrubber("contact admin@example.com") == "contact admin@example.com"
    assert scrubber("task id admin@task") == "task id admin@task"
    assert "admin@" not in scrubber("'http://admin@127.0.0.1:9/x'")


def test_a_shell_quoted_password_is_found_in_a_recorded_command():
    url = "http://alice:Old'Pw@127.0.0.1:9"
    scrubber = Scrubber(LABEL)
    scrubber.register(url)

    command = shlex.join(["nandatown", "test-agent", "--url", url])

    assert "Old" not in scrubber(command)


def test_an_at_sign_httpx_reads_as_path_is_flagged_not_guessed():
    """Town cannot tell a password with "/" in it from a path with "@"."""
    ambiguous = "http://alice:2024/Pw1@127.0.0.1:9"

    assert at_after_host(ambiguous)
    assert at_after_host("http://h/users/a@b")
    # A password "SplitA@SplitB/SplitC": httpx sends "SplitA" to "SplitB",
    # and "SplitC" is left in the path.
    assert has_credentials("http://alice:SplitA@SplitB/SplitC@127.0.0.1:9")
    assert at_after_host("http://alice:SplitA@SplitB/SplitC@127.0.0.1:9")
    assert not at_after_host("http://alice:pw@127.0.0.1:9/x")
    assert not at_after_host("http://127.0.0.1:9/x")
    # Neither form is rewritten: guessing would hide a real host.
    assert LABEL.label("http://h/users/a@b") == "http://h/users/a@b"


def test_concurrent_creators_agree_where_hard_links_are_not_supported(
        town_home, tmp_path):
    script = ("import os, sys\n"
              "def no_links(a, b):\n"
              "    raise OSError(45, 'Operation not supported')\n"
              "os.link = no_links\n"
              "from nandatown.url_credentials import local_key\n"
              "sys.stdout.write(local_key().hex())\n")
    processes = [subprocess.Popen([sys.executable, "-c", script],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=os.environ)
                 for _ in range(8)]
    results = [p.communicate(timeout=30) for p in processes]

    assert {out for out, _err in results} == {local_key().hex().encode()}
    assert all(b"withheld" not in err for _out, err in results)


def test_the_key_is_private_and_every_process_agrees_on_it(town_home):
    script = ("import sys; from nandatown.url_credentials import local_key;"
              " sys.stdout.write(local_key().hex())")
    processes = [subprocess.Popen([sys.executable, "-c", script],
                                  stdout=subprocess.PIPE, env=os.environ)
                 for _ in range(8)]
    keys = {p.communicate(timeout=30)[0] for p in processes}

    assert len(keys) == 1
    path = town_home / KEY_FILENAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert local_key().hex().encode() in keys


def test_the_key_is_created_where_hard_links_are_not_supported(
        town_home, monkeypatch):
    def no_links(source, destination):
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(os, "link", no_links)

    key = local_key()

    assert len(key) == 32 and local_key() == key
    assert stat.S_IMODE((town_home / KEY_FILENAME).stat().st_mode) == 0o600


def test_a_slow_writer_never_shows_another_process_half_a_key(
        town_home, monkeypatch):
    """Where hard links are missing, a reader must not catch a key mid-write."""
    import threading
    from unittest import mock

    real_fdopen = os.fdopen

    def slow_fdopen(fd, mode="r", *args, **kwargs):
        handle = real_fdopen(fd, mode, *args, **kwargs)
        if "w" in mode:
            write = handle.write

            def slowly(data):
                written = write(data[:4])
                handle.flush()
                time.sleep(0.3)
                return written + write(data[4:])

            handle.write = slowly
        return handle

    def no_links(source, destination):
        raise OSError(18, "Invalid cross-device link")

    labels = []
    with mock.patch("os.link", no_links), mock.patch("os.fdopen", slow_fdopen):
        def label_once():
            labels.append(Labeller().label("http://alice:pw@h"))

        threads = [threading.Thread(target=label_once) for _ in range(4)]
        for thread in threads:
            thread.start()
            time.sleep(0.05)
        for thread in threads:
            thread.join()

    assert len(set(labels)) == 1 and WITHHELD not in labels[0], labels


def test_a_corrupt_key_is_named_and_never_silently_replaced(town_home):
    town_home.mkdir(parents=True)
    (town_home / KEY_FILENAME).write_bytes(b"short")

    with pytest.raises(CredentialKeyError, match="remove it"):
        local_key()


# ---- Path ------------------------------------------------------------------

def test_credentials_reach_the_endpoint_and_nothing_records_them(
        tmp_path, capsys, auth_agent):
    url = f"http://{USER}:{SECRET}@{auth_agent}"

    code, out, bundle = bundle_of(capsys, [
        "test-agent", "--url", url, "--out", str(tmp_path / "runs")])

    assert code == 0 and "PASSED" in out, out
    assert SECRET not in out
    assert files_containing(bundle, SECRET) == []
    run = load_bundle(bundle)["run"]
    assert LABELLED.match(run.config["subject"]), run.config["subject"]
    assert "<operator-supplied-url>" in run.config["rerun_command"]
    assert "url" in run.config["rerun_required_inputs"]
    assert verify_bundle(bundle) == []

    assert main(["receipt", bundle]) == 0
    receipt_out = capsys.readouterr().out
    assert "nothing private leaves the bundle" not in receipt_out
    receipt = json.loads((Path(bundle) / "receipt.json").read_text())
    assert receipt["payload"]["claim"]["subject"] == (
        f"http://{WITHHELD}@{auth_agent}")
    assert SECRET not in json.dumps(receipt)
    assert verify_receipt(str(Path(bundle) / "receipt.json"), bundle) == []


def test_the_url_without_its_credentials_is_a_different_endpoint(
        tmp_path, capsys, auth_agent):
    """Stripping the credentials would have tested something else."""
    code, out, _ = bundle_of(capsys, [
        "test-agent", "--url", f"http://{auth_agent}",
        "--out", str(tmp_path / "runs")])

    assert code == 1 and "a2a_http_status_401" in out, out


def test_wrong_credentials_fail_without_being_recorded(
        tmp_path, capsys, auth_agent):
    wrong = "not-the-password"

    code, out, bundle = bundle_of(capsys, [
        "test-agent", "--url", f"http://{USER}:{wrong}@{auth_agent}",
        "--out", str(tmp_path / "runs")])

    assert code == 1 and "a2a_http_status_401" in out, out
    assert wrong not in out
    assert files_containing(bundle, wrong) == []


def test_an_error_quoting_the_url_is_recorded_without_its_credentials(
        tmp_path):
    """An unexpected failure is recorded as its own text, which can quote
    the request URL, credentials and all."""
    url = f"http://{USER}:{SECRET}@127.0.0.1:9"

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=build_agent_card(url))
        raise RuntimeError(f"upstream refused {request.url}")

    with httpx.Client(base_url=url,
                      transport=httpx.MockTransport(handler)) as http:
        bundle, _ = run_path_test(url, str(tmp_path / "runs"), http=http)

    assert files_containing(bundle, SECRET) == []
    reasons = [e.detail.get("reason", "") for e in load_bundle(bundle)["events"]]
    assert any("upstream refused http://<credentials " in r
               for r in reasons), reasons


def test_index_credentials_are_used_and_never_recorded(
        tmp_path, capsys, auth_agent):
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"agents": {"seller": {
        "url": f"http://{USER}:{SECRET}@{auth_agent}"}}}))

    code, out, bundle = bundle_of(capsys, [
        "test-agent", "--index", str(index), "--agent-name", "seller",
        "--out", str(tmp_path / "runs")])

    assert code == 0 and "PASSED" in out, out
    assert SECRET not in out
    assert files_containing(bundle, SECRET) == []
    # The index still holds the credentials, so its rerun is exact.
    assert "--index" in load_bundle(bundle)["run"].config["rerun_command"]


def test_two_sets_of_credentials_are_recorded_as_different_subjects(
        tmp_path):
    subjects = []
    for password in ("one", "two", "one"):
        bundle, _ = run_path_test(f"http://{USER}:{password}@127.0.0.1:9",
                                  str(tmp_path / password))
        subjects.append(load_bundle(bundle)["run"].config["subject"])

    assert subjects[0] != subjects[1]
    assert subjects[0] == subjects[2]


@pytest.mark.parametrize("password", ["Qu0te'Pw", 'Dq"Pw', "An<gl>e"])
def test_a_password_with_quotes_or_brackets_is_used_and_never_recorded(
        tmp_path, capsys, password):
    """httpx sends these; recognising credentials by pattern missed them."""
    with running_auth_agent(password) as base:
        code, out, bundle = bundle_of(capsys, [
            "test-agent", "--url", f"http://{USER}:{password}@{base}",
            "--out", str(tmp_path / "runs")])
        assert code == 0 and "PASSED" in out, out
        assert main(["receipt", bundle]) == 0
        out += capsys.readouterr().out

    assert password not in out
    assert files_containing(bundle, password) == []


@pytest.mark.parametrize("password", ["Hash#Pw1", "Slash/Pw2", "Q?Pw3"])
def test_credentials_in_a_url_httpx_cannot_parse_are_not_recorded(
        tmp_path, capsys, password):
    """The URL fails resolution, and the bundle is the kind people share."""
    url = f"http://{USER}:{password}@127.0.0.1:9"

    code, out, bundle = bundle_of(capsys, [
        "test-agent", "--url", url, "--out", str(tmp_path / "runs")])
    assert main(["a2a", "test", url]) == 1
    out += capsys.readouterr().out

    assert code == 1 and "invalid endpoint URL" in out, out
    fragment = password[:4]
    assert fragment not in out
    assert files_containing(bundle, fragment) == []


def test_an_agents_own_words_are_recorded_as_it_said_them(
        tmp_path, town_home):
    """Only the operator's credentials are withheld, never an agent's text."""
    name = "Quotes: see https://example.com,ops@example.org"

    def handler(request):
        card = build_agent_card("http://127.0.0.1:9")
        return httpx.Response(200, json=dict(card, name=name))

    with httpx.Client(base_url="http://127.0.0.1:9",
                      transport=httpx.MockTransport(handler)) as http:
        bundle, _ = run_path_test("http://127.0.0.1:9",
                                  str(tmp_path / "runs"), http=http)

    names = [e.detail.get("name") for e in load_bundle(bundle)["events"]
             if e.kind == "card_retrieved"]
    assert names == [name]
    assert not (town_home / KEY_FILENAME).exists()


def test_a_read_only_home_does_not_stop_a_run(tmp_path, town_home):
    """A home that already holds an identity can attest without writing,
    and credentials must not be what makes the run need to."""
    run_path_test("http://127.0.0.1:9", str(tmp_path / "first"))
    town_home.chmod(0o500)
    try:
        with pytest.warns(RuntimeWarning, match="cannot be used"):
            bundle, _ = run_path_test(f"http://{USER}:{SECRET}@127.0.0.1:9",
                                      str(tmp_path / "runs"))
    finally:
        town_home.chmod(0o700)

    assert files_containing(bundle, SECRET) == []
    assert WITHHELD in load_bundle(bundle)["run"].config["subject"]


def test_a_fresh_claim_dated_by_a_skewed_clock_is_still_waited_for(
        town_home):
    """Where hard links fail, as on some network shares, the file server
    dates the claim, and its clock may run seconds behind this machine's."""
    town_home.mkdir(parents=True)
    claim = town_home / KEY_FILENAME
    claim.touch()
    skewed = time.time() - 10
    os.utime(claim, (skewed, skewed))
    key = b"k" * 32

    def publish():
        time.sleep(0.3)
        staged = town_home / "staged"
        staged.write_bytes(key)
        os.replace(staged, claim)

    publisher = threading.Thread(target=publish)
    publisher.start()
    try:
        assert local_key(str(town_home)) == key
    finally:
        publisher.join()


def test_a_corrupt_key_withholds_credentials_rather_than_fail(
        tmp_path, town_home):
    town_home.mkdir(parents=True)
    (town_home / KEY_FILENAME).write_bytes(b"short")

    with pytest.warns(RuntimeWarning, match="cannot be used"):
        bundle, _ = run_path_test(f"http://{USER}:{SECRET}@127.0.0.1:9",
                                  str(tmp_path / "runs"))

    assert files_containing(bundle, SECRET) == []
    assert WITHHELD in load_bundle(bundle)["run"].config["subject"]


def test_an_abandoned_key_claim_is_reported_at_once(town_home):
    """An empty key file is a claim another process is still publishing,
    unless it is dated well before any publication could be under way."""
    town_home.mkdir(parents=True)
    claim = town_home / KEY_FILENAME
    claim.touch()
    long_ago = time.time() - 3600
    os.utime(claim, (long_ago, long_ago))

    started = time.monotonic()
    with pytest.raises(CredentialKeyError):
        local_key(str(town_home))

    assert time.monotonic() - started < 1


def test_a_url_with_leading_whitespace_does_not_leak(tmp_path, capsys):
    url = f" http://{USER}:LeadPw1@127.0.0.1:9"

    code, out, bundle = bundle_of(capsys, [
        "test-agent", "--url", url, "--out", str(tmp_path / "runs")])

    assert "LeadPw1" not in out
    assert files_containing(bundle, "LeadPw1") == []


def test_a_password_town_cannot_recognise_is_used_as_written(tmp_path,
                                                             capsys):
    """The documented limit: "9/Unseen1" as a password reads as port 9 and a
    path. Town neither refuses nor rewrites the URL, says it cannot
    recognise the credentials, and records the URL exactly as written."""
    url = "http://127.0.0.1:9/Unseen1@127.0.0.1:9"

    code, out, bundle = bundle_of(capsys, [
        "test-agent", "--url", url, "--out", str(tmp_path / "runs")])

    assert "cannot recognise the credentials" in out
    assert url not in out.split("note:")[1].splitlines()[0]
    assert "Unseen1" in out
    recorded = load_bundle(bundle)
    assert recorded["run"].config["subject"] == url
    first = recorded["events"][0]
    assert (first.kind, first.detail["url"]) == ("resolution_hop", url)


def test_a2a_test_points_out_an_at_sign_after_the_host(capsys):
    main(["a2a", "test", "http://127.0.0.1:9/Unseen2@127.0.0.1:9"])

    assert "cannot recognise the credentials" in capsys.readouterr().out


def test_an_ambiguous_at_sign_is_pointed_out(tmp_path, capsys):
    code, out, _bundle = bundle_of(capsys, [
        "test-agent", "--url", "http://127.0.0.1:2024/Pw1@127.0.0.1:9",
        "--out", str(tmp_path / "runs")])

    assert "percent-encode" in out


def test_a2a_test_withholds_credentials_a_card_repeats(capsys):
    """A card may name the URL it was reached at, credentials and all."""
    with running_auth_agent(advertise=True) as base:
        url = f"http://{USER}:{SECRET}@{base}"
        assert httpx.get(f"{url}/.well-known/agent-card.json",
                         trust_env=False).json()["url"] == url

        assert main(["a2a", "test", url]) == 0

    out = capsys.readouterr().out
    assert SECRET not in out
    assert f"http://{WITHHELD}@{base}" in out


def test_an_a2a_artifact_preview_cannot_cut_credentials_short():
    url = f"http://{USER}:{SECRET}@127.0.0.1:9"
    # The preview keeps 200 characters: the password, and not its "@".
    artifact = "x" * (200 - len(f"http://{USER}:{SECRET}")) + url

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=build_agent_card(url))
        envelope = json.loads(request.content)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": envelope["id"], "result": {
                "id": "t1", "kind": "task", "status": {"state": "completed"},
                "artifacts": [{"artifactId": "a", "parts": [
                    {"kind": "text", "text": artifact}]}]}})

    scrubber = Scrubber(Labeller(withhold_only=True))
    scrubber.register(url)
    with httpx.Client(base_url=url,
                      transport=httpx.MockTransport(handle)) as http:
        report = probe_endpoint(url, http=http, redact=scrubber)

    assert artifact[:200].endswith(SECRET)
    assert SECRET not in report["artifact"]
    assert "http://<credentials" in report["artifact"]


def test_a_fulfillment_preview_cannot_cut_credentials_short(tmp_path):
    """An unparseable fulfillment is recorded as a 200-character preview.
    Cut after the password and before its "@", the credentials no longer
    look like credentials, so they are withheld before the cut."""
    url = f"http://{USER}:{SECRET}@127.0.0.1:9"
    text = "x" * (200 - len(f"http://{USER}:{SECRET}")) + url + " and more"

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200,
                                  json=build_agent_card("http://127.0.0.1:9"))
        envelope = json.loads(request.content)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": envelope["id"], "result": {
                "id": "t1", "kind": "task", "status": {"state": "completed"},
                "artifacts": [{"artifactId": "a", "parts": [
                    {"kind": "text", "text": text}]}]}})

    with httpx.Client(base_url=url,
                      transport=httpx.MockTransport(handle)) as http:
        bundle, _ = run_path_test(url, str(tmp_path / "runs"), http=http)

    assert text[:200].endswith(SECRET)
    previews = [e.detail["text"] for e in load_bundle(bundle)["events"]
                if e.kind == "fulfillment_unparseable"]
    assert previews and all(len(p) <= 200 for p in previews)
    assert files_containing(bundle, SECRET) == []


@contextlib.contextmanager
def serving_agent(card, text):
    """A stdlib A2A agent on localhost with this card, answering each
    message/send with one completed task whose text part is text(base),
    where base is the agent's own host and port."""
    class Agent(http.server.BaseHTTPRequestHandler):
        def respond(self, body):
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.respond(card)

        def do_POST(self):
            envelope = json.loads(self.rfile.read(
                int(self.headers["Content-Length"])))
            self.respond({"jsonrpc": "2.0", "id": envelope["id"], "result": {
                "id": "t1", "kind": "task", "status": {"state": "completed"},
                "artifacts": [{"artifactId": "a", "parts": [
                    {"kind": "text", "text": text(base)}]}]}})

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Agent)
    base = f"127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


def nested(depth, leaf):
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("shape", [
    pytest.param(lambda cut: cut, id="text"),
    pytest.param(lambda cut: [cut], id="list"),
    pytest.param(lambda cut: {"echo": cut}, id="object"),
])
def test_a2a_test_withholds_before_cutting_its_artifact_preview(capsys,
                                                                shape):
    """The artifact preview keeps 200 characters: here, the password and
    not its "@". The text case guards withholding before the cut; the
    list and object cases guard shapes that are not text."""
    def artifact(base):
        url = f"http://{USER}:{SECRET}@{base}"
        return shape("x" * (200 - len(f"http://{USER}:{SECRET}")) + url)

    with serving_agent(build_agent_card("http://127.0.0.1:9"),
                       artifact) as base:
        main(["a2a", "test", f"http://{USER}:{SECRET}@{base}"])

    assert SECRET not in capsys.readouterr().out


def test_deeply_nested_agent_output_is_the_agents_failure_not_towns(
        tmp_path, capsys):
    """Withholding walks everything an agent returns, however deep."""
    deep = nested(600, "http://127.0.0.1:9")
    card = dict(build_agent_card("http://127.0.0.1:9"), name=deep)
    with serving_agent(card, lambda base: deep) as base:
        url = f"http://{USER}:{SECRET}@{base}"
        assert main(["a2a", "test", url]) in (0, 1)
        bundle, result = run_path_test(url, str(tmp_path / "runs"))

    assert SECRET not in capsys.readouterr().out
    kinds = [e.kind for e in load_bundle(bundle)["events"]]
    assert "town_driver_error" not in kinds
    assert "fulfillment_unparseable" in kinds
    assert result.verdict == "failed"


def test_credentials_are_withheld_with_or_without_an_empty_password():
    """httpx sends "tok" and "tok:" as the same credentials."""
    for operator, echoed in [("http://tok@h", "http://tok:@h"),
                             ("http://tok:@h", "http://tok@h")]:
        scrubber = Scrubber(LABEL)
        scrubber.register(operator)
        assert "tok" not in scrubber(f"see {echoed}/x"), (operator, echoed)
    scrubber = Scrubber(LABEL)
    scrubber.register("http://a:b:@h")
    assert scrubber("http://a:b@h") == "http://a:b@h"
    scrubber = Scrubber(LABEL)
    scrubber.register("http://alice%3A@h")  # the user "alice:", no password
    assert scrubber("http://alice@h") == "http://alice@h"


def test_an_operators_user_name_in_agent_text_is_left_alone(tmp_path):
    name = "contact admin@example.com"

    def handler(request):
        card = build_agent_card("http://127.0.0.1:9")
        return httpx.Response(200, json=dict(card, name=name))

    with httpx.Client(base_url="http://admin@127.0.0.1:9",
                      transport=httpx.MockTransport(handler)) as http:
        bundle, _ = run_path_test("http://admin@127.0.0.1:9",
                                  str(tmp_path / "runs"), http=http)

    names = [e.detail.get("name") for e in load_bundle(bundle)["events"]
             if e.kind == "card_retrieved"]
    assert names == [name]


@pytest.mark.parametrize("argv", [
    ["run", "quote-clean", "--agent", f"a2a:http://{USER}:{SECRET}@h:9"],
    ["run", "quote-clean", "--agent", f"seller=A2A:http://{USER}:{SECRET}@h:9"],
])
def test_a_mistyped_harness_is_not_echoed_with_its_credentials(
        tmp_path, capsys, argv):
    # An unknown harness raises rather than returning a usage error, as it
    # did before; what matters here is that neither form repeats the secret.
    try:
        main(argv + ["--out", str(tmp_path / "runs")])
        message = ""
    except Exception as exc:  # noqa: BLE001
        message = str(exc)

    assert SECRET not in capsys.readouterr().out
    assert SECRET not in message


def test_a_credential_free_report_shows_its_rerun_as_recorded(tmp_path):
    """A rerun command is not a URL. Read whole, "http://host:port ...
    --path-profile name@0.3" parses as credentials running up to that "@"."""
    with httpx.Client(base_url="http://127.0.0.1:30536",
                      transport=httpx.MockTransport(lambda r: httpx.Response(
                          200, json=build_agent_card("http://127.0.0.1:30536")))
                      ) as http:
        bundle, _ = run_path_test("http://127.0.0.1:30536",
                                  str(tmp_path / "runs"), http=http)

    loaded = load_bundle(bundle)
    rerun = loaded["run"].config["rerun_command"]
    report = render_report(loaded)

    assert "@0.3" in rerun and "<credentials" not in report
    assert f"Rerun:     {rerun}" in report


def test_an_old_reports_rerun_keeps_its_host_and_profile(old_bundle):
    report = render_report(load_bundle(str(old_bundle)))
    rerun = next(line for line in report.splitlines()
                 if line.startswith("Rerun:"))

    assert OLD_SECRET not in rerun
    assert "@0.3" not in rerun.split("--path-profile")[0].rsplit("@", 1)[-1]
    assert "--path-profile a2a-capability-fulfillment@0.3" in rerun


def test_a_run_without_credentials_never_creates_the_key(tmp_path, town_home):
    run_path_test("http://127.0.0.1:9", str(tmp_path / "runs"))

    assert not (town_home / KEY_FILENAME).exists()


# ---- evidence recorded before credentials were withheld --------------------

@pytest.fixture
def old_bundle(tmp_path):
    directory = tmp_path / "old-bundle"
    shutil.copytree(OLD_BUNDLE, directory)
    return directory


def test_a_new_receipt_over_old_evidence_withholds_its_credentials(
        old_bundle):
    before = {p.name: p.read_bytes() for p in old_bundle.iterdir()}

    path = make_receipt(str(old_bundle))

    receipt = json.loads(Path(path).read_text())
    assert OLD_SECRET not in json.dumps(receipt)
    assert WITHHELD in receipt["payload"]["claim"]["subject"]
    assert verify_receipt(path, str(old_bundle)) == []
    # The evidence itself is not rewritten.
    assert {p.name: p.read_bytes() for p in old_bundle.iterdir()
            if p.name != "receipt.json"} == before
    assert verify_bundle(str(old_bundle)) == []


def test_a_receipt_issued_before_this_still_verifies(old_bundle, tmp_path):
    receipt = tmp_path / "old-receipt.json"
    shutil.copy(OLD_RECEIPT, receipt)

    assert verify_receipt(str(receipt), str(old_bundle)) == []


def test_reports_withhold_credentials_a_run_config_recorded_anywhere(
        old_bundle):
    """Earlier Track runs recorded an a2a: harness URL and a rerun command
    carrying it; earlier Path reruns shell-quoted a password with a quote."""
    bundle = load_bundle(str(old_bundle))
    track_url = "http://trk:TrackLeft-TrackRight@127.0.0.1:8940"
    quoted_url = "http://alice:OldLeft'OldRight@127.0.0.1:8940"
    bundle["run"] = bundle["run"].model_copy(update={"config": dict(
        bundle["run"].config,
        harnesses={"seller": f"a2a:{track_url}"},
        rerun_command=shlex.join(["nandatown", "test-agent", "--url",
                                  quoted_url]) + " --agent seller=a2a:"
                      + track_url)})

    report = render_report(bundle)

    for fragment in ("TrackLeft", "OldLeft", OLD_SECRET):
        assert fragment not in report, fragment
    assert "Verdict:" in report


def test_reports_withhold_an_old_track_password_holding_a_space(old_bundle):
    """Before Track quoted its rerun command, a password with a space was
    recorded whole in the harness and split across two words of the rerun,
    and a stage note could quote the endpoint."""
    bundle = load_bundle(str(old_bundle))
    url = "http://trk:Spaced Out@127.0.0.1:8940"
    bundle["run"] = bundle["run"].model_copy(update={"config": dict(
        bundle["run"].config, harnesses={"seller": f"a2a:{url}"},
        rerun_command=f"nandatown run quote-clean --agent seller=a2a:{url}")})
    stages = list(bundle["result"].stages)
    stages[0] = stages[0].model_copy(
        update={"note": f"A2A endpoint {url} refused the connection"})
    bundle["result"] = bundle["result"].model_copy(update={"stages": stages})

    report = render_report(bundle)

    assert "Spaced" not in report and "Out@" not in report
    assert "refused the connection" in report


def test_the_visualizer_of_old_evidence_withholds_its_credentials(
        old_bundle, tmp_path, capsys):
    before = {p.name: p.read_bytes() for p in old_bundle.iterdir()}
    out = tmp_path / "town.html"

    assert main(["visualize", str(old_bundle), "-o", str(out)]) == 0

    html = out.read_text()
    assert OLD_SECRET not in html and OLD_SECRET not in capsys.readouterr().out
    assert "credentials withheld" in html.replace("\\u003c", "<")
    assert {p.name: p.read_bytes() for p in old_bundle.iterdir()} == before


def test_a_replay_of_old_evidence_withholds_its_credentials():
    """An old Track run recorded its harness URL raw, and an event could
    quote it. Replay prints each event's detail as JSON, where a quote in
    the password is escaped, so it withholds before printing."""
    bundle = load_bundle(str(FIXTURES / "track-0.2.0" / "quote-clean-dupresp"))
    url = 'http://trk:Quo"ted-Pw@127.0.0.1:8940'
    bundle["run"] = bundle["run"].model_copy(update={"config": dict(
        bundle["run"].config, harnesses={"seller": f"a2a:{url}"})})
    events = list(bundle["events"])
    events[0] = events[0].model_copy(update={
        "subject": url, "detail": {"note": f"A2A endpoint {url} refused"}})
    bundle["events"] = events

    replay = render_replay(bundle)

    assert "Quo" not in replay and "ted-Pw" not in replay, replay
    assert "refused" in replay


def test_reports_of_old_evidence_withhold_its_credentials(old_bundle):
    report = render_report(load_bundle(str(old_bundle)))

    assert OLD_SECRET not in report
    assert WITHHELD in report


# ---- Pulse -----------------------------------------------------------------

@pytest.fixture
def auth_server():
    """200 for alice:s3cret-pw, 401 for anything else."""
    expected = "Basic " + base64.b64encode(f"{USER}:{SECRET}".encode()).decode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.headers.get("Authorization") == expected
            self.send_response(200 if ok else 401)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def db_text(db):
    with sqlite3.connect(db) as conn:
        return json.dumps(conn.execute("SELECT * FROM probes").fetchall())


def test_pulse_probes_with_credentials_and_keeps_only_labels(
        tmp_path, auth_server):
    db = str(tmp_path / "pulse.db")

    run_pulse({"svc": f"http://{USER}:{SECRET}@{auth_server}"}, count=2,
              interval=0, db_path=db)

    assert SECRET not in db_text(db)
    with sqlite3.connect(db) as conn:
        assert {row[0] for row in conn.execute(
            "SELECT status FROM probes")} == {200}
    assert SECRET not in render_pulse_report(db)
    assert SECRET not in json.dumps(
        [r.model_dump() for r in export_records(db)])
    assert "<credentials " in availability(db)["svc"]["url"]


def test_pulse_keeps_two_sets_of_credentials_apart(tmp_path, auth_server):
    db = str(tmp_path / "pulse.db")
    run_pulse({"svc": f"http://{USER}:wrong@{auth_server}"}, count=1,
              interval=0, db_path=db)
    run_pulse({"svc": f"http://{USER}:{SECRET}@{auth_server}"}, count=1,
              interval=0, db_path=db)

    current = availability(db)["svc"]
    assert len(current["previous_endpoints"]) == 1
    assert current["url"] != current["previous_endpoints"][0]["url"]


def test_pulse_history_recorded_before_this_is_labelled_and_joined(
        tmp_path, auth_server):
    db = str(tmp_path / "pulse.db")
    url = f"http://{USER}:{SECRET}@{auth_server}"
    run_pulse({"svc": url}, count=1, interval=0, db_path=db)
    with sqlite3.connect(db) as conn:
        # What an earlier Town wrote: the URL itself.
        conn.execute("INSERT INTO probes VALUES (?,?,?,?,?,?)",
                     ("svc", url, time.time() - 60, 1, 200, 3.0))

    current = availability(db)["svc"]
    assert current["checks"] == 2 and current["previous_endpoints"] == []
    assert SECRET not in render_pulse_report(db)
    assert SECRET not in json.dumps(
        [r.model_dump() for r in export_records(db)])


def test_pulse_never_prints_earlier_credentials(tmp_path, capsys,
                                                auth_server):
    """The reported case: a target re-pointed away from credentials."""
    db = str(tmp_path / "pulse.db")
    for target in (f"svc=http://{USER}:{SECRET}@{auth_server}",
                   f"svc=http://{auth_server}"):
        assert main(["pulse", "--target", target, "--count", "1",
                     "--interval", "0", "--db", db]) == 0
    assert main(["pulse", "--report", "--db", db]) == 0
    assert main(["pulse", "--records", "--db", db]) == 0

    assert SECRET not in capsys.readouterr().out


@pytest.mark.parametrize("password", ["Qu0te'Pw", 'Dq"Pw', "An<gl>e",
                                      "Sp ace"])
def test_pulse_never_keeps_a_password_with_quotes_brackets_or_spaces(
        tmp_path, password):
    db = str(tmp_path / "pulse.db")

    run_pulse({"svc": f"http://{USER}:{password}@127.0.0.1:9"}, count=1,
              interval=0, db_path=db)

    assert password not in db_text(db)
    assert password not in render_pulse_report(db)
    assert password not in json.dumps(
        [r.model_dump() for r in export_records(db)])


@pytest.mark.parametrize("target", [
    f"svc=http://{USER}:Hash#Pw1@127.0.0.1:9",
    f"http://{USER}:p=w0rd@127.0.0.1:9",   # no name, and "=" in the password
    f"http://{USER}:2024/w0rd@127.0.0.1:9",  # no name, and read as a path
    f"http://{USER}:Hash#w0rd@127.0.0.1:9",  # no name, and unparseable
    f"{USER}:w0rd@127.0.0.1:9",              # no name and no scheme
    f"http:/{USER}:w0rd@127.0.0.1:9",        # no name, scheme mistyped
    f"svc=http:{USER}:w0rd@127.0.0.1:9",     # a name, scheme mistyped
    f"{USER}:Hash=w0rd@127.0.0.1:9",         # split inside the password
])
def test_a_malformed_pulse_target_is_not_echoed_with_its_credentials(
        tmp_path, capsys, target):
    assert main(["pulse", "--target", target, "--count", "1",
                 "--db", str(tmp_path / "p.db")]) == 2

    out = capsys.readouterr().out
    assert "Hash" not in out and "w0rd" not in out, out
    assert "--target 1" in out


def test_pulse_history_is_readable_from_a_home_that_cannot_hold_a_key(
        tmp_path, town_home):
    db = str(tmp_path / "pulse.db")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE probes (name TEXT NOT NULL, url TEXT NOT"
                     " NULL, at REAL NOT NULL, ok INTEGER NOT NULL, status"
                     " INTEGER NOT NULL, latency_ms REAL NOT NULL)")
        conn.execute("INSERT INTO probes VALUES (?,?,?,?,?,?)",
                     ("svc", f"http://{USER}:{SECRET}@127.0.0.1:9",
                      time.time(), 1, 200, 3.0))
    town_home.mkdir(parents=True)
    town_home.chmod(0o500)
    try:
        with pytest.warns(RuntimeWarning, match="cannot be used"):
            report = render_pulse_report(db)
    finally:
        town_home.chmod(0o700)

    assert SECRET not in report


def test_a_duplicate_pulse_target_name_is_refused_without_repeating_it(
        tmp_path, capsys):
    """A "name" can be part of a password split at its "="."""
    target = "Hash=http://w0rd@127.0.0.1:9"
    assert main(["pulse", "--target", target, "--target", target,
                 "--count", "1", "--db", str(tmp_path / "p.db")]) == 2

    out = capsys.readouterr().out
    assert "Hash" not in out and "w0rd" not in out, out
    assert "distinct name" in out and "--target 2" in out


def test_pulse_points_out_an_at_sign_after_a_targets_host(
        tmp_path, capsys, auth_server):
    assert main(["pulse", "--target",
                 f"svc=http://{auth_server}/users/a@b", "--count", "1",
                 "--interval", "0", "--db", str(tmp_path / "p.db")]) == 0

    out = capsys.readouterr().out
    assert "percent-encode" in out and "a@b" not in out


def test_an_unusable_pulse_target_is_refused_without_its_credentials(
        tmp_path, capsys):
    assert main(["pulse", "--target",
                 f"svc=http://{USER}:{SECRET}@xn--a.localhost:9",
                 "--count", "1", "--db", str(tmp_path / "p.db")]) == 2

    assert SECRET not in capsys.readouterr().out
