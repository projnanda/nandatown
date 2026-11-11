"""Lost Link's external HTTP service, exercised by three Town Lab roles.

No Lost Link application code is vendored here. The operator is an explicit
scripted human fixture, not an agent allowed to approve its own claim.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import validator

CONTRACT = "lost-link-lifecycle@1"
PRIVATE_DETAIL = "SYNTHETIC-LL-HW8-PRIVATE-green-cactus-472"
PICKUP = "Synthetic Hayden front desk, 09:00-17:00"


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if k not in {
            "private_detail", "your_private_detail", "claim_message",
            "Authorization", "X-Human-Token"}}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


class ServiceRole(SimAgent):
    def request(self, operation, method, path, body=None, expected=200,
                as_agent=False):
        credentials = json.loads(Path(os.environ["LOSTLINK_TEST_CREDENTIALS"]).read_text())
        if self.name == "operator" and not as_agent:
            headers = {"X-Human-Token": credentials["operator"]}
        else:
            headers = {"Authorization": "Bearer " + credentials[self.name]}
        start = time.perf_counter()
        response = httpx.request(method, os.environ["LOSTLINK_URL"] + path,
                                 json=body, headers=headers, timeout=3)
        payload = response.json()
        self.api.observe("lostlink.http", operation, {
            "operation": operation, "method": method, "path": path,
            "status": response.status_code, "request": public(body),
            "response": public(payload),
            "latency_ms": round((time.perf_counter() - start) * 1000, 3)})
        if response.status_code != expected:
            raise RuntimeError(f"{operation}: expected HTTP {expected}, got {response.status_code}")
        return payload


@role("lostlink.finder")
class Finder(ServiceRole):
    def on_start(self):
        self.api.register(["lostlink.found"])
        result = self.request("register_found", "POST", "/v1/reports", {
            "kind": "found", "category": "water bottle", "color": "blue",
            "location": "Hayden Library", "date": "2026-11-10",
            "description": "blue water bottle", "private_detail": PRIVATE_DETAIL,
            "request_id": "finder-bottle"})
        self.found = result["report_id"]
        self.api.later(.2, self.announce)

    def announce(self):
        owners = self.api.lookup("lostlink.owner")
        self.api.send(owners[0]["name"], "lostlink_found_available", {"found_id": self.found})


@role("lostlink.owner")
class Owner(ServiceRole):
    def on_start(self):
        self.api.register(["lostlink.owner"])
        self.case = None

    def handle_lostlink_found_available(self, message):
        cards = self.api.lookup("lostlink.found") + self.api.lookup("lostlink.human")
        self.api.observe("lostlink.discovery", "directory", {"names": sorted(c["name"] for c in cards)})
        lost = self.request("register_lost", "POST", "/v1/reports", {
            "kind": "lost", "category": "flask", "color": "navy",
            "location": "library", "date": "2026-11-10",
            "description": "navy flask", "request_id": "owner-bottle"})
        matches = self.request("find_matches", "GET", f"/v1/reports/{lost['report_id']}/matches")
        self.api.observe("lostlink.search_privacy", "search", {
            "leak": PRIVATE_DETAIL in json.dumps(matches) or "private_detail" in json.dumps(matches)})
        found = matches["candidates"][0]["report_id"]
        claim_body = {"lost_report_id": lost["report_id"], "found_report_id": found,
                      "claim_message": "It has a cactus sticker", "request_id": "owner-claim"}
        claim = self.request("submit_claim", "POST", "/v1/claims", claim_body)
        if self.case is not None:
            self.api.observe("lostlink.duplicate", self.case, {
                "same_case": claim["case_id"] == self.case,
                "report_duplicate": lost["duplicate_request"],
                "claim_duplicate": claim["duplicate_request"]})
            return
        self.case = claim["case_id"]
        self.request("pending_status", "GET", f"/v1/cases/{self.case}")
        if self.config.get("probe_permissions"):
            self.request("agent_approval_denied", "POST", f"/v1/human/cases/{self.case}/decision",
                         {"decision": "approve", "pickup_instructions": "forged"}, expected=403)
            self.request("forged_flag_denied", "POST", "/v1/claims",
                         {**claim_body, "request_id": "forged", "human_approved": True}, expected=422)
            self.request("status_after_misuse", "GET", f"/v1/cases/{self.case}")
        self.api.send("operator", "lostlink_review_requested", {"case_id": self.case})

    def handle_lostlink_clarify(self, message):
        self.request("clarification_status", "GET", f"/v1/cases/{self.case}")
        self.request("add_info", "POST", f"/v1/cases/{self.case}/info", {"text": "green"})
        self.api.send("operator", "lostlink_info_added", {"case_id": self.case})

    def handle_lostlink_approved(self, message):
        self.request("approved_status", "GET", f"/v1/cases/{self.case}")
        self.api.send("operator", "lostlink_handover", {"case_id": self.case})

    def handle_lostlink_returned(self, message):
        self.request("returned_status", "GET", f"/v1/cases/{self.case}")


@role("lostlink.human_fixture")
class HumanFixture(ServiceRole):
    def on_start(self):
        self.api.register(["lostlink.human"])

    def handle_lostlink_review_requested(self, message):
        self.case = message["body"]["case_id"]
        inbox = self.request("human_inbox", "GET", "/v1/human/inbox")
        row = next(c for c in inbox if c["case_id"] == self.case)
        self.api.observe("lostlink.operator_check", self.case, {
            "private_detail_matches_fixture": row["your_private_detail"] == PRIVATE_DETAIL,
            "claim_has_expected_clue": "cactus" in row["claim_message"]})
        self.request("need_info", "POST", f"/v1/human/cases/{self.case}/decision",
                     {"decision": "need_info", "note": "What color is the sticker?"})
        self.api.send("owner", "lostlink_clarify", {"case_id": self.case})

    def handle_lostlink_info_added(self, message):
        inbox = self.request("human_followup", "GET", "/v1/human/inbox")
        row = next(c for c in inbox if c["case_id"] == self.case)
        if row["follow_up_info"] != ["green"]:
            raise RuntimeError("operator did not receive the owner's clarification")
        self.request("human_approve", "POST", f"/v1/human/cases/{self.case}/decision",
                     {"decision": "approve", "pickup_instructions": PICKUP})
        self.api.send("owner", "lostlink_approved", {"case_id": self.case})

    def handle_lostlink_handover(self, message):
        self.request("human_returned", "POST", f"/v1/human/cases/{self.case}/returned")
        self.api.send("owner", "lostlink_returned", {"case_id": self.case})


@validator(CONTRACT)
def evaluate(spec, trace):
    """Replay recorded HTTP observations; never call the service during replay."""
    stages = []

    def check(name, events, predicate):
        if not events:
            stages.append(StageResult(name=name, status="not_enough_evidence",
                                      note="Required observation is missing"))
            return
        try:
            passed = bool(predicate(events))
        except (KeyError, IndexError, TypeError):
            passed = False
        stages.append(StageResult(name=name, status="passed" if passed else "failed",
                                  evidence=[e.event_id for e in events][:8]))

    def http(operation):
        return trace.find("lostlink.http", operation=operation)

    def body(events):
        return events[0].detail["response"]

    found, lost, matches, claim = (http(k) for k in
                                 ("register_found", "register_lost", "find_matches", "submit_claim"))
    check("town_discovery", trace.find("lostlink.discovery"),
          lambda es: all(e.detail["names"] == ["finder", "operator"] for e in es))
    check("reports_registered", found + lost,
          lambda es: bool(found and lost) and all(e.detail["status"] == 200 for e in es))
    check("target_retrieved", matches,
          lambda es: bool(found) and body(found)["report_id"] in
          [c["report_id"] for c in body(matches)["candidates"]])
    check("search_private_detail_withheld", trace.find("lostlink.search_privacy"),
          lambda es: all(e.detail["leak"] is False for e in es))
    check("claim_links_reports", claim,
          lambda es: bool(found and lost) and es[0].detail["request"]["found_report_id"] == body(found)["report_id"]
          and es[0].detail["request"]["lost_report_id"] == body(lost)["report_id"]
          and body(es)["status"] == "submitted")
    check("pickup_withheld_before_approval", http("pending_status"),
          lambda es: body(es)["status"] == "submitted" and "pickup_instructions" not in body(es))
    check("operator_receives_private_claim", trace.find("lostlink.operator_check"),
          lambda es: all(e.detail == {"private_detail_matches_fixture": True,
                                      "claim_has_expected_clue": True} for e in es))
    check("clarification_round_trip", http("clarification_status") + http("add_info"),
          lambda es: bool(http("clarification_status") and http("add_info"))
          and body(http("clarification_status"))["status"] == "needs_clarification"
          and body(http("add_info"))["status"] == "submitted")
    check("human_approval_releases_pickup", http("human_approve") + http("approved_status"),
          lambda es: bool(http("human_approve") and http("approved_status"))
          and body(http("human_approve"))["decision"] == "approve"
          and body(http("approved_status"))["status"] == "approved_for_pickup"
          and body(http("approved_status"))["pickup_instructions"] == PICKUP)
    check("return_confirmed", http("human_returned") + http("returned_status"),
          lambda es: bool(http("human_returned") and http("returned_status"))
          and all(e.detail["response"]["status"] == "returned" for e in es))
    if any(a.config.get("probe_permissions") for a in spec.agents):
        check("agent_cannot_approve", http("agent_approval_denied"),
              lambda es: all(e.detail["status"] == 403 for e in es))
        check("forged_approval_flag_refused", http("forged_flag_denied"),
              lambda es: all(e.detail["status"] == 422 for e in es))
        check("misuse_keeps_case_pending", http("status_after_misuse"),
              lambda es: body(es)["status"] == "submitted" and "pickup_instructions" not in body(es))
    if any(f.action == "duplicate" for f in spec.faults):
        check("duplicate_delivery_is_idempotent", trace.find("lostlink.duplicate"),
              lambda es: all(e.detail == {"same_case": True, "report_duplicate": True,
                                          "claim_duplicate": True} for e in es))
    return stages
