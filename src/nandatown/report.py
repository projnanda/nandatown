"""The System Fitness Report: a readable view generated from the bundle.

The report is not a record. It restates the evidence in plain language,
stage by stage, and never claims more than the bundle holds.
"""

from __future__ import annotations

import time
import shlex
from typing import Any

from .url_credentials import Labeller, Scrubber, scrub

STATUS_LABEL = {
    "passed": "Passed",
    "failed": "Failed",
    "not_enough_evidence": "Inconclusive",
    "not_tested": "Not tested",
    "error": "ERROR (town fault)",
}

STAGE_MEANING = {
    "accepted": "the town committed the request before reporting success",
    "claimed": "a seller claimed the work under a lease",
    "received": "the seller acknowledged receipt through a valid fence",
    "processed": "the seller applied the task exactly once",
    "response": "the response was accepted and reached the buyer",
    "correct": "the buyer checked the total itself",
    "recovered_after_restart": "accepted work survived the crash and was"
                               " redelivered",
    "stale_fence_rejected": "the old attempt could not act after its lease",
    "duplicate_recognized": "the participant recognized work it already"
                            " handled",
    "wakeup_loss_tolerated": "a lost wake-up hint did not lose inbox work",
    "ack_retry_survived": "a lost acknowledgement was retried and recorded",
    "portable_identity": "the participant joined with a verified portable identity grant",
    "discovery": "cards published and peers found through the index",
    "negotiation": "offers alternated to an agreed price inside bounds",
    "settlement": "money moved only through recorded escrow",
    "reputation": "receipts drove the public score",
    "memory_reuse": "a remembered counterparty replaced a fresh lookup",
    "announced": "the task was announced with its award rule",
    "bidding": "on-time bids counted, late bids rejected",
    "award": "the award followed the declared rule",
    "delivery": "the item reached the winner",
    "ballots": "every voter's first ballot counted",
    "one_agent_one_vote": "a second ballot from the same voter was rejected",
    "tally": "the count matches the ballots",
    "result_broadcast": "every voter received the result",
    "quorum_commit": "commit only after a quorum of acknowledgements",
    "agreement": "every honest agent committed the same value",
    "fault_recovered": "the dropped message was retried and recovered",
    "procurement": "every component went to the lowest bid",
    "milestones": "each part was paid through its own escrow",
    "assembly_order": "parts before assembly, assembly before delivery",
    "customer_settled": "the customer paid exactly once",
    "spoof_detected": "the forged card was recorded as unverified",
    "honest_verified": "the honest card verified",
    "containment": "the spoofer got no traffic and no money",
    "honest_trade_completed": "the real trade still went through",
    "ledger_conserved": "money was conserved across every movement",
    "privacy": "declared private fields never left the run",
    "resolution": "the subject was found through the declared path",
    "agent_card_retrieval": "the agent card was fetched and digested",
    "descriptor_consistency": "the observed card matches the pinned one",
    "protocol_invocation": "a native protocol exchange returned a task",
    "semantic_result": "the task produced the required result",
    "duplicate_request": "the duplicate order caused no second"
                         " fulfillment",
}

SCOPE_SENTENCE = ("This result applies only to the named agents, releases,"
                  " scenario, failure, evaluator, and time window.")


def render_report(bundle: dict[str, Any]) -> str:
    profile = bundle["profile"]
    run = bundle["run"]
    result = bundle["result"]

    lines: list[str] = []
    add = lines.append
    add("NANDA Town System Fitness Report")
    add("=" * 40)
    add(f"Run:       {run.run_id}")
    if bundle.get("mode") == "path":
        add(f"Subject:   {run.config.get('subject')}"
            " (an already-running external agent)")
        add(f"Profile:   {profile.ref}"
            f" ({run.profile_fingerprint[:23]})")
        add(f"Condition: {profile.controlled_condition}: the same"
            " logical order is delivered twice")
    elif bundle.get("mode") == "lab":
        faults = ", ".join(f"{f.action} {f.kind}" for f in profile.faults) \
            or "none"
        add(f"Scenario:  {profile.name} (seed {run.config.get('seed')})")
        add(f"Faults:    {faults}")
        add(f"Agents:    " + ", ".join(
            f"{p['name']} ({p['role']})" for p in run.participants))
    else:
        task = profile.task
        add(f"Profile:   {profile.name} (fault: {profile.fault})")
        add(f"Task:      quote {task.quantity} x {task.sku} at"
            f" {task.unit_price_cents} cents, expecting"
            f" {task.expected_total_cents} cents")
    add(f"Releases:  " + ", ".join(f"{k} {v}"
                                   for k, v in sorted(run.releases.items())))
    if bundle.get("mode") == "lab":
        add(f"Duration:  {run.config.get('logical_time', 0):.1f} logical"
            " seconds, deterministic")
        for note in getattr(profile, "adaptations", []) or []:
            add(f"Adapted:   {note}")
    else:
        created = time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                time.gmtime(run.created_at))
        add(f"Started:   {created}")
    scope = (" (adapted reference flow only)"
             if bundle.get("mode") == "lab" and profile.validator == "adapted"
             else "")
    add(f"Verdict:   {result.verdict.upper()}{scope}")
    if result.verdict != "passed":
        broken = next((s for s in result.stages
                       if s.status in ("failed", "error")), None)
        if broken is None:
            broken = next((s for s in result.stages
                           if s.status == "not_enough_evidence"), None)
        if broken is not None:
            note = f" ({broken.note})" if broken.note else ""
            add(f"First broken stage: {broken.name}{note}")
    rerun = run.config.get("rerun_command")
    if rerun:
        add(f"Rerun:     {rerun}")
    add("")
    add("The journey: bring, connect, attempt, disrupt, inspect, improve.")
    add("")
    add("Stages (each one a separate claim with its own failure boundary):")
    name_width = max(len(s.name) for s in result.stages)
    for s in result.stages:
        label = STATUS_LABEL[s.status]
        meaning = ("not exercised in this run" if s.status == "not_tested"
                   else STAGE_MEANING.get(s.name, ""))
        evidence = f" [{', '.join(s.evidence)}]" if s.evidence else ""
        add(f"  {s.name.ljust(name_width)}  {label:<20} {meaning}{evidence}")
        if s.note:
            add(f"  {' ' * name_width}  note: {s.note}")
    add("")
    refused = sum(1 for e in bundle["events"]
                  if e.kind == "grant_permission_denied")
    invalid = sum(1 for e in bundle["events"]
                  if e.kind == "invalid_json_value_rejected")
    add(f"Events recorded: {len(bundle['events'])}."
        f" Intents recorded: {len(bundle['intents'])}."
        + (f" Refused by grant permissions: {refused}." if refused else "")
        + (f" Refused as invalid JSON values: {invalid}." if invalid
           else ""))
    add(SCOPE_SENTENCE)
    add("One run is one scoped observation, not a certificate.")
    add("Improve: fix what failed and rerun the same profile.")
    return _withhold_recorded_credentials(bundle, "\n".join(lines) + "\n")


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _withhold_recorded_credentials(bundle: dict[str, Any], text: str) -> str:
    scrubber = recorded_credentials(bundle)
    return scrubber(text) if scrubber else text


def shown_without_recorded_credentials(
        bundle: dict[str, Any]) -> dict[str, Any]:
    """bundle as a display shows it: the events, run and stage notes with
    recorded credentials withheld. The records themselves are untouched,
    and a bundle with none comes back as it is."""
    scrubber = recorded_credentials(bundle)
    if not scrubber:
        return bundle
    run, result = bundle["run"], bundle["result"]
    return dict(
        bundle,
        events=[event.model_copy(update={
            "subject": scrubber(event.subject),
            "detail": scrub(event.detail, scrubber)})
            for event in bundle["events"]],
        run=run.model_copy(update={
            "participants": scrub(run.participants, scrubber),
            "config": scrub(run.config, scrubber)}),
        result=result.model_copy(update={"stages": [
            stage.model_copy(update={"note": scrub(stage.note, scrubber)})
            for stage in result.stages]}))


def recorded_credentials(bundle: dict[str, Any]) -> Scrubber:
    """What displays of evidence recorded before labelling must withhold.

    Such a bundle can still carry credentials in its subject, its rerun
    command or a failure note quoting the endpoint. A report, a replay
    or a visualizer is display, not evidence, so it withholds them without
    touching the bundle. The credentials to withhold are the ones in the
    locators the run recorded, found the way httpx finds them; a label
    Town recorded is shown as recorded, and nothing else is searched.
    """
    scrubber = Scrubber(Labeller(withhold_only=True))
    # Everything the run's configuration recorded: the subject, and a Track
    # harness such as "a2a:http://user:secret@host", each read whole, since
    # a password may hold a space, and word by word as well. The rerun
    # command is read only word by word, where shell quoting may have
    # respelled a password: whole, "--url http://host:1 --path-profile
    # name@0.3" parses as credentials running up to that last "@".
    for key, value in bundle["run"].config.items():
        for recorded in _strings(value):
            if key != "rerun_command":
                scrubber.register(recorded)
            if any(ch.isspace() for ch in recorded):
                try:
                    words = shlex.split(recorded)
                except ValueError:
                    words = recorded.split()
                for word in words:
                    scrubber.register(word)
    for event in bundle.get("events") or []:
        if event.kind in ("resolution_hop", "resolution_failed",
                          "card_retrieved", "card_fetch_failed"):
            scrubber.register(event.subject)
            scrubber.register(event.detail.get("url"))
    for intent in bundle.get("intents") or []:
        payload = intent.payload if hasattr(intent, "payload") else intent.get(
            "payload", {})
        if isinstance(payload, dict):
            scrubber.register(payload.get("url"))
    return scrubber
