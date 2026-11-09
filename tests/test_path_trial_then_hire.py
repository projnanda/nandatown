"""Trial-then-hire: a hiring agent's verdict, checked by Town.

The subject is a small stub A2A hiring agent defined here: it takes an
invoice-extraction job posting, "trials" three candidates and returns a
ranked verdict. The tests need no hiring-agent code, no model keys and no
network. Town checks that the verdict holds together and that a resent
request does not start a second trial; it never checks whether the
recommended candidate is actually the best one.
"""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from nandatown import cli, path_profiles
from nandatown.bundle import load_bundle, verify_bundle
from nandatown.path_runner import evaluate_path, run_path_test
from nandatown.records import fingerprint
from nandatown.report import render_report

SUBJECT = "http://testserver"
PROFILE = "a2a-trial-then-hire@0.1"
UNSCORABLE = "a2a-trial-then-hire-unscorable@0.1"
CARD = {"name": "stub-hiring-agent", "description": "Trials candidate"
        " agents on a posted job and recommends one.",
        "url": SUBJECT + "/", "version": "0.1",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text"], "defaultOutputModes": ["text"],
        "skills": [{"id": "trial_then_hire", "name": "Trial then hire"}]}


def stage(result, name):
    return next(s for s in result.stages if s.name == name)


def ranked_verdict(trial_id):
    return {
        "status": "tie", "recommended": "beta@1",
        "tied": ["alpha@1", "beta@1"], "trial_accuracy": 0.95,
        "ci_95": [0.9, 1.0], "cost_per_doc": 0.002,
        "valid_until": "2026-12-09", "tie": True,
        "ranking": [
            {"provider": "alpha@1", "accuracy": 0.97, "ci_95": [0.93, 1.0],
             "cost_per_doc": 0.004, "timeouts": 0, "mean_latency_ms": 900.0},
            {"provider": "beta@1", "accuracy": 0.95, "ci_95": [0.9, 1.0],
             "cost_per_doc": 0.002, "timeouts": 0, "mean_latency_ms": 700.0},
            {"provider": "gamma@1", "accuracy": 0.0, "ci_95": [0.0, 0.0],
             "cost_per_doc": None, "timeouts": 6, "mean_latency_ms": None},
        ],
        "rationale": "alpha@1 and beta@1 tie; beta@1 wins the tie-break.",
        "evidence": trial_id,
    }


class HiringAgent:
    """A stub hiring agent with at most one planted defect.

    An honest agent stores its reply text per request_id and returns it
    verbatim on an identical retry, and rejects a job whose output cannot
    be scored automatically (a free-text schema) without paying anyone.
    """

    def __init__(self, defect=None, mode="offline"):
        self.defect = defect
        self.mode = mode
        self.jobs = 0
        self.replies = {}

    def hire(self, job):
        self.jobs += 1
        job_id = f"job-{self.jobs}"
        if job.get("output_schema") == {"type": "string"} \
                and self.defect != "accept_unscorable":
            providers_usd = 0.03 if self.defect == "charge_unscorable" else 0.0
            return {"job_id": job_id, "job_status": "rejected",
                    "reason": "the output can't be scored automatically",
                    "verdict": None,
                    "spend_usd": {"platform_usd": 0.0004,
                                  "providers_usd": providers_usd,
                                  "total_usd": 0.0004 + providers_usd}}
        verdict = ranked_verdict(f"trial-{self.jobs}")
        ranking = verdict["ranking"]
        if self.defect == "wrong_recommended":
            verdict["recommended"] = "delta@1"
        elif self.defect == "bad_ci":
            ranking[1]["ci_95"] = [0.96, 1.0]
        elif self.defect == "unsorted":
            ranking[0], ranking[2] = ranking[2], ranking[0]
        elif self.defect == "tie_flag":
            verdict["tie"] = False
        elif self.defect == "one_candidate":
            del ranking[1:]
            verdict.update(status="insufficient_candidates",
                           recommended=None, tie=False)
        elif self.defect == "insufficient":
            verdict.update(status="insufficient_candidates",
                           recommended=None, tie=False)
        elif self.defect == "insufficient_recommends":
            verdict.update(status="insufficient_candidates", tie=False)
        return {"job_id": job_id, "job_status": "ranked", "reason": None,
                "verdict": verdict,
                "spend_usd": {"platform_usd": 0.004, "providers_usd": 0.12,
                              "total_usd": 0.124}}

    def reply_text(self, body):
        request_id = body["request_id"]
        if request_id in self.replies and self.defect != "second_trial":
            return self.replies[request_id]
        artifact = dict(self.hire(body["job"]), request_id=request_id,
                        candidates={"considered": ["alpha@1", "beta@1",
                                                   "gamma@1"],
                                    "excluded": []})
        if self.mode is not None:
            artifact["mode"] = self.mode
        text = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
        self.replies[request_id] = text
        return text

    def rpc(self, envelope):
        body = json.loads(envelope["params"]["message"]["parts"][0]["text"])
        text = self.reply_text(body)
        task = {"kind": "task", "id": f"task-{self.jobs}",
                "contextId": "ctx-1", "status": {"state": "completed"},
                "artifacts": [{"artifactId": "hire", "name": "hire",
                               "parts": [{"kind": "text", "text": text}]}]}
        return {"jsonrpc": "2.0", "id": envelope["id"], "result": task}

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


def run(tmp_path, agent, profile=PROFILE, **kwargs):
    with agent.client() as http:
        return run_path_test(SUBJECT, str(tmp_path), profile_ref=profile,
                             http=http, **kwargs)


@contextmanager
def loopback(agent, delay=0.0):
    """The stub agent over real HTTP, optionally slow to answer."""

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
                self.reply(agent.rpc(envelope))
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


# -- the profiles -------------------------------------------------------


def test_profiles_carry_the_job_a_long_limit_and_their_scope():
    for ref, schema in ((PROFILE, "object"), (UNSCORABLE, "string")):
        profile = path_profiles.get_path_profile(ref)
        assert profile.evaluator == "trial-hire-evaluator@0.1"
        assert profile.capability == "trial_then_hire"
        assert profile.controlled_condition == "duplicate_request"
        # A live hire takes minutes; the stock 15 s limit cannot fit it.
        assert profile.limits["timeout_seconds"] == 240.0
        assert list(profile.request) == ["job"]
        assert profile.request["job"]["output_schema"]["type"] == schema
        not_tested = " ".join(profile.expected["not_tested"])
        for gap in ("tasks/get", "streaming", "payment", "hire step"):
            assert gap in not_tested


def test_stock_timeout_variant_differs_only_in_its_limit():
    main = path_profiles.get_path_profile(PROFILE)
    stock = path_profiles.get_path_profile(
        "a2a-trial-then-hire-stock-timeout@0.1")
    assert stock.limits["timeout_seconds"] == 15.0
    assert (stock.request, stock.expected, stock.evaluator) \
        == (main.request, main.expected, main.evaluator)


def test_hire_slower_than_the_stock_limit_fails_protocol_invocation(
        tmp_path, monkeypatch):
    # The boundary case, scaled down: a hire slower than the limit.
    ref = "a2a-trial-then-hire-stock-timeout@0.1"
    stock = path_profiles.get_path_profile(ref)
    monkeypatch.setitem(path_profiles.PATH_PROFILES, ref, stock.model_copy(
        update={"limits": dict(stock.limits, timeout_seconds=0.5)}))
    with loopback(HiringAgent(), delay=1.5) as url:
        directory, result = run_path_test(url, str(tmp_path),
                                          profile_ref=ref)
    assert stage(result, "protocol_invocation").status == "failed"
    assert stage(result, "protocol_invocation").note == "a2a_timeout"
    assert [stage(result, name).status for name in
            ("semantic_result", "duplicate_request")] == ["not_tested"] * 2
    assert result.verdict == "failed"
    assert verify_bundle(directory) == []


# -- the main profile ---------------------------------------------------


def test_honest_hiring_agent_passes(tmp_path):
    directory, result = run(tmp_path, HiringAgent())
    assert result.verdict == "passed"
    assert result.evaluator_version == "path-trial-hire-0.1"
    semantic = stage(result, "semantic_result")
    assert semantic.status == "passed"
    assert semantic.note.startswith("offline run: job job-1 ranked 3")
    assert "'beta@1'" in semantic.note
    assert "not that the pick is the best provider" in semantic.note
    assert stage(result, "duplicate_request").status == "passed"
    # Unpinned, so descriptor consistency is honestly not tested.
    assert stage(result, "descriptor_consistency").status == "not_tested"
    assert verify_bundle(directory) == []


def test_candidates_are_recorded_as_sent_but_not_judged(tmp_path):
    agent = HiringAgent()
    directory, result = run(tmp_path, agent)
    hires = [e.detail["hire"] for e in load_bundle(directory)["events"]
             if e.kind == "fulfillment_observed"]
    assert [h["candidates"] for h in hires] == [
        {"considered": ["alpha@1", "beta@1", "gamma@1"], "excluded": []}] * 2
    assert result.verdict == "passed"
    assert verify_bundle(directory) == []


def test_a_result_without_candidates_still_passes(tmp_path):
    agent = HiringAgent()
    reply_text = agent.reply_text

    def without_candidates(body):
        artifact = json.loads(reply_text(body))
        artifact.pop("candidates")
        return json.dumps(artifact, sort_keys=True, separators=(",", ":"))

    agent.reply_text = without_candidates
    directory, result = run(tmp_path, agent)
    assert result.verdict == "passed"
    hires = [e.detail["hire"] for e in load_bundle(directory)["events"]
             if e.kind == "fulfillment_observed"]
    assert all("candidates" not in h for h in hires)


def test_pinned_card_passes_every_stage(tmp_path):
    directory, result = run(tmp_path, HiringAgent(),
                            pin_card_digest=fingerprint(CARD))
    assert [s.status for s in result.stages] == ["passed"] * 6
    assert verify_bundle(directory) == []


def test_a_live_run_is_labelled_live(tmp_path):
    _, result = run(tmp_path, HiringAgent(mode="live"))
    assert stage(result, "semantic_result").note.startswith("live run:")


def test_bundle_replays_to_the_same_judgment(tmp_path):
    directory, result = run(tmp_path, HiringAgent(defect="bad_ci"))
    bundle = load_bundle(directory)
    replayed = evaluate_path(bundle["profile"], result.run_id,
                             bundle["events"])
    assert [(s.name, s.status, s.note) for s in replayed.stages] \
        == [(s.name, s.status, s.note) for s in result.stages]
    assert verify_bundle(directory) == []


def test_report_names_the_failure(tmp_path):
    directory, _ = run(tmp_path, HiringAgent(defect="wrong_recommended"))
    report = render_report(load_bundle(directory))
    assert "a2a-trial-then-hire@0.1" in report
    assert "delta@1" in report


@pytest.mark.parametrize("defect, expected", [
    ("wrong_recommended",
     "verdict.recommended: 'delta@1' is not a candidate in the ranking"),
    ("bad_ci", "verdict.ranking[1].ci_95: expected [lo, hi] with"
               " lo <= 0.95 <= hi, observed [0.96, 1.0]"),
    ("unsorted", "expected candidates ordered by accuracy, highest first"),
    ("tie_flag", "verdict.tie: expected True for status 'tie',"
                 " observed False"),
    ("one_candidate", "expected at least 2 ranked candidates, observed 1"),
    ("insufficient_recommends",
     "verdict.recommended: expected null for status"
     " 'insufficient_candidates', observed 'beta@1'"),
])
def test_a_verdict_that_does_not_hold_together_fails(tmp_path, defect,
                                                     expected):
    directory, result = run(tmp_path, HiringAgent(defect=defect))
    semantic = stage(result, "semantic_result")
    assert semantic.status == "failed"
    assert expected in semantic.note
    assert "(mode 'offline')" in semantic.note
    # The duplicate is still observed: the agent answered it the same way.
    assert stage(result, "duplicate_request").status == "passed"
    assert result.verdict == "failed"
    assert verify_bundle(directory) == []


def test_insufficient_candidates_without_a_pick_passes(tmp_path):
    _, result = run(tmp_path, HiringAgent(defect="insufficient"))
    assert stage(result, "semantic_result").status == "passed"
    assert "verdict 'insufficient_candidates', recommended None" \
        in stage(result, "semantic_result").note


def test_a_result_without_a_mode_fails(tmp_path):
    # An unlabelled result could be an offline run read as a live one.
    _, result = run(tmp_path, HiringAgent(mode=None))
    semantic = stage(result, "semantic_result")
    assert semantic.status == "failed"
    assert "mode: expected 'live' or 'offline', observed None" in semantic.note


def test_a_second_trial_on_the_duplicate_fails(tmp_path):
    directory, result = run(tmp_path, HiringAgent(defect="second_trial"))
    assert stage(result, "semantic_result").status == "passed"
    duplicate = stage(result, "duplicate_request")
    assert duplicate.status == "failed"
    assert "idempotency defect" in duplicate.note
    assert "attempt 1 gave job 'job-1', trial 'trial-1', attempt 2 gave" \
           " job 'job-2', trial 'trial-2'" in duplicate.note
    assert result.verdict == "failed"
    assert verify_bundle(directory) == []


# -- the unscorable variant (our failure case) ---------------------------


def test_unscorable_job_rejected_before_any_trial_passes(tmp_path):
    directory, result = run(tmp_path, HiringAgent(), profile=UNSCORABLE)
    assert result.verdict == "passed"
    semantic = stage(result, "semantic_result")
    assert semantic.status == "passed"
    assert "was rejected before any trial" in semantic.note
    assert stage(result, "duplicate_request").status == "passed"
    assert verify_bundle(directory) == []


def test_unscorable_job_accepted_instead_of_rejected_fails(tmp_path):
    _, result = run(tmp_path, HiringAgent(defect="accept_unscorable"),
                    profile=UNSCORABLE)
    semantic = stage(result, "semantic_result")
    assert semantic.status == "failed"
    assert "job_status: expected 'rejected', observed 'ranked'" \
        in semantic.note


def test_unscorable_job_that_paid_candidates_fails(tmp_path):
    _, result = run(tmp_path, HiringAgent(defect="charge_unscorable"),
                    profile=UNSCORABLE)
    semantic = stage(result, "semantic_result")
    assert semantic.status == "failed"
    assert "spend_usd.providers_usd: expected 0" in semantic.note


# -- timeouts -----------------------------------------------------------


def test_timeout_fails_protocol_invocation(tmp_path):
    directory, result = run(tmp_path, HiringAgent(defect="timeout"))
    invocation = stage(result, "protocol_invocation")
    assert invocation.status == "failed"
    assert invocation.note == "a2a_timeout"
    assert stage(result, "semantic_result").status == "not_tested"
    assert stage(result, "duplicate_request").status == "not_tested"
    assert result.verdict == "failed"
    assert verify_bundle(directory) == []


def test_slow_agent_over_real_http_hits_the_profile_limit(tmp_path,
                                                          monkeypatch):
    # The limit comes from the profile; a short copy keeps the test fast.
    profile = path_profiles.get_path_profile(PROFILE)
    fast = profile.model_copy(update={"limits": dict(
        profile.limits, timeout_seconds=0.5)})
    monkeypatch.setitem(path_profiles.PATH_PROFILES, PROFILE, fast)
    with loopback(HiringAgent(), delay=1.5) as url:
        _, result = run_path_test(url, str(tmp_path), profile_ref=PROFILE)
    assert stage(result, "protocol_invocation").note == "a2a_timeout"
    assert stage(result, "semantic_result").status == "not_tested"


# -- the CLI ------------------------------------------------------------


def test_cli_runs_the_profile_by_id(tmp_path, capsys):
    with loopback(HiringAgent()) as url:
        code = cli.main(["test-agent", "--url", url, "--path-profile",
                         PROFILE, "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "under profile a2a-trial-then-hire@0.1" in out
    assert "5 of 6 path stages passed; 1 not tested." in out
