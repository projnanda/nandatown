"""Invoice extraction: a provider agent being hired, checked by Town.

The subject is a small stub A2A provider defined here. It extracts the one
pinned synthetic invoice into JSON. Town checks the result's correlation,
cost and types against the request's own output_schema, and that a resent
request is not fulfilled twice. It never checks field accuracy, and the
bundle records the output's types, not its values.
"""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from nandatown import cli, path_profiles
from nandatown.bundle import load_bundle, verify_bundle
from nandatown.path_runner import evaluate_path, run_path_test

SUBJECT = "http://testserver"
PROFILE = "a2a-invoice-extraction@0.1"
CARD = {"name": "stub-invoice-provider", "description": "Extracts invoices.",
        "url": SUBJECT + "/", "version": "1",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text"], "defaultOutputModes": ["text"],
        "skills": [{"id": "invoice_extraction", "name": "Invoice extraction",
                    "tags": ["invoice_extraction"]}]}
# The pinned invoice's answer, entirely synthetic.
INVOICE = {
    "vendor_name": "Anderson LLC", "invoice_number": "BILL-9567",
    "invoice_date": "2026-05-23", "currency": "USD",
    "line_items": [
        {"description": "27-inch monitor", "quantity": 9.0,
         "unit_price": 301.74, "amount": 2715.66},
        {"description": "Software licence, annual seat", "quantity": 21.0,
         "unit_price": 411.8, "amount": 8647.8},
    ],
    "subtotal": 11363.46, "tax": 710.22, "total": 12073.68,
}


def stage(result, name):
    return next(s for s in result.stages if s.name == name)


class Provider:
    """A stub provider with at most one planted defect. An honest one
    stores its reply per request_id and returns it verbatim on a retry."""

    def __init__(self, defect=None):
        self.defect = defect
        self.fulfilments = 0
        self.replies = {}

    def result(self, body):
        self.fulfilments += 1
        output = json.loads(json.dumps(INVOICE))
        result = {"task_id": body["task_id"], "output": output,
                  "cost_usd": 0.004, "provider_version": "1",
                  "request_id": body["request_id"]}
        if self.defect == "invalid_invoice":
            result["output"] = {"text": "not json"}
        elif self.defect == "string_total":
            output["total"] = "12073.68"
        elif self.defect == "extra_key":
            output["notes"] = "paid"
        elif self.defect == "bad_line_item":
            output["line_items"][1] = "monitor x9"
        elif self.defect == "no_request_id":
            del result["request_id"]
        elif self.defect == "wrong_task_id":
            result["task_id"] = "another-task"
        elif self.defect == "negative_cost":
            result["cost_usd"] = -0.01
        elif self.defect == "second_fulfilment":
            # A provider that re-runs its model reports a new spend.
            result["cost_usd"] = 0.004 * self.fulfilments
        return result

    def task(self, body):
        if self.defect == "budget":
            return {"kind": "task", "id": "task-1", "contextId": "ctx-1",
                    "status": {"state": "failed", "message": {
                        "kind": "message", "role": "agent",
                        "messageId": "msg-1", "parts": [{
                            "kind": "text",
                            "text": '{"error":"budget_exhausted"}'}]}},
                    "artifacts": []}
        request_id = body["request_id"]
        if request_id not in self.replies or self.defect == "second_fulfilment":
            self.replies[request_id] = json.dumps(
                self.result(body), sort_keys=True, separators=(",", ":"))
        return {"kind": "task", "id": f"task-{self.fulfilments}",
                "contextId": "ctx-1", "status": {"state": "completed"},
                "artifacts": [{"artifactId": "result", "parts": [
                    {"kind": "text", "text": self.replies[request_id]}]}]}

    def rpc(self, envelope):
        body = json.loads(envelope["params"]["message"]["parts"][0]["text"])
        return {"jsonrpc": "2.0", "id": envelope["id"],
                "result": self.task(body)}

    def client(self):
        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, json=CARD)
            if self.defect == "timeout":
                raise httpx.ReadTimeout("no reply in time", request=request)
            return httpx.Response(200, json=self.rpc(json.loads(
                request.content)))

        return httpx.Client(base_url=SUBJECT,
                            transport=httpx.MockTransport(handle))


def run(tmp_path, provider):
    with provider.client() as http:
        return run_path_test(SUBJECT, str(tmp_path), profile_ref=PROFILE,
                             http=http)


@contextmanager
def loopback(provider, delay=0.0):
    """The stub provider over real HTTP, optionally slow to answer."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(CARD)

        def do_POST(self):
            envelope = json.loads(
                self.rfile.read(int(self.headers["Content-Length"])))
            time.sleep(delay)
            try:
                self.reply(provider.rpc(envelope))
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


# -- the profile --------------------------------------------------------


def test_profile_pins_one_synthetic_invoice_and_its_scope():
    profile = path_profiles.get_path_profile(PROFILE)
    request = profile.request
    assert profile.evaluator == "invoice-extraction-evaluator@0.1"
    assert profile.capability == "invoice_extraction"
    assert profile.limits["timeout_seconds"] == 90.0
    assert request["task_id"] == "nandatown-invoice-001"
    assert request["requester_id"] == "nandatown"
    assert request["deadline_ms"] == 60000 and request["offered_price"] == 0.01
    # Pinned by tag, so the URL survives a rebase of the commit.
    assert request["document_url"] == (
        "https://raw.githubusercontent.com/chris-a-ackerman/nandatown/"
        "agent-hunter-fixture-v1/examples/agent-hunter/fixtures/d/"
        "1070b4b25d3c.pdf")
    pdf = (Path(__file__).parents[1] / "examples" / "agent-hunter"
           / "fixtures" / "d" / "1070b4b25d3c.pdf")
    assert pdf.read_bytes().startswith(b"%PDF")
    assert request["output_schema"]["title"] == "Invoice"
    assert "field accuracy" in " ".join(profile.expected["not_tested"])


# -- passing and failing results ----------------------------------------


def test_honest_provider_passes(tmp_path):
    directory, result = run(tmp_path, Provider())
    assert result.verdict == "passed"
    assert result.evaluator_version == "path-invoice-extraction-0.1"
    semantic = stage(result, "semantic_result")
    assert semantic.status == "passed"
    assert "field accuracy is not checked" in semantic.note
    assert stage(result, "duplicate_request").status == "passed"
    assert verify_bundle(directory) == []


def test_bundle_records_the_output_types_not_the_values(tmp_path):
    directory, _ = run(tmp_path, Provider())
    events = (Path(directory) / "events.jsonl").read_text()
    assert '"vendor_name":"string"' in events.replace(" ", "")
    for value in ("Anderson LLC", "BILL-9567", "12073.68"):
        assert value not in events


@pytest.mark.parametrize("defect, expected", [
    ("invalid_invoice", "output.vendor_name: missing"),
    ("string_total", "output.total: expected number, observed string"),
    ("extra_key", "output.notes: not in the schema"),
    ("bad_line_item",
     "output.line_items[1]: expected object, observed string"),
    ("no_request_id", "observed request_id None"),
    ("wrong_task_id", "task_id: expected 'nandatown-invoice-001',"
                      " observed 'another-task'"),
    ("negative_cost", "cost_usd: expected a number >= 0, observed -0.01"),
])
def test_an_invalid_extraction_fails(tmp_path, defect, expected):
    directory, result = run(tmp_path, Provider(defect=defect))
    semantic = stage(result, "semantic_result")
    assert semantic.status == "failed"
    assert expected in semantic.note
    assert result.verdict == "failed"
    assert verify_bundle(directory) == []


def test_nullable_fields_accept_null(tmp_path, monkeypatch):
    monkeypatch.setitem(INVOICE, "invoice_number", None)
    monkeypatch.setitem(INVOICE, "tax", None)
    _, result = run(tmp_path, Provider())
    assert stage(result, "semantic_result").status == "passed"


def test_a_second_fulfilment_on_the_duplicate_fails(tmp_path):
    directory, result = run(tmp_path, Provider(defect="second_fulfilment"))
    assert stage(result, "semantic_result").status == "passed"
    duplicate = stage(result, "duplicate_request")
    assert duplicate.status == "failed"
    assert "second distinct fulfillment" in duplicate.note
    assert verify_bundle(directory) == []


def test_a_failed_task_fails_semantic_result_with_its_error(tmp_path):
    directory, result = run(tmp_path, Provider(defect="budget"))
    # The A2A exchange itself worked; the provider declined the work.
    assert stage(result, "protocol_invocation").status == "passed"
    semantic = stage(result, "semantic_result")
    assert semantic.status == "failed"
    assert "'budget_exhausted'" in semantic.note
    assert stage(result, "duplicate_request").status == "not_tested"
    assert verify_bundle(directory) == []


def test_bundle_replays_to_the_same_judgment(tmp_path):
    directory, result = run(tmp_path, Provider(defect="string_total"))
    bundle = load_bundle(directory)
    replayed = evaluate_path(bundle["profile"], result.run_id,
                             bundle["events"])
    assert [(s.name, s.status, s.note) for s in replayed.stages] \
        == [(s.name, s.status, s.note) for s in result.stages]


# -- timeouts and the CLI ------------------------------------------------


def test_timeout_fails_protocol_invocation(tmp_path):
    directory, result = run(tmp_path, Provider(defect="timeout"))
    invocation = stage(result, "protocol_invocation")
    assert invocation.status == "failed" and invocation.note == "a2a_timeout"
    assert stage(result, "semantic_result").status == "not_tested"
    assert stage(result, "duplicate_request").status == "not_tested"
    assert verify_bundle(directory) == []


def test_slow_provider_over_real_http_hits_the_profile_limit(tmp_path,
                                                             monkeypatch):
    profile = path_profiles.get_path_profile(PROFILE)
    fast = profile.model_copy(update={"limits": dict(
        profile.limits, timeout_seconds=0.5)})
    monkeypatch.setitem(path_profiles.PATH_PROFILES, PROFILE, fast)
    with loopback(Provider(), delay=1.5) as url:
        _, result = run_path_test(url, str(tmp_path), profile_ref=PROFILE)
    assert stage(result, "protocol_invocation").note == "a2a_timeout"
    assert stage(result, "semantic_result").status == "not_tested"


def test_cli_runs_the_profile_by_id(tmp_path, capsys):
    with loopback(Provider()) as url:
        code = cli.main(["test-agent", "--url", url, "--path-profile",
                         PROFILE, "--out", str(tmp_path)])
    assert code == 0
    assert "5 of 6 path stages passed; 1 not tested." \
        in capsys.readouterr().out
