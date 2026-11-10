#!/usr/bin/env python3
"""Check the redirect handshake between agents in Town against a running Remembrane health memory.

Requesters (a clinic, a coach, an employer programme, and an adversarial coach) ask the patient's personal agent
for health information through Town's HTTP mailbox. The personal agent (personal_agent.py, a separate process)
never answers itself: it replies with a scoped token from Remembrane, or a decline. Each requester redeems its
token at Remembrane's /context and the script checks what came back against the sharing policy, by category.
A last run points the personal agent at an unreachable memory and expects a fail-closed reply. The same agent is
then served over A2A: Town's own probe (`nandatown a2a test`) checks its card and one round trip, and the same
requesters repeat the handshake with A2A message/send.

    python run_check.py --out evidence.json --remembrane-url http://127.0.0.1:8765 \
        --owner-token-file .../hw8_owner_token --patient-file .../hw8_patient --project-version <commit>
    python run_check.py --verify evidence/local.json        # offline; needs neither Remembrane nor Town running

Evidence keeps protocol events, fingerprints, purposes, verdicts, and what each requester received: the context text
and its sources (kind, category, date, who said it). The patient is synthetic. Tokens are never kept.
"""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import time

import httpx
import uvicorn

from nandatown.a2a_adapter import artifact_texts, probe_endpoint, send_message
from nandatown.client import TownClient
from nandatown.coordinator import build_app
from nandatown.profiles import PROFILES
from nandatown.records import fingerprint

HERE = Path(__file__).resolve().parent
TOWN_BASE = "6f9a7c064486c4a0ae9242f1ff341b8a4aad0244"
FORMAT = "remembrane-redirect-v1"
AGENT = "personal-agent"
TIMEOUT = 10
REPLY_WAIT = 150          # purpose assignment may call a model on a live memory
BUDGET = 10000
GRANTED_KEYS = {"request_id", "status", "purpose", "purpose_reason", "note", "patient", "token", "redeem_url",
                "expires_in_minutes"}
OTHER_KEYS = {"request_id", "status", "purpose", "purpose_reason", "note"}

# --- the sharing policy, by category (Remembrane eval/world_v4/POLICY.md §2-§4 at the project version) ----------
SENSITIVE = {
    "mental_health": {"cond_mental_health", "med_psychiatric", "life_mood"},
    "substance_use": {"cond_substance_use", "med_substance_use"},
    "hiv_sexual_health": {"cond_hiv_sti", "med_hiv", "lab_hiv"},
    "reproductive_health": {"cond_reproductive", "med_sex_hormones"},
}
ALL_SENSITIVE = set().union(*SENSITIVE.values())
FITNESS = {"cond_circulatory", "cond_respiratory", "cond_musculoskeletal", "cond_injury", "cond_diabetes_glucose",
           "cond_blood_immune", "cond_neuro", "med_cardiovascular", "med_antithrombotic", "med_diabetes",
           "med_respiratory", "med_pain_nsaid", "med_corticosteroid", "allergy", "vitals", "lab_glucose",
           "lab_blood_count", "life_sleep", "life_exercise", "life_food"}
# This patient opted mental health out for clinic intake; substance use needs consent for clinical requesters.
INTAKE_WITHHELD = SENSITIVE["mental_health"] | SENSITIVE["substance_use"]


def in_scope(purpose, kind, category):
    """Category-level scope (time windows are not checked here)."""
    if purpose == "intake":
        return (category.startswith(("cond_", "med_", "lab_", "life_")) or category in {"allergy", "vitals",
                "care_contact"}) and category not in INTAKE_WITHHELD
    if purpose == "fitness":
        if category in ALL_SENSITIVE:
            return False
        # any recent symptom of a non-sensitive category is in a coach's scope (POLICY.md §3)
        return category in FITNESS or (kind == "symptom" and category.startswith("cond_"))
    return False


class CheckFailure(ValueError):
    """A diagnostic without health content, tokens or credentials."""


def require(condition, message):
    if not condition:
        raise CheckFailure(message)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def coordinator(db_path):
    """Town's unmodified HTTP coordinator on a disposable loopback socket."""
    admin = secrets.token_urlsafe(24)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(build_app(str(db_path), admin), log_level="critical",
                                               access_log=False))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + TIMEOUT
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            require(server.started, "Town coordinator did not start")
            with httpx.Client(base_url=url, timeout=TIMEOUT, trust_env=False) as http:
                yield http, {"X-Town-Admin": admin}, url
        finally:
            server.should_exit = True
            thread.join(timeout=TIMEOUT)


@contextmanager
def personal_agent(town_url, run_id, token, memory_url, owner, patient):
    """personal_agent.py as its own process, with Town's participant environment."""
    env = {**os.environ, "TOWN_URL": town_url, "RUN_ID": run_id, "NAME": AGENT, "TOKEN": token,
           "DEADLINE": str(REPLY_WAIT * 8), "REMEMBRANE_URL": memory_url, "REMEMBRANE_OWNER_TOKEN": owner,
           "REMEMBRANE_PATIENT": patient}
    proc = subprocess.Popen([sys.executable, str(HERE / "personal_agent.py")], env=env)
    try:
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=TIMEOUT)


def request_body(r):
    body = {"card": r["card"], "message": r["message"]}
    if r.get("requested_purpose"):
        body["requested_purpose"] = r["requested_purpose"]
    return body


def wait_reply(client, message_id):
    deadline = time.monotonic() + REPLY_WAIT
    while time.monotonic() < deadline:
        client.notify(wait=0.5)
        got = client.claim()
        if got:
            return got
    raise CheckFailure(f"no reply to {message_id} through Town")


SOURCE_FIELDS = ("kind", "category", "date", "source", "origin")


def category_counts(sources):
    return dict(sorted(Counter(f"{s.get('kind')}/{s.get('category')}" for s in sources.values()).items()))


def redeem(memory, reply, message):
    """What a requester that has never heard of Remembrane does with the redirect: POST to the address with the token."""
    r = memory.post(reply["redeem_url"], headers={"Authorization": f"Bearer {reply['token']}"},
                    json={"patient": reply["patient"], "questions": [message], "budget": BUDGET}, timeout=300)
    out = {"http": r.status_code}
    if r.status_code == 200:
        j = r.json()
        sources = j.get("sources") or {}
        items = j.get("item_ids") or []
        out.update(purpose=j.get("purpose"), items=len(items), tokens=j.get("tokens"),
                   sources_cover_items=set(items) <= set(sources),
                   categories=category_counts(sources),
                   text_fingerprint=fingerprint({"text": j.get("text", "")}),
                   text=j.get("text", ""),
                   sources={i: {k: s.get(k) for k in SOURCE_FIELDS} for i, s in sorted(sources.items())})
    # boundary checks with the same token: tampered, and used to mint a new one
    bad = reply["token"][:-4] + ("AAAA" if not reply["token"].endswith("AAAA") else "BBBB")
    out["tampered_token_http"] = memory.post(reply["redeem_url"], headers={"Authorization": f"Bearer {bad}"},
                                             json={"patient": reply["patient"], "questions": [message]}).status_code
    out["requester_delegate_http"] = memory.post(
        reply["redeem_url"].rsplit("/", 1)[0] + "/delegate", headers={"Authorization": f"Bearer {reply['token']}"},
        json={"patient": reply["patient"], "card": {"kind": "clinic intake agent"}}).status_code
    return out


def run_requesters(http, admin, town_url, memory_url, owner, patient, requesters, memory):
    profile = PROFILES["quote-clean"].model_dump()   # only the mailbox is used; the quote task does not run
    roles = {AGENT: "personal_agent", **{r["name"]: "requester" for r in requesters}}
    profile.update(name="remembrane-redirect", roles=roles, lease_seconds=30,
                   capabilities={AGENT: ["health.redirect"], **{r["name"]: [] for r in requesters}})
    created = http.post("/runs", headers=admin, json={"profile": profile})
    created.raise_for_status()
    run = created.json()
    run_id = run["run_id"]
    cases = []
    with personal_agent(town_url, run_id, run["join_tokens"][AGENT], memory_url, owner, patient):
        for r in requesters:
            client = TownClient(town_url, run_id, http)
            client.join(r["name"], run["join_tokens"][r["name"]])
            require(any(p["name"] == AGENT and "health.redirect" in p["capabilities"]
                        for p in client.participants()), "Town did not list the personal agent's capability")
            mid = f"req-{r['name']}"
            client.send(mid, AGENT, "health.request", request_body(r))
            got = wait_reply(client, mid)
            require(got["kind"] == "health.redirect" and got["from"] == AGENT, "unexpected reply kind or sender")
            reply = got["body"]
            observed = {"status": reply.get("status"), "purpose": reply.get("purpose"),
                        "reply_keys": sorted(reply), "has_token": bool(reply.get("token")),
                        "expires_in_minutes": reply.get("expires_in_minutes"),
                        "correlated": reply.get("request_id") == mid}
            if observed["has_token"]:
                observed["context"] = redeem(memory, reply, r["message"])
            client.ack(got["message_id"], got["fence"], "processed")
            cases.append({"case": r["name"], "source": r.get("source"), "expect": r["expect"],
                          "request_fingerprint": fingerprint(request_body(r)), "observed": observed})
    http.post(f"/runs/{run_id}/finish", headers=admin).raise_for_status()
    events = http.get(f"/runs/{run_id}/events", headers=admin)
    events.raise_for_status()
    for c in cases:
        c["mailbox"] = mailbox_trace(events.json()["events"], c["case"])
        verify_case(c)
    return cases


def mailbox_trace(events, name):
    """Protocol evidence for one requester's exchange: no bodies, sessions or tokens."""
    keep = {"message_accepted": ("sender", "to", "kind", "content_fingerprint"), "message_claimed": ("claimant",),
            "ack_recorded": ("status",)}
    subjects = {f"req-{name}", f"redirect-req-{name}"}
    return [{"kind": e["kind"], "subject": e["subject"], "observer": e["observer"],
             "detail": {k: e["detail"][k] for k in keep[e["kind"]]}}
            for e in events if e["kind"] in keep and e["subject"] in subjects]


def verdict(case):
    """Every expectation for one case, recomputed from the recorded observation alone."""
    exp, obs = case["expect"], case["observed"]
    checks = {"purpose": obs["purpose"] == exp["purpose"], "status": obs["status"] == exp["status"],
              "correlated": obs["correlated"]}
    if exp["status"] == "granted":
        ctx = obs.get("context") or {}
        cats = ctx.get("categories") or {}
        checks.update(
            reply_holds_only_the_redirect=set(obs["reply_keys"]) == GRANTED_KEYS,
            short_lived_token=obs["has_token"] and 0 < (obs["expires_in_minutes"] or 0) <= 60,
            context_ok=ctx.get("http") == 200 and ctx.get("purpose") == exp["purpose"] and ctx.get("items", 0) > 0,
            every_item_has_a_source=bool(ctx.get("sources_cover_items")),
            every_source_in_scope=bool(cats) and all(in_scope(exp["purpose"], *k.split("/", 1)) for k in cats),
            # the published text and sources are the ones the verdict was computed on
            text_matches_fingerprint=ctx.get("text_fingerprint") == fingerprint({"text": ctx.get("text", "")}),
            sources_match_counts=category_counts(ctx.get("sources") or {}) == cats
            and len(ctx.get("sources") or {}) == ctx.get("items"),
            tampered_token_refused=ctx.get("tampered_token_http") == 401,
            requester_cannot_mint_tokens=ctx.get("requester_delegate_http") == 403)
    else:
        checks.update(no_token=not obs["has_token"] and "context" not in obs,
                      reply_holds_no_health_content=set(obs["reply_keys"]) == OTHER_KEYS)
    return checks


def verify_case(case):
    failed = [k for k, ok in verdict(case).items() if not ok]
    require(not failed, f"{case['case']}: failed {failed}")
    name = case["case"]
    req, red = f"req-{name}", f"redirect-req-{name}"
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", case["request_fingerprint"]), "invalid request fingerprint")
    trace = case["mailbox"]
    accepted = [e for e in trace if e["kind"] == "message_accepted"]
    require([e["subject"] for e in accepted] == [req, red], f"{name}: Town did not carry request then redirect")
    require(accepted[0]["detail"] == {"sender": name, "to": AGENT, "kind": "health.request",
                                      "content_fingerprint": case["request_fingerprint"]},
            f"{name}: the request Town carried is not the recorded one")
    require({k: v for k, v in accepted[1]["detail"].items() if k != "content_fingerprint"}
            == {"sender": AGENT, "to": name, "kind": "health.redirect"}, f"{name}: redirect not addressed back")
    for subject, claimant in ((req, AGENT), (red, name)):
        events = [e for e in trace if e["subject"] == subject]
        require([e["kind"] for e in events] == ["message_accepted", "message_claimed", "ack_recorded"]
                and events[1]["detail"] == {"claimant": claimant}
                and events[2]["observer"] == claimant and events[2]["detail"] == {"status": "processed"},
                f"{name}: {subject} was not accepted, claimed and acknowledged in order")


@contextmanager
def a2a_agent(memory_url, owner, patient):
    """personal_agent.py --a2a as its own process; yields its base URL once the card is served."""
    port = free_port()
    env = {**os.environ, "REMEMBRANE_URL": memory_url, "REMEMBRANE_OWNER_TOKEN": owner, "REMEMBRANE_PATIENT": patient}
    proc = subprocess.Popen([sys.executable, str(HERE / "personal_agent.py"), "--a2a", "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + TIMEOUT
        while True:
            try:
                if httpx.get(url + "/.well-known/agent-card.json", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            require(time.monotonic() < deadline, "the A2A personal agent did not start")
            time.sleep(0.1)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=TIMEOUT)


def run_a2a(url, requesters, memory):
    """Town's A2A probe, then each requester's handshake over message/send."""
    report = probe_endpoint(url)          # exactly what `nandatown a2a test URL` runs
    probe = {"ok": report["ok"], "problems": report["problems"], "task_state": report.get("task_state"),
             "card_name": (report.get("card") or {}).get("name")}
    cases = []
    for r in requesters:
        task = send_message(url, json.dumps(request_body(r)), timeout_seconds=REPLY_WAIT)
        texts = artifact_texts(task)
        require(len(texts) == 1 and isinstance(texts[0], str), f"{r['name']}: expected one text artifact")
        reply = json.loads(texts[0])
        observed = {"status": reply.get("status"), "purpose": reply.get("purpose"), "reply_keys": sorted(reply),
                    "has_token": bool(reply.get("token")), "expires_in_minutes": reply.get("expires_in_minutes"),
                    "correlated": reply.get("request_id") == task.get("id")}
        if observed["has_token"]:
            observed["context"] = redeem(memory, reply, r["message"])
        case = {"case": r["name"], "source": r.get("source"), "expect": r["expect"],
                "request_fingerprint": fingerprint(request_body(r)), "observed": observed,
                "task": {"kind": task.get("kind"), "state": (task.get("status") or {}).get("state"),
                         "text_artifacts": len(texts)}}
        verify_a2a_case(case)
        cases.append(case)
    verify_probe(probe)
    return {"probe": probe, "cases": cases}


def verify_probe(probe):
    # Town's probe sends a widget quote; a health redirect agent must answer it with a well-formed failed task.
    require(probe["ok"] and not probe["problems"] and probe["task_state"] == "failed"
            and probe["card_name"] == "remembrane-personal-agent", "Town's A2A probe did not pass as expected")


def verify_a2a_case(case):
    failed = [k for k, ok in verdict(case).items() if not ok]
    require(not failed, f"a2a {case['case']}: failed {failed}")
    require(case["task"] == {"kind": "task", "state": "completed", "text_artifacts": 1},
            f"a2a {case['case']}: not one completed task with one text artifact")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", case["request_fingerprint"]), "invalid request fingerprint")


def verify_file(path):
    evidence = json.loads(Path(path).read_text(encoding="utf-8"))
    require(evidence.get("format") == FORMAT and evidence.get("town_base") == TOWN_BASE,
            "unexpected evidence format or Town base")
    fixtures = {r["name"]: r for r in json.loads((HERE / "requesters.json").read_text(encoding="utf-8"))["requesters"]}
    names = [c["case"] for c in evidence["cases"]]
    require(names == list(fixtures) + ["memory-unreachable"], "evidence does not hold every case in order")
    for c in evidence["cases"]:
        if c["case"] in fixtures:
            require(c["request_fingerprint"] == fingerprint(request_body(fixtures[c["case"]]))
                    and c["expect"] == fixtures[c["case"]]["expect"], f"{c['case']}: not the committed fixture")
        verify_case(c)
        print(f"Verified {c['case']}: {c['observed']['status']} / {c['observed']['purpose']}")
    a2a = evidence["a2a"]
    verify_probe(a2a["probe"])
    print(f"Verified a2a probe: card {a2a['probe']['card_name']}, quote answered {a2a['probe']['task_state']}")
    require([c["case"] for c in a2a["cases"]] == list(fixtures) + ["memory-unreachable"],
            "A2A evidence does not hold every case in order")
    for c in a2a["cases"]:
        if c["case"] in fixtures:
            require(c["request_fingerprint"] == fingerprint(request_body(fixtures[c["case"]]))
                    and c["expect"] == fixtures[c["case"]]["expect"], f"a2a {c['case']}: not the committed fixture")
        verify_a2a_case(c)
        print(f"Verified a2a {c['case']}: {c['observed']['status']} / {c['observed']['purpose']}")
    return evidence


def record(args):
    fixtures = json.loads((HERE / "requesters.json").read_text(encoding="utf-8"))["requesters"]
    owner = Path(args.owner_token_file).read_text(encoding="utf-8").strip()
    patient = Path(args.patient_file).read_text(encoding="utf-8").strip()
    url = httpx.URL(args.remembrane_url)
    require(url.host in {"127.0.0.1", "localhost", "::1"} or url.scheme == "https",
            "use a loopback HTTP or an HTTPS Remembrane URL")
    with httpx.Client(timeout=TIMEOUT, trust_env=False) as memory:
        require(memory.get(f"{args.remembrane_url}/health").status_code == 200, "Remembrane is not reachable")
        with tempfile.TemporaryDirectory(prefix="remembrane-town-") as scratch:
            with coordinator(Path(scratch) / "town.db") as (http, admin, town_url):
                cases = run_requesters(http, admin, town_url, args.remembrane_url, owner, patient, fixtures, memory)
            with coordinator(Path(scratch) / "town-down.db") as (http, admin, town_url):
                down = {**next(r for r in fixtures if r["name"] == "coach"), "name": "memory-unreachable",
                        "source": "s_p1_02 with the memory stopped", "expect": {"status": "unavailable",
                                                                                 "purpose": None}}
                cases += run_requesters(http, admin, town_url, f"http://127.0.0.1:{free_port()}", owner, patient,
                                        [down], memory)
        with a2a_agent(args.remembrane_url, owner, patient) as url:
            a2a = run_a2a(url, fixtures, memory)
        with a2a_agent(f"http://127.0.0.1:{free_port()}", owner, patient) as url:
            a2a["cases"] += run_a2a(url, [down], memory)["cases"]
    return {"format": FORMAT, "town_base": TOWN_BASE, "project": "Remembrane",
            "project_version": args.project_version, "mode": "local-synthetic",
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cases": cases, "a2a": a2a}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    action = ap.add_mutually_exclusive_group(required=True)
    action.add_argument("--out", type=Path, help="run every case and write the evidence JSON here")
    action.add_argument("--verify", type=Path, help="check an existing evidence JSON offline")
    action.add_argument("--show", nargs=2, metavar=("EVIDENCE", "CASE"),
                        help="print what one requester received over the mailbox (context text and sources)")
    ap.add_argument("--remembrane-url", default="http://127.0.0.1:8765")
    ap.add_argument("--owner-token-file")
    ap.add_argument("--patient-file")
    ap.add_argument("--project-version", help="the Remembrane commit being tested")
    args = ap.parse_args()
    try:
        if args.verify:
            verify_file(args.verify)
            return 0
        if args.show:
            evidence = json.loads(Path(args.show[0]).read_text(encoding="utf-8"))
            case = next((c for c in evidence["cases"] if c["case"] == args.show[1]), None)
            require(case is not None, f"no case {args.show[1]!r}; cases: {[c['case'] for c in evidence['cases']]}")
            obs = case["observed"]
            print(f"# {case['case']} ({case['source']}): {obs['status']}, purpose {obs['purpose']}\n")
            ctx = obs.get("context")
            if not ctx:
                print("(no token, nothing received)")
                return 0
            print(ctx["text"])
            print(f"\n# {ctx['items']} items, {ctx['tokens']} tokens; sources:")
            for i, src in ctx["sources"].items():
                print(f"{i}  {src['date']}  {src['kind']}/{src['category']}  {src['source']}")
            return 0
        if not (args.owner_token_file and args.patient_file and args.project_version):
            ap.error("--out needs --owner-token-file, --patient-file and --project-version")
        require(re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", args.project_version), "invalid project version label")
        evidence = record(args)
        args.out.write_text(json.dumps(evidence, indent=1) + "\n", encoding="utf-8")
        for c in evidence["cases"]:
            ctx = c["observed"].get("context") or {}
            print(f"PASS {c['case']}: {c['observed']['status']} / {c['observed']['purpose']}"
                  + (f" ; {ctx['items']} items, all in scope" if ctx else ""))
        print(f"PASS a2a probe: card {evidence['a2a']['probe']['card_name']}, quote answered "
              f"{evidence['a2a']['probe']['task_state']}")
        for c in evidence["a2a"]["cases"]:
            print(f"PASS a2a {c['case']}: {c['observed']['status']} / {c['observed']['purpose']}")
        return 0
    except CheckFailure as error:
        print(f"Check failed: {error}", file=sys.stderr)
    except (OSError, httpx.HTTPError, KeyError, TypeError, ValueError) as error:
        print(f"Check failed: {type(error).__name__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
