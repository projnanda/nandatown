# SPDX-License-Identifier: Apache-2.0
"""Leal in Town: a job agent gets a free recipe from Leal and follows it while it works.

Leal (https://github.com/Leal-protocol/leal) helps people trust agents with jobs that
matter. Its first product is recipes: a job someone already figured out, written down so
any agent can follow it. The job-application recipe holds the steps, rules and warnings
its creator learned from more than 20 real applications. The agent follows it while it
works, rather than being checked against it afterwards.

What a run exercises, end to end:

1. an employer publishes a job card to Town's registry
2. Leal publishes its recipe service to the same registry, with its public signing key
3. the candidate's job agent discovers the job and Leal through the registry
4. it asks Leal for the recipe over Town's messaging, and Leal answers with the recipe its
   ``get_recipe`` MCP tool returns (live, or replayed from a capture)
5. the agent follows the recipe step by step, starting with screening the posting
6. at the recipe's ``self_check`` step it sends the exact application to Leal, which runs
   its ``check_application`` MCP tool and answers with a decision and a signed attestation
7. only on approve does it submit, attaching the attestation; the employer verifies the
   signature against Leal's registry key and that the packet is the one Leal checked
8. it records the confirmation word for word, or stops at the screening step when the
   posting bars visa sponsorship, or holds the application when Leal does not approve

Loaded by ``plugin_files:`` in the scenarios beside this file. It registers three roles
and two validators without changing Town's own modules.

Example::

    nandatown run integrations/leal/scenarios/recipe_application.yaml
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import Trace, validator

_HERE = Path(__file__).resolve().parent
JOB_CAPABILITY = "hire.product-manager"
RECIPE_CAPABILITY = "leal.recipes"


def _sibling(name: str):
    """Import a module next to this file without putting it on sys.path."""
    full = f"nandatown_leal_{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _HERE / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load {name}.py beside {__file__}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


source_mod = _sibling("leal_source")


# -- the three roles -------------------------------------------------------


@role("leal_recipes")
class LealRecipes(SimAgent):
    """Leal's recipe service as a Town participant.

    It answers ``recipe_request`` with the recipe Leal's ``get_recipe`` tool returns, and
    ``application_check`` with what Leal's ``check_application`` tool decides.

    Example::

        agents: [{name: leal, role: leal_recipes}]
    """

    def on_start(self) -> None:
        self.source = source_mod.source_from_env()
        self.api.observe(
            "leal_source", "leal", {"mode": self.source.mode, "detail": self.source.detail}
        )
        facts: dict[str, Any] = {
            "recipes": ["job-applications"],
            "price": "free",
            "tools": ["get_recipe", "check_application"],
        }
        try:
            facts["public_key"] = self.source.public_key()
        except source_mod.LealUnavailable as exc:
            self.api.observe("leal_key_unavailable", "leal", {"reason": str(exc)})
        self.api.register([RECIPE_CAPABILITY], facts)

    def handle_application_check(self, msg: dict[str, Any]) -> None:
        body = msg["body"]
        packet = body["packet"]
        try:
            answer = self.source.check_application(body["profile_id"], packet)
        except source_mod.LealUnavailable as exc:
            self.api.reply(msg, "application_check_unavailable", {"reason": str(exc)})
            return
        decision = answer["decision"]
        self.api.observe(
            "application_checked",
            msg["sender"],
            {
                "outcome": decision["outcome"],
                "reasons": decision["reasons"],
                "checks": [c["name"] for c in decision.get("checks", [])],
                "packet_sha256": source_mod.packet_hash(packet),
            },
        )
        self.api.reply(msg, "application_checked", answer)

    def handle_recipe_request(self, msg: dict[str, Any]) -> None:
        recipe_id = msg["body"]["recipe_id"]
        try:
            recipe = self.source.get_recipe(recipe_id)
        except source_mod.LealUnavailable as exc:
            self.api.reply(msg, "recipe_unavailable", {"recipe_id": recipe_id, "reason": str(exc)})
            return
        self.api.observe(
            "recipe_served",
            msg["sender"],
            {
                "recipe_id": recipe_id,
                "version": recipe["version"],
                "steps": [step["id"] for step in recipe["steps"]],
            },
        )
        self.api.reply(msg, "recipe", {"recipe": recipe})


@role("employer")
class Employer(SimAgent):
    """Publishes one job and answers applications the way a portal would.

    Example::

        config: {employer: Acme Payments, role: Product Manager, posting_text: ..., sponsors: true}
    """

    def on_start(self) -> None:
        c = self.config
        self.api.register(
            [JOB_CAPABILITY],
            {"employer": c["employer"], "role": c["role"], "posting_text": c["posting_text"]},
        )

    def handle_job_application(self, msg: dict[str, Any]) -> None:
        application = msg["body"]["application"]
        attestation = msg["body"].get("leal_attestation")
        digest = source_mod.packet_hash(application)
        if attestation is not None or self.config.get("requires_leal", False):
            leal = self.api.lookup(RECIPE_CAPABILITY)
            key = leal[0].get("facts", {}).get("public_key", "") if leal else ""
            problem = source_mod.attestation_problem(attestation, application, key)
            self.api.observe(
                "attestation_verified",
                msg["sender"],
                {"ok": problem is None, "problem": problem, "packet_sha256": digest},
            )
            if problem is not None and self.config.get("requires_leal", False):
                reason = f"Leal attestation rejected: {problem}"
                self.api.observe("application_rejected", msg["sender"], {"reason": reason})
                self.api.reply(msg, "application_rejected", {"reason": reason})
                return
        needs = application["answers"].get("requires_sponsorship", "").lower() == "yes"
        if needs and not self.config.get("sponsors", True):
            reason = "this role does not offer visa sponsorship"
            self.api.observe("application_rejected", msg["sender"], {"reason": reason})
            self.api.reply(msg, "application_rejected", {"reason": reason})
            return
        text = (
            f"Thank you for applying to {self.config['role']} at {self.config['employer']}."
            " Your application has been received."
        )
        self.api.observe(
            "application_received", msg["sender"], {"confirmation": text, "packet_sha256": digest}
        )
        self.api.reply(msg, "application_received", {"confirmation": text})


@role("job_agent")
class JobAgent(SimAgent):
    """The candidate's agent: finds the job, gets Leal's recipe, follows it, applies.

    With ``use_recipe: false`` it does the same job without the recipe, as a comparison.

    Example::

        config: {employer: Acme Payments, documents: clean, use_recipe: true}
    """

    def on_start(self) -> None:
        self.recipe: dict[str, Any] | None = None
        self.job: dict[str, Any] | None = None
        self.leal: str | None = None
        self.application: dict[str, Any] | None = None
        self.waiting = False
        self.waiting_check = False
        self.api.register(["apply"])
        self.api.later(0.5, self.find_job)

    def find_job(self) -> None:
        wanted = self.config["employer"]
        cards = [
            card
            for card in self.api.lookup(JOB_CAPABILITY)
            if card.get("facts", {}).get("employer") == wanted
        ]
        if not cards:
            self.api.observe("no_job_found", self.name, {"employer": wanted})
            return
        self.job = cards[0]
        self.api.observe("job_found", self.job["name"], {"employer": wanted})
        if not self.config.get("use_recipe", True):
            self.do_the_job()
            return
        leal = self.api.lookup(RECIPE_CAPABILITY)
        if not leal:
            self.api.observe("recipe_unavailable", self.name, {"reason": "Leal not found"})
            self.do_the_job()
            return
        self.waiting = True
        self.leal = leal[0]["name"]
        self.api.send(self.leal, "recipe_request", {"recipe_id": "job-applications"})
        self.api.later(self.config.get("wait", 2.0), self.recipe_timeout)

    def handle_recipe(self, msg: dict[str, Any]) -> None:
        if not self.waiting:
            return
        self.waiting = False
        self.recipe = msg["body"]["recipe"]
        self.api.observe(
            "recipe_received", self.name, {"version": self.recipe["version"], "from": msg["sender"]}
        )
        self.do_the_job()

    def handle_recipe_unavailable(self, msg: dict[str, Any]) -> None:
        if self.waiting:
            self.waiting = False
            self.api.observe("recipe_unavailable", self.name, {"reason": msg["body"]["reason"]})
            self.do_the_job()

    def recipe_timeout(self) -> None:
        if self.waiting:
            self.waiting = False
            self.api.observe("recipe_unavailable", self.name, {"reason": "no answer from Leal"})
            self.do_the_job()

    def _step(self, step_id: str, result: str, **detail: Any) -> None:
        version = self.recipe["version"] if self.recipe else None
        self.api.observe(
            "recipe_step", self.name, {"step": step_id, "result": result, "recipe": version, **detail}
        )

    def do_the_job(self) -> None:
        assert self.job is not None
        facts = self.job["facts"]
        if self.recipe is not None:
            for step in self.recipe["steps"]:
                if step["id"] == "screen":
                    posting = facts["posting_text"].lower()
                    hits = [
                        p for p in step["stop_phrases"] if re.search(rf"\b{re.escape(p)}\b", posting)
                    ]
                    if hits:
                        self._step("screen", "stop", phrase=hits[0])
                        self.api.observe(
                            "application_stopped",
                            self.job["name"],
                            {"phrase": hits[0], "documents_written": 0},
                        )
                        return
                    self._step("screen", "pass")
                elif step["id"] == "self_check":
                    # The recipe's last step before submitting: Leal checks the exact packet.
                    self.write_documents()
                    self.ask_leal_to_check()
                    return
                elif step["id"] == "submit":
                    break
                else:
                    self._step(step["id"], "done")
        self.write_documents()
        self.submit(None)

    def write_documents(self) -> None:
        assert self.job is not None
        self.application = source_mod.build_application(self.config["documents"], self.job["facts"])
        self.api.observe(
            "documents_written",
            self.job["name"],
            {
                "documents": 2,
                "resume_pages": self.application["resume_pages"],
                "letter_pages": self.application["letter_pages"],
                "recipe": self.recipe["version"] if self.recipe else None,
            },
        )

    def ask_leal_to_check(self) -> None:
        assert self.leal is not None and self.application is not None
        self.waiting_check = True
        self.api.send(
            self.leal,
            "application_check",
            {"profile_id": source_mod.PROFILE_ID, "packet": self.application},
        )
        self.api.later(self.config.get("wait", 2.0), self.check_timeout)

    def handle_application_checked(self, msg: dict[str, Any]) -> None:
        if not self.waiting_check:
            return
        self.waiting_check = False
        decision = msg["body"]["decision"]
        outcome = decision["outcome"]
        if outcome == "approve":
            self._step("self_check", "pass", leal=outcome)
            self.submit(msg["body"]["attestation"])
            return
        self._step("self_check", "ask" if outcome == "ask_user" else "stop", leal=outcome)
        self.hold(outcome, decision["reasons"])

    def handle_application_check_unavailable(self, msg: dict[str, Any]) -> None:
        if self.waiting_check:
            self.waiting_check = False
            self._step("self_check", "stop", leal="unavailable")
            self.hold("unavailable", [msg["body"]["reason"]])

    def check_timeout(self) -> None:
        if self.waiting_check:
            self.waiting_check = False
            self._step("self_check", "stop", leal="unavailable")
            self.hold("unavailable", ["no answer from Leal"])

    def hold(self, outcome: str, reasons: list[str]) -> None:
        """Fail closed: without Leal's approval nothing is submitted in the candidate's name."""
        assert self.job is not None
        self.api.observe(
            "application_held", self.job["name"], {"leal": outcome, "reasons": reasons}
        )

    def submit(self, attestation: dict[str, Any] | None) -> None:
        assert self.job is not None and self.application is not None
        body: dict[str, Any] = {"application": self.application}
        if attestation is not None:
            body["leal_attestation"] = attestation
        self.api.send(self.job["name"], "job_application", body)
        if self.recipe is not None:
            self._step("submit", "done")

    def handle_application_received(self, msg: dict[str, Any]) -> None:
        self.api.observe(
            "confirmation_recorded", msg["sender"], {"confirmation": msg["body"]["confirmation"]}
        )

    def handle_application_rejected(self, msg: dict[str, Any]) -> None:
        self.api.observe("rejection_recorded", msg["sender"], {"reason": msg["body"]["reason"]})


# -- validators ------------------------------------------------------------


def _ids(events: list[Any]) -> list[str]:
    return [e.event_id for e in events]


def _stage(name: str, ok: bool, events: list[Any], fail: str, note: str = "") -> StageResult:
    if not events:
        return StageResult(name=name, status="not_enough_evidence", note=fail)
    return StageResult(
        name=name, status="passed" if ok else "failed", evidence=_ids(events)[:8],
        note=note if ok else fail,
    )


def _agents(spec, use_recipe: bool) -> list[str]:
    return [
        a.name
        for a in spec.agents
        if a.role == "job_agent" and bool(a.config.get("use_recipe", True)) == use_recipe
    ]


def _source_stage(trace: Trace) -> StageResult:
    events = trace.find("leal_source")
    mode = events[0].detail.get("mode") if events else None
    return _stage("leal_source_recorded", bool(events), events, "no Leal source recorded",
                  f"recipe served in {mode} mode")


def _served_stage(trace: Trace, agent: str) -> tuple[StageResult, list[str]]:
    served = trace.find("recipe_served", subject=agent)
    received = trace.find("recipe_received", subject=agent)
    steps = served[0].detail.get("steps", []) if served else []
    stage = _stage(
        "recipe_served_by_leal", bool(served and received), served + received,
        f"{agent} never received the recipe from Leal",
        f"Leal served recipe {served[0].detail.get('version')} to {agent}" if served else "",
    )
    return stage, steps


def _self_check_stage(trace: Trace, agent: str) -> StageResult:
    """Leal's check_application approved the exact packet the employer then received."""
    checked = trace.find("application_checked", subject=agent)
    received = trace.find("application_received", subject=agent)
    held = trace.find("application_held", observer=agent)
    last = checked[-1].detail if checked else {}
    same = bool(received) and received[0].detail.get("packet_sha256") == last.get("packet_sha256")
    ok = last.get("outcome") == "approve" and same
    if held:
        fail = f"Leal did not approve ({held[0].detail.get('leal')}): " + "; ".join(
            held[0].detail.get("reasons", []))
    else:
        fail = "the submitted application is not the packet Leal approved"
    return _stage(
        "recipe_self_check_passed", ok, checked + received + held, fail,
        f"Leal approved the exact packet that was submitted "
        f"({len(last.get('checks', []))} checks, sha256 {str(last.get('packet_sha256', ''))[:12]})")


def _attestation_stage(trace: Trace, agent: str) -> StageResult:
    """The employer verified Leal's signature against the key in Leal's registry card."""
    verified = trace.find("attestation_verified", subject=agent)
    ok = bool(verified) and bool(verified[0].detail.get("ok"))
    return _stage(
        "employer_verified_leal_attestation", ok, verified,
        f"the employer could not verify Leal's attestation: "
        f"{verified[0].detail.get('problem') if verified else 'none attached'}",
        "signature valid, and the packet received is the one Leal checked")


@validator("leal_recipe_application")
def leal_recipe_application(spec, trace: Trace) -> list[StageResult]:
    """The main workflow: get the recipe, follow every step, apply, record the confirmation."""
    stages = [_source_stage(trace)]
    agent = _agents(spec, True)[0]
    found = trace.find("job_found", observer=agent)
    stages.append(_stage("job_discovered", bool(found), found,
                         "the agent did not find the job in Town's registry",
                         "found through Town's registry"))
    served_stage, steps = _served_stage(trace, agent)
    stages.append(served_stage)
    done = trace.find("recipe_step", subject=agent)
    followed = [e.detail.get("step") for e in done]
    stages.append(_stage(
        "recipe_followed_in_order", bool(steps) and followed == steps, done,
        f"steps followed {followed} differ from the recipe {steps}",
        " > ".join(followed)))
    stages.append(_self_check_stage(trace, agent))
    stages.append(_attestation_stage(trace, agent))
    received = trace.find("application_received", subject=agent)
    recorded = trace.find("confirmation_recorded", observer=agent)
    same = bool(received and recorded) and (
        received[0].detail.get("confirmation") == recorded[0].detail.get("confirmation"))
    stages.append(_stage(
        "confirmation_recorded_word_for_word", same, received + recorded,
        "no employer confirmation recorded word for word",
        recorded[0].detail.get("confirmation", "") if recorded else ""))
    return stages


@validator("leal_recipe_sponsorship_bar")
def leal_recipe_sponsorship_bar(spec, trace: Trace) -> list[StageResult]:
    """The boundary case: a posting that bars sponsorship, with and without the recipe."""
    stages = [_source_stage(trace)]
    with_recipe = _agents(spec, True)[0]
    without = _agents(spec, False)[0]
    served_stage, _ = _served_stage(trace, with_recipe)
    stages.append(served_stage)

    stopped = trace.find("application_stopped", observer=with_recipe)
    wrote = trace.find("documents_written", observer=with_recipe)
    phrase = stopped[0].detail.get("phrase") if stopped else None
    stages.append(_stage(
        "recipe_agent_stopped_at_screening", bool(stopped) and not wrote, stopped + wrote,
        "the agent following the recipe wrote documents for a job it cannot get",
        f'stopped on "{phrase}", 0 documents written, nothing submitted'))

    control_wrote = trace.find("documents_written", observer=without)
    rejected = trace.find("application_rejected", subject=without)
    stages.append(_stage(
        "agent_without_recipe_applied_and_was_rejected", bool(control_wrote and rejected),
        control_wrote + rejected,
        "the comparison agent did not apply and get rejected as expected",
        f"wrote {control_wrote[0].detail.get('documents') if control_wrote else 0} documents, "
        f"rejected: {rejected[0].detail.get('reason') if rejected else ''}"))
    return stages
