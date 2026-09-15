import http.server
import socket
import threading

from nandatown.cli import main
import httpx
import pytest

from nandatown.pulse import (
    availability,
    export_records,
    probe,
    render_pulse_report,
    run_pulse,
    unprobeable,
)


class QuietHandler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_server(status=200):
    handler = type("Handler", (QuietHandler,), {"status": status})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/health"


def stop_server(server):
    server.shutdown()
    server.server_close()


def test_pulse_records_up_then_down(tmp_path):
    server, url = start_server()
    db = str(tmp_path / "pulse.db")
    run_pulse({"svc": url}, count=2, interval=0.05, db_path=db)
    server.shutdown()
    server.server_close()
    run_pulse({"svc": url}, count=2, interval=0.05, db_path=db)

    stats = availability(db)["svc"]
    assert stats["checks"] == 4
    assert stats["up"] == 2
    assert stats["availability"] == 50.0
    assert stats["last_ok"] is False

    records = export_records(db)
    assert len(records) == 4
    assert [r.result for r in records] == ["passed", "passed", "failed",
                                           "failed"]
    assert all(r.observer == "town-pulse.v1" for r in records)

    report = render_pulse_report(db)
    assert "50.0%" in report
    assert "now DOWN" in report
    assert "History is the evidence" in report


def test_single_endpoint_history_has_no_previous_endpoints(tmp_path):
    server, url = start_server()
    db = str(tmp_path / "pulse.db")
    try:
        run_pulse({"svc": url}, count=2, interval=0, db_path=db)
    finally:
        stop_server(server)

    stats = availability(db)["svc"]
    assert stats["url"] == url
    assert stats["checks"] == 2
    assert stats["previous_endpoints"] == []
    assert url not in render_pulse_report(db)


def test_repointed_name_does_not_blend_endpoint_histories(tmp_path):
    old_server, old = start_server(200)
    new_server, new = start_server(503)
    db = str(tmp_path / "pulse.db")
    try:
        run_pulse({"svc": old}, count=1, interval=0, db_path=db)
        run_pulse({"svc": new}, count=1, interval=0, db_path=db)
    finally:
        stop_server(old_server)
        stop_server(new_server)

    stats = availability(db)["svc"]
    # The headline numbers describe the endpoint Pulse probed most recently.
    assert stats["url"] == new
    assert stats["checks"] == 1
    assert stats["up"] == 0
    assert stats["availability"] == 0.0
    assert stats["last_ok"] is False
    assert stats["median_latency_ms"] is None
    # The earlier endpoint keeps its own history instead of vanishing.
    assert [(e["url"], e["checks"], e["up"], e["availability"], e["last_ok"])
            for e in stats["previous_endpoints"]] == [(old, 1, 1, 100.0, True)]

    records = export_records(db)
    assert [r.result for r in records] == ["passed", "failed"]
    assert old in records[0].evidence[0]
    assert new in records[1].evidence[0]

    report = render_pulse_report(db)
    assert "50.0%" not in report
    assert "0.0% of 1 checks, now DOWN" in report
    assert new in report
    assert f"earlier endpoint {old}: 100.0% of 1 checks, last up" in report


def test_name_returning_to_an_endpoint_reports_that_endpoint(tmp_path):
    a_server, a = start_server(200)
    b_server, b = start_server(503)
    db = str(tmp_path / "pulse.db")
    try:
        run_pulse({"svc": a}, count=1, interval=0, db_path=db)
        run_pulse({"svc": b}, count=1, interval=0, db_path=db)
        run_pulse({"svc": a}, count=1, interval=0, db_path=db)
    finally:
        stop_server(a_server)
        stop_server(b_server)

    stats = availability(db)["svc"]
    assert stats["url"] == a
    assert (stats["checks"], stats["up"], stats["last_ok"]) == (2, 2, True)
    assert [(e["url"], e["checks"], e["last_ok"])
            for e in stats["previous_endpoints"]] == [(b, 1, False)]


def test_pulse_cli(tmp_path, capsys):
    server, url = start_server()
    db = str(tmp_path / "pulse.db")
    try:
        assert main(["pulse", "--target", f"svc={url}", "--count", "2",
                     "--interval", "0.05", "--db", db]) == 0
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert "100.0%" in out
    assert main(["pulse", "--records", "--db", db]) == 0
    assert "town-pulse.v1" in capsys.readouterr().out
    assert main(["pulse", "--db", db]) == 2


def start_counting_server():
    hits = []

    class CountingHandler(QuietHandler):
        def do_GET(self):
            hits.append(self.path)
            super().do_GET()

    server = http.server.HTTPServer(("127.0.0.1", 0), CountingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/health", hits


def test_pulse_cli_refuses_reused_target_name_before_probing(tmp_path,
                                                             capsys):
    first, first_url, first_hits = start_counting_server()
    second, second_url, second_hits = start_counting_server()
    db = tmp_path / "pulse.db"
    try:
        code = main(["pulse", "--target", f"svc={first_url}",
                     "--target", f"svc={second_url}", "--count", "1",
                     "--interval", "0", "--db", str(db)])
    finally:
        for server in (first, second):
            server.shutdown()
            server.server_close()
    out = capsys.readouterr().out
    assert code == 2
    assert "target name 'svc' is given more than once" in out
    assert first_hits == [] and second_hits == []
    assert not db.exists()


def test_free_port_probe_fails_cleanly(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    db = str(tmp_path / "pulse.db")
    run_pulse({"gone": f"http://127.0.0.1:{dead_port}/"}, count=1,
              interval=0, db_path=db)
    assert availability(db)["gone"]["availability"] == 0.0


# httpx parses each of these, then raises when the host is read or when
# the address is decoded. Neither raise is an httpx.HTTPError.
UNPROBEABLE_URLS = [
    pytest.param("http://xn--a.localhost:9", id="undecodable-a-label"),
    pytest.param("http://xn--.localhost:9", id="empty-a-label"),
    pytest.param("http://[v1.fe80::a+en1]:9", id="bad-ipv6"),
    pytest.param("http://", id="no-host"),
]


@pytest.mark.parametrize("url", UNPROBEABLE_URLS)
def test_unprobeable_url_is_named_not_raised(url):
    assert unprobeable(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:9",
                                 "http://xn--caf-dma.localhost:8940",
                                 "https://agent.example/health"])
def test_a_usable_url_is_probeable(url):
    assert unprobeable(url) is None


@pytest.mark.parametrize("url", UNPROBEABLE_URLS)
def test_pulse_cli_refuses_an_unusable_target_url_before_probing(
        tmp_path, capsys, url):
    server, good_url, hits = start_counting_server()
    db = tmp_path / "pulse.db"
    try:
        code = main(["pulse", "--target", f"good={good_url}",
                     "--target", f"bad={url}", "--count", "1",
                     "--interval", "0", "--db", str(db)])
    finally:
        server.shutdown()
        server.server_close()

    out = capsys.readouterr().out
    assert code == 2
    assert f"target 'bad' has an unusable URL {url!r}" in out
    assert hits == []
    assert not db.exists()


@pytest.mark.parametrize("url", UNPROBEABLE_URLS)
def test_one_unprobeable_target_does_not_end_the_schedule(tmp_path, url):
    """A schedule is history. One bad target must not truncate the rest."""
    server, good_url, hits = start_counting_server()
    db = tmp_path / "pulse.db"
    try:
        run_pulse({"good": good_url, "bad": url}, count=3, interval=0,
                  db_path=str(db))
    finally:
        server.shutdown()
        server.server_close()

    measured = availability(str(db))
    assert measured["good"]["checks"] == 3
    assert measured["good"]["up"] == 3
    assert measured["bad"]["checks"] == 3
    assert measured["bad"]["up"] == 0
    assert len(hits) == 3


@pytest.mark.parametrize("url", UNPROBEABLE_URLS)
def test_probing_an_unusable_url_is_down_not_an_exception(url):
    result = probe(url)

    assert result["ok"] is False
    assert result["status"] == 0
    assert result["error"] == "unprobeable URL"


class RedirectHandler(http.server.BaseHTTPRequestHandler):
    location = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", self.location)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_redirect_server(location):
    handler = type("Handler", (RedirectHandler,), {"location": location})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/"


# httpx prepares the next request for any redirect, even one it will not
# follow, and these Locations raise while it does: reading the host of the
# first three, and parsing the last, which httpx reports as a protocol
# error like a server's own malformed response.
MALFORMED_LOCATIONS = [
    pytest.param("http://xn--a.localhost:9/", id="undecodable-a-label"),
    pytest.param("http://xn--.localhost:9/", id="empty-a-label"),
    pytest.param("//xn--a.localhost/", id="scheme-relative"),
    pytest.param("http://[::1/", id="invalid-url"),
]


@pytest.mark.parametrize("location", MALFORMED_LOCATIONS)
def test_a_malformed_redirect_is_the_answer_the_server_gave(location):
    """The server answered; only the address it pointed at is unusable.

    Pulse does not follow redirects, so a 302 with a well-formed Location
    is recorded as the 302 it is. A malformed one is still that answer, not
    a server that is down and not a crash.
    """
    server, url = start_redirect_server(location)
    try:
        result = probe(url)
    finally:
        server.shutdown()
        server.server_close()

    assert result["status"] == 302
    assert result["ok"] is True
    assert result["error"] == "unusable redirect location"


@pytest.mark.parametrize("location", MALFORMED_LOCATIONS)
def test_one_malformed_redirect_does_not_end_the_schedule(tmp_path,
                                                          location):
    redirect, bad_url = start_redirect_server(location)
    good, good_url, hits = start_counting_server()
    db = str(tmp_path / "pulse.db")
    try:
        run_pulse({"good": good_url, "redirect": bad_url}, count=3,
                  interval=0, db_path=db)
    finally:
        for server in (redirect, good):
            server.shutdown()
            server.server_close()

    measured = availability(db)
    assert measured["good"]["checks"] == 3
    assert measured["redirect"]["checks"] == 3
    assert len(hits) == 3


def test_a_well_formed_redirect_is_unchanged():
    server, url = start_redirect_server("http://127.0.0.1:9/elsewhere")
    try:
        result = probe(url)
    finally:
        server.shutdown()
        server.server_close()

    assert (result["ok"], result["status"]) == (True, 302)
    assert "error" not in result


class TruncatedBodyHandler(http.server.BaseHTTPRequestHandler):
    status = 200
    location = "http://127.0.0.1:9/elsewhere"

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Location", self.location)
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b"too short")
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("status,location", [
    pytest.param(200, "http://127.0.0.1:9/elsewhere", id="200"),
    pytest.param(302, "http://127.0.0.1:9/elsewhere", id="302"),
    pytest.param(302, "http://xn--a.localhost:9/", id="302-undecodable"),
    pytest.param(302, "http://[::1/", id="302-invalid-url"),
])
def test_a_body_that_never_arrives_is_still_a_failed_probe(status, location):
    """Only a redirect httpx cannot build is kept as the server's answer:
    a response whose body is cut short, redirect or not, is a failure."""
    handler = type("Handler", (TruncatedBodyHandler,),
                   {"status": status, "location": location})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = probe(f"http://127.0.0.1:{server.server_port}/")
    finally:
        server.shutdown()
        server.server_close()

    assert (result["ok"], result["status"]) == (False, 0)
    assert result["error"] == "RemoteProtocolError"


def test_an_unusable_proxy_setting_is_not_recorded_as_every_service_down(
        monkeypatch):
    """A proxy the environment names but httpx cannot use is this machine's
    misconfiguration, not the target's, and is not taken for a redirect."""
    server, url = start_redirect_server("http://xn--a.localhost:9/")
    for name in ("ALL_PROXY", "HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, "http://[::1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    try:
        with pytest.raises(httpx.InvalidURL):
            probe(url)
    finally:
        server.shutdown()
        server.server_close()
