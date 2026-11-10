"""LOOM in Nanda Town: Lab roles and validators for a health-context
access workflow.

LOOM itself runs outside Town as an HTTP service (the `loom-portal`
package from the LOOM project). This file only adapts it to the Lab:

* ``loom_gateway`` publishes the ``health.context`` capability in the
  town registry and forwards every signed request it receives to the
  LOOM service over HTTP. LOOM's verdict comes back as a reply, as an
  attributed ``loom_decision`` event, and, for overreach, as a signed
  data-facts receipt that moves the requester's town reputation.
  If LOOM cannot be reached the gateway fails closed.
* ``loom_requester`` is a scripted agent that finds the gateway through
  the registry and runs a list of requests and fetches on logical days.
  Each step declares the outcome the LOOM policy should produce; the
  validator compares those declarations with what was recorded.

Load it from a scenario with ``plugin_files: [loom_plugin.py]``.
Nothing here is Town's own fixture behavior: the decisions are made by
the external LOOM service.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import _check, _missing, validator

CAPABILITY = "health.context"
DEFAULT_URL = "http://127.0.0.1:8787"
TIERS = ["L0", "L1", "L2", "L3"]
TIER_FIELDS = {"L0": {"raw_signal"}, "L1": {"readings"},
               "L2": {"glucose_patterns", "alerts"}, "L3": {"constraints"}}


def _allowed_fields(tier: str) -> set[str]:
    if tier not in TIERS:
        return set()
    out: set[str] = set()
    for t in TIERS[TIERS.index(tier):]:
        out |= TIER_FIELDS[t]
    return out


# -- roles ---------------------------------------------------------------


@role("loom_gateway")
class LoomGateway(SimAgent):
    """Front door to the external LOOM portal."""

    def on_start(self):
        self.url = (self.config.get("loom_url")
                    or os.environ.get("LOOM_URL", DEFAULT_URL)).rstrip("/")
        self.ns = self.api._engine.run_id
        self.api.register([CAPABILITY], {"service": "loom-portal"})
        try:
            with urllib.request.urlopen(self.url + "/health",
                                        timeout=3) as r:
                health = json.loads(r.read())
            self.api.observe("loom_portal_health", self.name,
                             {"ok": bool(health.get("ok")),
                              "version": health.get("version"),
                              "policy": health.get("policy")})
        except (OSError, ValueError) as exc:
            self.api.observe("loom_unavailable", self.name,
                             {"stage": "health",
                              "error": type(exc).__name__})

    def _call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.url}/v1/{self.ns}/{path}",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read())
            except ValueError:
                return {"decision": "denied", "reason": f"http_{exc.code}"}
        except (OSError, ValueError) as exc:
            self.api.observe("loom_unavailable", body["request_id"],
                             {"stage": path, "error": type(exc).__name__})
            # Fail closed: no portal, no data.
            return {"decision": "denied", "reason": "loom_unreachable",
                    "request_id": body["request_id"]}

    def _forward(self, msg, path: str):
        body = dict(msg["body"])
        # The town's envelope is authoritative for who is asking.
        body["agent_id"] = msg["sender"]
        body["at"] = self.api.now
        out = self._call(path, body)
        data = out.pop("data", None)
        detail = {k: out.get(k) for k in (
            "request_id", "op", "decision", "reason", "task",
            "publisher", "tier_requested", "tier_granted",
            "fields_requested", "fields_granted", "grant_id",
            "valid_days", "expires_at", "single_use", "credit_before", "credit_after",
            "replay", "policy") if k in out}
        detail["requester"] = msg["sender"]
        if isinstance(out.get("friction"), dict):
            detail["confirmation"] = out["friction"].get("confirmation")
            detail["auto_renew"] = out["friction"].get("auto_renew")
        self.api.observe("loom_decision", body["request_id"], detail)
        if out.get("decision") == "blocked" and not out.get("replay"):
            # LOOM's verdict becomes a signed receipt in the shared
            # trust layer, so other town agents can see it.
            self.api.rate(msg["sender"], "bad")
        reply = dict(detail)
        if out.get("decision") == "granted" and data is not None:
            reply["data"] = data
        kind = ("context_grant" if out.get("decision") == "granted"
                else "context_denied")
        self.api.reply(msg, kind, reply)

    def handle_context_request(self, msg):
        self._forward(msg, "requests")

    def handle_context_fetch(self, msg):
        self._forward(msg, "fetch")


@role("loom_requester")
class LoomRequester(SimAgent):
    """A scripted agent that asks LOOM for health context."""

    def on_start(self):
        self.grants: dict[str, str] = {}
        self.api.register([f"agent.{self.config.get('category', 'unknown')}"])
        self.api.later(0.01, self._discover)

    def _discover(self):
        cards = self.api.lookup(CAPABILITY)
        if not cards:
            self.api.observe("loom_gateway_missing", self.name, {})
            return
        self.gateway = cards[0]["name"]
        self.api.observe("loom_gateway_found", self.name,
                         {"gateway": self.gateway})
        for i, step in enumerate(self.config.get("steps", [])):
            self.api.later(max(0.0, float(step["day"]) - self.api.now),
                           lambda i=i, step=step: self._run(i, step))

    def _run(self, i: int, step: dict[str, Any]):
        rid = f"{self.name}-{i}"
        if step["op"] == "fetch":
            gid = self.grants.get(step["task"], "")
            self.api.send(self.gateway, "context_fetch",
                          {"request_id": rid, "grant_id": gid})
            return
        self.api.send(self.gateway, "context_request", {
            "request_id": rid,
            "publisher": self.config["publisher"],
            "category": self.config["category"],
            "task": step["task"], "purpose": step.get("purpose", ""),
            "tier": step["tier"], "fields": step.get("fields", []),
        })

    def _received(self, msg, granted: bool):
        b = msg["body"]
        if granted and b.get("grant_id") and b.get("task"):
            self.grants.setdefault(b["task"], b["grant_id"])
        data = b.get("data") if granted else None
        self.api.observe("context_received", b.get("request_id", ""), {
            "decision": b.get("decision"), "reason": b.get("reason"),
            "tier_granted": b.get("tier_granted"),
            "fields_received": sorted(data) if isinstance(data, dict)
            else [],
            "fields_requested": b.get("fields_requested"),
            "replay": b.get("replay"),
        })

    def handle_context_grant(self, msg):
        self._received(msg, True)

    def handle_context_denied(self, msg):
        self._received(msg, False)


# -- validators ----------------------------------------------------------


def _expectations(spec) -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for a in spec.agents:
        if a.role != "loom_requester":
            continue
        for i, step in enumerate(a.config.get("steps", [])):
            out.append((a.name, f"{a.name}-{i}", step))
    return out


def _first_decisions(trace) -> dict[str, Any]:
    first = {}
    for e in trace.find("loom_decision"):
        if isinstance(e.subject, str) and e.subject not in first \
                and not e.detail.get("replay"):
            first[e.subject] = e
    return first


@validator("loom_health_access")
def loom_health_access(spec, trace):
    stages = []
    exp = _expectations(spec)
    requesters = sorted({name for name, _, _ in exp})
    decisions = _first_decisions(trace)

    health = trace.find("loom_portal_health")
    stages.append(_check(
        "loom_reachable", bool(health) and all(e.detail.get("ok")
                                               for e in health),
        [e.event_id for e in health] or trace.ids("loom_unavailable"),
        "the gateway could not confirm the external LOOM service",
        "gateway reached LOOM " + ", ".join(
            f"{e.detail.get('version')} ({e.detail.get('policy')})"
            for e in health[:1])))

    found = {e.subject: e for e in trace.find("loom_gateway_found")}
    stages.append(_check(
        "discovery", all(r in found for r in requesters),
        [found[r].event_id for r in requesters if r in found],
        "a requester did not find the LOOM gateway through the registry",
        f"{len(found)} of {len(requesters)} requesters found"
        f" {CAPABILITY} via the town registry"))

    mismatches, evidence = [], []
    for _, rid, step in exp:
        e = decisions.get(rid)
        if e is None:
            mismatches.append(f"{rid}: no decision")
            continue
        evidence.append(e.event_id)
        d = e.detail
        want = step.get("expect", {})
        for key, val in want.items():
            if d.get(key) != val:
                mismatches.append(f"{rid}: {key}={d.get(key)!r},"
                                  f" expected {val!r}")
    stages.append(_check(
        "decisions_match_policy", not mismatches and bool(exp), evidence,
        "; ".join(mismatches[:4]) or "no expectations declared",
        f"all {len(exp)} requests and fetches got the declared outcome"))

    leaks, recv_ids = [], []
    for e in trace.find("context_received"):
        recv_ids.append(e.event_id)
        d = e.detail
        got = set(d.get("fields_received") or [])
        if d.get("decision") != "granted" and got:
            leaks.append(f"{e.subject}: data on a {d.get('decision')}")
        elif got - _allowed_fields(d.get("tier_granted") or ""):
            leaks.append(f"{e.subject}: {sorted(got)} beyond"
                         f" {d.get('tier_granted')}")
        elif d.get("fields_requested") is not None \
                and got - set(d.get("fields_requested")):
            leaks.append(f"{e.subject}: fields not requested")
    stages.append(_check(
        "minimum_disclosure", not leaks, recv_ids,
        "; ".join(leaks[:4]),
        "every delivery stayed within its granted tier and requested"
        " fields; denied requests carried no data"))

    blocked = [e for e in decisions.values()
               if e.detail.get("decision") == "blocked"]
    rep = trace.find("reputation_updated", outcome="bad")
    bad_subjects = {e.subject for e in rep}
    contained = bool(blocked) and all(
        e.detail.get("credit_after", 0) < e.detail.get("credit_before", 0)
        and e.detail.get("requester") in bad_subjects for e in blocked)
    no_data = all(not (r.detail.get("fields_received"))
                  for b in blocked
                  for r in trace.find("context_received", subject=b.subject))
    stages.append(_check(
        "overreach_contained", contained and no_data,
        [e.event_id for e in blocked] + [e.event_id for e in rep],
        "an overreaching request was not blocked, penalized, and"
        " recorded in the town trust layer",
        "overreach was blocked, the publisher lost LOOM credit, and a"
        " signed receipt lowered the requester's town reputation"))

    expired = [e for e in decisions.values()
               if e.detail.get("reason") == "expired"]
    in_window = {e.detail.get("grant_id") for e in decisions.values()
                 if e.detail.get("op") == "fetch"
                 and e.detail.get("decision") == "granted"}
    stages.append(_check(
        "expiry_enforced",
        bool(expired) and any(e.detail.get("grant_id") in in_window
                              for e in expired),
        [e.event_id for e in expired],
        "no fetch was denied after a grant that had worked within its"
        " validity window",
        "a grant that served fetches inside its window was refused"
        " after expiry"))

    by_task: dict[tuple, set] = {}
    bands: dict[tuple, set] = {}
    ev = []
    for e in decisions.values():
        d = e.detail
        if d.get("op") == "request" and d.get("decision") == "granted":
            key = (d.get("publisher"), d.get("task"))
            by_task.setdefault(key, set()).add(d.get("tier_granted"))
            bands.setdefault(key, set()).add(d.get("confirmation"))
            ev.append(e.event_id)
    varied = [k for k in by_task if len(bands[k]) > 1]
    stages.append(_check(
        "friction_not_depth",
        bool(varied) and all(len(by_task[k]) == 1 for k in varied),
        ev, "no publisher was seen at two credit bands, or its tier"
        " changed with credit",
        "after a credit drop the same task kept its tier; only the"
        " confirmation requirement changed"))

    groups: dict[str, list] = {}
    for e in trace.find("loom_decision"):
        groups.setdefault(e.subject, []).append(e)
    dup = {k: v for k, v in groups.items() if len(v) > 1}
    if not dup:
        stages.append(_missing("idempotent_retry",
                               "no request was delivered twice"))
    else:
        ok = all(
            len({(x.detail.get("decision"), x.detail.get("credit_after"))
                 for x in v}) == 1
            and sum(not x.detail.get("replay") for x in v) == 1
            for v in dup.values())
        stages.append(_check(
            "idempotent_retry", ok,
            [x.event_id for v in dup.values() for x in v],
            "a duplicated request changed the decision or credit twice",
            "a duplicated request was answered as a replay; credit moved"
            " once"))
    return stages


@validator("loom_health_access_portal_down")
def loom_portal_down(spec, trace):
    decisions = trace.find("loom_decision")
    received = trace.find("context_received")
    unreachable = trace.find("loom_unavailable")
    leaked = [e.event_id for e in received if e.detail.get("fields_received")]
    stages = [
        _check("loom_unreachable_observed", bool(unreachable),
               [e.event_id for e in unreachable],
               "the gateway did not record that LOOM was unreachable",
               "the gateway recorded the outage"),
        _check("fail_closed",
               bool(decisions) and not leaked and all(
                   e.detail.get("decision") == "denied"
                   and e.detail.get("reason") == "loom_unreachable"
                   for e in decisions),
               [e.event_id for e in decisions] + leaked,
               "a request was answered with data while LOOM was down",
               "every request was denied with no data while LOOM was"
               " unreachable"),
    ]
    return stages
