"""Registry snapshot profile: identity claims checked against a record that
outlives the run.

A card may carry a claim that an external registry identity speaks through
this agent. The claim is a statement signed, once and outside any run, by
the identity's controller key:

    {"statement": "registry-claim/v1", "agent": <town agent name>,
     "identity": <identity id>, "registry": <registry id>}

The registry layer checks the claim when the agent publishes its own card
(signed by its own town key): the statement must verify under the
controller key it presents, and the hash of that key must be the identity's
current controller in a pinned registry snapshot. A key the snapshot
recorded for the identity and has since moved away from is refused as a
former controller, however valid its signatures. A signature cannot see a
transfer; only a record that outlives the run can.

The snapshot is captured before the run, pinned by fingerprint, and
recorded in the trace in full. Each claim event records the card, its town
signature and the claim, so the validator recomputes every decision from
events.jsonl. No chain call, wall clock or private controller key enters a
run: agents sign town messages with seeded per-run keys.

    identity  claims.v1                 seeded Ed25519 town keys; cards carry
                                        configured claims
    auth      ed25519.v1                Ed25519 under the sender's own town
                                        key; refuses senders whose own claim
                                        the registry refused
    registry  snapshot.mock.v1          synthetic snapshot
              snapshot.mock.sigonly.v1  control: claim signatures checked,
                                        the record not compared
    validator registry_rotation         version registry-rotation-0.2

The registries extend index.owned.v1, so only a name's own town key changes
its listing. A source adapter adds registries for its own
captured snapshot by subclassing SnapshotIndex with a source, a pinned
fingerprint and a snapshot path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from nandatown.layers import UnknownPlugin, register, resolve
from nandatown.layers.registry_layer import OwnedIndex
from nandatown.records import StageResult, canonical_json, fingerprint
from nandatown.sim.agents import Seller, role
from nandatown.sim.validators import Trace, validator

HERE = Path(__file__).resolve().parent
SCHEMA = "registry-snapshot/v1"
STATEMENT = "registry-claim/v1"
VALIDATOR_VERSION = "registry-rotation-0.2"
MOCK_PIN = "sha256:77ab614f48fdc852c568e5c1c825c76f1dba7ef4c5a37359d6913ed4bdcb6a4e"
SNAPSHOT_FIELDS = ("schema", "source", "network", "registry", "key_hash", "tip",
                   "identities")


def file_sha256(path: str | Path) -> str:
    """sha256 of a file with line endings normalized, so a checkout on any
    platform reports the same value."""
    raw = Path(path).read_bytes().replace(b"\r\n", b"\n")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def key_hash(pub_key_hex: str) -> str:
    """blake2b-224 of the raw Ed25519 public key (the snapshot's key_hash)."""
    return hashlib.blake2b(bytes.fromhex(pub_key_hex),
                           digest_size=28).hexdigest()


def claim_statement(agent: str, identity: str, registry: str) -> dict:
    return {"statement": STATEMENT, "agent": agent, "identity": identity,
            "registry": registry}


def signed_by(pub_key_hex: Any, payload: Any, signature: Any) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_key_hex)).verify(
            bytes.fromhex(signature), canonical_json(payload).encode())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def replay_history(record: dict, tip_slot: int) -> str:
    """Why a record is inconsistent, or "" when its history derives exactly
    its status and controller, in order, at or before the tip."""
    owner, status, last = None, None, -1
    for i, h in enumerate(record.get("history", [])):
        slot = h.get("slot")
        if not isinstance(slot, int) or slot < last or slot > tip_slot:
            return f"history entry {i} has slot {slot} out of order or after the tip"
        last = slot
        event = h.get("event")
        if i == 0:
            if event != "registered":
                return "history must start with registered"
            owner, status = h.get("owner_pkh"), "active"
        elif status != "active":
            return f"history entry {i} follows retirement"
        elif event == "controller_rotated":
            if h.get("from_pkh") != owner:
                return f"history entry {i} rotates from a key that was not the controller"
            owner = h.get("to_pkh")
        elif event == "retired":
            owner, status = None, "retired"
        elif event != "updated":
            return f"history entry {i} has unknown event {event!r}"
    if status is None:
        return "record has no history"
    if record.get("status") != status or record.get("owner_pkh") != owner:
        return "status or controller does not match its own history"
    return ""


def former_rotation(record: dict, pkh: str) -> dict | None:
    """The last rotation that moved control away from pkh, if any."""
    moved = [h for h in record.get("history", [])
             if h.get("event") == "controller_rotated" and h.get("from_pkh") == pkh]
    return moved[-1] if moved else None


def registered_pin(registry_plugin: str, source: str) -> str | None:
    """The pin of the run's registry plugin, if it reads this source."""
    try:
        cls = resolve("registry", registry_plugin)
    except UnknownPlugin:
        return None
    return cls.pinned if getattr(cls, "source", None) == source else None


# -- identity ------------------------------------------------------------

@register("identity", "claims.v1")
class ClaimsIdentity:
    """Seeded Ed25519 town keys; a card may carry a configured registry claim."""

    def __init__(self, engine):
        self.engine = engine
        self.identities: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self.claims = {a.name: a.config["claim"] for a in engine.spec.agents
                       if isinstance(a.config.get("claim"), dict)}

    def create(self, name: str) -> dict[str, Any]:
        if name in self.identities:
            return self.identities[name]
        self._seq += 1
        sk = Ed25519PrivateKey.from_private_bytes(
            bytes(self.engine.rng.getrandbits(8) for _ in range(32)))
        pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        ident = {"name": name, "agent_id": f"aid-{self._seq}", "sk": sk,
                 "pub_key_hex": pub}
        self.identities[name] = ident
        self.engine.emit("town", "identity_created", name,
                         {"agent_id": ident["agent_id"], "town_key": pub})
        return ident

    def key(self, name: str) -> str | None:
        """A town-local secret, for hmac.v1."""
        ident = self.identities.get(name)
        return hashlib.sha256(ident["sk"].private_bytes_raw()).hexdigest() \
            if ident else None

    def public_key(self, name: str) -> str | None:
        ident = self.identities.get(name)
        return ident["pub_key_hex"] if ident else None

    def card(self, name: str, capabilities: list[str],
             facts: dict[str, Any]) -> dict[str, Any]:
        ident = self.create(name)
        facts = dict(facts)
        if name in self.claims:
            facts["registry_claim"] = dict(self.claims[name])
        return {"name": name, "agent_id": ident["agent_id"],
                "capabilities": capabilities, "facts": facts}


# -- auth ----------------------------------------------------------------

@register("auth", "ed25519.v1")
class Ed25519Auth:
    """Ed25519 under the sender's own town key; refuses unbound senders.

    Every call is judged the same way, whatever the payload looks like:
    the signature must verify under the town key of the name it claims.
    A name whose own claim the registry refused cannot sign anything that
    verifies. Needs identity claims.v1.
    """

    def __init__(self, engine):
        self.engine = engine

    def sign_as(self, name: str, payload: Any) -> str:
        ident = self.engine.layers["identity"].create(name)
        if "sk" not in ident:
            raise TypeError("auth ed25519.v1 needs identity claims.v1")
        return ident["sk"].sign(canonical_json(payload).encode()).hex()

    def verify(self, claimed_name: str, payload: Any, signature: str,
               subject: str = "") -> bool:
        unbound = getattr(self.engine.layers["registry"], "unbound", {})
        if claimed_name in unbound:
            self.engine.emit("town", "registry_sender_unbound",
                             subject or claimed_name,
                             {"sender": claimed_name,
                              "identity": unbound[claimed_name]})
            return False
        identity = self.engine.layers["identity"]
        ok = signed_by(identity.public_key(claimed_name), payload, signature)
        if not ok:
            self.engine.emit("town", "signature_invalid",
                             subject or claimed_name, {"claimed": claimed_name})
        return ok


# -- registry ------------------------------------------------------------

class SnapshotIndex(OwnedIndex):
    """index.owned.v1 plus a registry-claim check when an agent publishes
    its own card. Subclasses set source, pinned and snapshot_path."""

    source = ""
    pinned = ""
    snapshot_path: Path = Path()
    compare_records = True
    defined_in = __file__  # a source adapter's subclass sets its own

    def __init__(self, engine):
        super().__init__(engine)
        self.unbound: dict[str, str] = {}
        self._announced = False
        self.snapshot, self.problem = self._load(self.snapshot_path)

    def _load(self, path: Path) -> tuple[dict | None, str]:
        try:
            snap = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, f"no snapshot at {Path(path).name}"
        except ValueError:
            return None, f"{Path(path).name} is not JSON"
        if not isinstance(snap, dict) or snap.get("schema") != SCHEMA:
            return None, f"{Path(path).name} is not a {SCHEMA} snapshot"
        if fingerprint(snap) != self.pinned:
            return None, (f"{Path(path).name} fingerprint {fingerprint(snap)}"
                          f" is not the pinned {self.pinned}")
        if snap.get("source") != self.source:
            return None, f"snapshot source {snap.get('source')!r} is not {self.source!r}"
        tip = snap.get("tip", {}).get("slot")
        for ident, record in snap.get("identities", {}).items():
            why = replay_history(record, tip if isinstance(tip, int) else -1)
            if why:
                return None, f"inconsistent record for {ident}: {why}"
        return snap, ""

    def _announce(self) -> None:
        if self._announced:
            return
        self._announced = True
        source_file = Path(self.defined_in).resolve()
        try:
            rel = source_file.relative_to(HERE).as_posix()
        except ValueError:
            rel = source_file.name
        base = {"source": self.source, "compare_records": self.compare_records,
                "validator_version": VALIDATOR_VERSION,
                "profile_sha256": file_sha256(__file__),
                "source_file": rel, "source_sha256": file_sha256(source_file)}
        if self.snapshot is None:
            self.engine.emit("town", "registry_snapshot_unavailable", "registry",
                             dict(base, reason=self.problem))
            return
        s = self.snapshot
        self.engine.emit("town", "registry_snapshot_loaded", "registry", dict(
            base, fingerprint=fingerprint(s), pinned=self.pinned,
            **{k: s.get(k) for k in SNAPSHOT_FIELDS}))

    def lookup(self, capability: str, include_unverified: bool = False):
        self._announce()
        return super().lookup(capability, include_unverified)

    def publish(self, publisher: str, card: dict[str, Any],
                signature: str) -> bool:
        self._announce()
        facts = card.get("facts") if isinstance(card.get("facts"), dict) else {}
        claim = facts.get("registry_claim")
        name = card["name"]
        identity = self.engine.layers["identity"]
        town_key = identity.public_key(name)
        if claim is None or not signed_by(town_key, card, signature):
            # A card the name's own town key did not sign is never evaluated
            # as a claim and changes nothing about the name: index.owned.v1
            # refuses it or stores it unverified, as for any forgery.
            return super().publish(publisher, card, signature)
        evidence = {"publisher": publisher, "card": card,
                    "card_signature": signature, "town_key": town_key}

        configured = identity.claims.get(name)
        if not isinstance(claim, dict) or configured is None \
                or claim.get("identity") != configured.get("identity"):
            return self._refuse_claim(card, "claim_not_configured_for_agent", evidence)
        if self.snapshot is None:
            return self._refuse_claim(card, "snapshot_unavailable",
                                      dict(evidence, detail=self.problem))
        statement = claim_statement(name, claim["identity"],
                                    self.snapshot.get("registry"))
        if not signed_by(claim.get("controller_pub"), statement,
                         claim.get("signature")):
            return self._refuse_claim(card, "claim_signature_invalid", evidence)
        evidence.update(identity=claim["identity"],
                        controller_pub=claim["controller_pub"],
                        controller_pkh=key_hash(claim["controller_pub"]),
                        signature=claim["signature"])
        if not self.compare_records:
            return self._list(publisher, card, signature, "registry_claim_unchecked",
                              dict(evidence, reason="record comparison disabled"))
        record = self.snapshot.get("identities", {}).get(claim["identity"])
        if record is None:
            return self._refuse_claim(card, "identity_unknown", evidence)
        if record["status"] != "active":
            return self._refuse_claim(card, "identity_retired", evidence)
        if record["owner_pkh"] != evidence["controller_pkh"]:
            moved = former_rotation(record, evidence["controller_pkh"])
            return self._refuse_claim(card, "controller_mismatch", dict(
                evidence, owner_pkh=record["owner_pkh"],
                rotation_tx=moved and moved.get("tx")))
        return self._list(publisher, card, signature, "registry_claim_resolved",
                          dict(evidence, owner_pkh=record["owner_pkh"],
                               registry_ref=record.get("registry_ref")))

    def _list(self, publisher, card, signature, kind, evidence) -> bool:
        """List the card through index.owned.v1; record the claim only if it
        was actually listed."""
        listed = super().publish(publisher, card, signature)
        if listed:
            self.engine.emit("town", kind, card["name"], evidence)
        return listed

    def _refuse_claim(self, card: dict[str, Any], reason: str,
                      evidence: dict) -> bool:
        # Called only for a card signed by the name's own town key: the
        # agent's own claim failed, so the agent is unbound.
        name = card["name"]
        claim = card["facts"].get("registry_claim")
        self.unbound[name] = claim.get("identity") if isinstance(claim, dict) else None
        self.engine.emit("town", "registry_claim_refused", name,
                         dict(evidence, reason=reason))
        self.engine.emit("town", "card_publish_refused", name,
                         {"capabilities": card.get("capabilities"),
                          "publisher": evidence["publisher"],
                          "reason": f"registry claim refused: {reason}"})
        return False


@register("registry", "snapshot.mock.v1")
class MockSnapshotIndex(SnapshotIndex):
    """Registry claims checked against the synthetic mock snapshot."""
    source = "mock"
    pinned = MOCK_PIN
    snapshot_path = HERE / "snapshots" / "mock.json"


@register("registry", "snapshot.mock.sigonly.v1")
class MockSigOnlyIndex(MockSnapshotIndex):
    """Control: claim signatures checked, the mock snapshot record not."""
    compare_records = False


@role("former_controller")
class FormerController(Seller):
    """A seller configured with a claim signed by a key the registry has
    since moved the identity away from, and which presents it. Its
    signatures are all valid; it delivers honestly. A former controller
    that leaves its claim off is an ordinary seller and is not refused by
    the town (the validator still fails a run that lists or pays it)."""


# -- validator -----------------------------------------------------------

def _stage(name, status, evidence=(), note=""):
    return StageResult(name=name, status=status, evidence=list(evidence)[:8], note=note)


def _ids(events) -> list[str]:
    return [e.event_id for e in events]


def _claim_recomputes(event, snap: dict, agent: str, town_keys: dict) -> str:
    """Why a claim event's recorded evidence does not support it, or ""."""
    d = event.detail
    card = d.get("card")
    if not isinstance(card, dict) or card.get("name") != agent:
        return "the recorded card is not this agent's"
    if d.get("town_key") != town_keys.get(agent):
        return "the recorded town key is not the agent's"
    if not signed_by(d.get("town_key"), card, d.get("card_signature")):
        return "the recorded card is not signed by the agent's town key"
    claim = (card.get("facts") or {}).get("registry_claim") or {}
    if (claim.get("controller_pub"), claim.get("signature"), claim.get("identity")) != \
            (d.get("controller_pub"), d.get("signature"), d.get("identity")):
        return "the recorded claim is not the one on the card"
    statement = claim_statement(agent, d.get("identity"), snap.get("registry"))
    if not signed_by(d.get("controller_pub"), statement, d.get("signature")):
        return "the recorded claim signature does not verify"
    if key_hash(d.get("controller_pub") or "00") != d.get("controller_pkh"):
        return "the recorded key hash is not the hash of the recorded key"
    return ""


def _not_former(event, records: dict) -> str:
    """Why a refusal's key is not a former controller of its identity in
    the recorded snapshot, or ""."""
    record = records.get(event.detail.get("identity"), {})
    pkh = event.detail.get("controller_pkh")
    if record.get("status") != "active" or record.get("owner_pkh") == pkh:
        return "the recorded identity is not active under another key"
    if not former_rotation(record, pkh):
        return "the recorded history never moved control away from this key"
    return ""


@validator("registry_rotation")
def registry_rotation(spec, trace: Trace) -> list:
    """Recomputed from events.jsonl alone, per agent: every former
    controller's own claim is refused on the snapshot record before any
    traffic with it, and it is never listed, contacted or paid; every
    current controller's own claim resolves to the recorded controller and
    it is paid for an order placed after that. Without a loaded, pinned,
    consistent snapshot that holds every configured identity, nothing is
    judged."""
    rotation = ["former_controller_refused", "former_controller_contained",
                "current_controller_verified", "current_controller_paid"]
    formers = [a.name for a in spec.agents if a.role == "former_controller"]
    currents = [a.name for a in spec.agents if a.role == "seller"
                and isinstance(a.config.get("claim"), dict)]
    configured = {a.name: a.config["claim"].get("identity") for a in spec.agents
                  if isinstance(a.config.get("claim"), dict)}

    def judged_nothing(first, note):
        return [first] + [_stage(n, "not_enough_evidence", note=note) for n in rotation]

    loaded = trace.find("registry_snapshot_loaded")
    unavailable = trace.find("registry_snapshot_unavailable")
    if not loaded or unavailable or len(loaded) > 1:
        why = unavailable[0].detail.get("reason") if unavailable else "no single snapshot event"
        return judged_nothing(_stage("snapshot_loaded", "not_enough_evidence",
                                     note=f"no registry snapshot: {why}"),
                              "needs a loaded registry snapshot")
    snap_event = loaded[0]
    snap = snap_event.detail
    if snap.get("validator_version") != VALIDATOR_VERSION:
        return judged_nothing(_stage("snapshot_loaded", "not_enough_evidence",
                                     note=f"bundle judged by {snap.get('validator_version')},"
                                          f" this validator is {VALIDATOR_VERSION}"),
                              "validator version differs")
    rebuilt = fingerprint({k: snap.get(k) for k in SNAPSHOT_FIELDS})
    pin = registered_pin(spec.layers.get("registry", ""), snap.get("source"))
    if not (rebuilt == snap.get("fingerprint") == pin):
        return judged_nothing(_stage("snapshot_loaded", "error", _ids([snap_event]),
                                     f"recorded snapshot rebuilds to {rebuilt}; recorded"
                                     f" {snap.get('fingerprint')}; pinned {pin}"),
                              "needs the pinned snapshot")
    tip = (snap.get("tip") or {}).get("slot", -1)
    records = snap.get("identities") or {}
    inconsistent = [f"{i}: {w}" for i, r in records.items() if (w := replay_history(r, tip))]
    if inconsistent:
        return judged_nothing(_stage("snapshot_loaded", "error", _ids([snap_event]),
                                     "recorded snapshot is inconsistent: "
                                     + "; ".join(inconsistent)),
                              "needs a consistent registry snapshot")
    missing = sorted({i for i in configured.values() if i not in records})
    if missing:
        return judged_nothing(_stage("snapshot_loaded", "not_enough_evidence",
                                     _ids([snap_event]),
                                     f"the snapshot does not hold {', '.join(missing)}"),
                              "the record cannot judge an identity it does not hold")
    if formers and currents and len({configured.get(a) for a in formers + currents}) != 1:
        return judged_nothing(_stage("snapshot_loaded", "not_enough_evidence",
                                     _ids([snap_event]),
                                     "former and current controllers claim different identities"),
                              "not a rotation of one identity")
    compare = snap.get("compare_records") is not False
    stages = [_stage("snapshot_loaded", "passed", _ids([snap_event]),
                     f"{snap.get('source')} snapshot {rebuilt} (pinned), tip slot {tip};"
                     f" records compared: {compare}")]
    town_keys = {e.subject: e.detail.get("town_key") for e in trace.find("identity_created")}
    failed_ids = {e.subject for e in trace.find("delivery_failed")}
    delivered_ids = {e.subject for e in trace.find("message_delivered")}

    def sent_to(agent):
        return [e for e in trace.find("message_sent") if e.detail.get("to") == agent]

    def accepted_from(agent):
        return [e for e in trace.find("message_sent") if e.observer == agent
                and e.subject in delivered_ids and e.subject not in failed_ids]

    def listed(agent):
        return [e for e in trace.find("card_registered", verified=True) if e.subject == agent]

    # Former controllers.
    if not formers:
        stages += [_stage(n, "not_enough_evidence", note="the scenario has no former controller")
                   for n in rotation[:2]]
    else:
        if not compare:
            stages.append(_stage("former_controller_refused", "not_tested",
                                 note="control: records are not compared, so no claim"
                                      " can be refused on the record"))
        else:
            ok, evidence, notes = True, [], []
            for agent in formers:
                traffic = sent_to(agent) + accepted_from(agent)
                first = min((trace.index(e) for e in traffic), default=len(trace.events))
                good = []
                for e in trace.find("registry_claim_refused", reason="controller_mismatch"):
                    if e.subject != agent or trace.index(e) > first:
                        continue
                    why = (_claim_recomputes(e, snap, agent, town_keys)
                           or ("" if e.detail.get("identity") == configured.get(agent)
                               else "the refused identity is not the agent's configured one")
                           or _not_former(e, records))
                    if why:
                        notes.append(f"{agent} ({e.event_id}): {why}")
                    else:
                        good.append(e)
                if good:
                    evidence += _ids(good[:1])
                else:
                    ok = False
                    notes.append(f"{agent} was not refused as a former controller before"
                                 f" its first traffic")
                    evidence += _ids(traffic[:2] + listed(agent))
            stages.append(_stage("former_controller_refused", "passed" if ok else "failed",
                                 evidence or trace.ids("run_finished"), "; ".join(notes)))

        ok, evidence, counts = True, [], []
        for agent in formers:
            releases = trace.find("escrow_released", to=agent)
            settled = trace.find("payment_settled", to=agent)
            bad = listed(agent) + sent_to(agent) + accepted_from(agent) + releases + settled
            counts.append(f"{agent}: {len(listed(agent))} listing(s), {len(sent_to(agent))}"
                          f" sent to, {len(accepted_from(agent))} accepted from,"
                          f" {len(releases)} escrow release(s), {len(settled)} settlement(s)")
            if bad:
                ok = False
                evidence += _ids(bad)
        stages.append(_stage("former_controller_contained", "passed" if ok else "failed",
                             evidence if not ok else trace.ids("run_finished"),
                             "; ".join(counts)))

    # Current controllers.
    if not currents:
        stages += [_stage(n, "not_enough_evidence", note="the scenario has no claiming seller")
                   for n in rotation[2:]]
        return stages
    anchor_kind = "registry_claim_resolved" if compare else "registry_claim_unchecked"
    anchored_at, ok, evidence, notes = {}, True, [], []
    for agent in currents:
        claims = [e for e in trace.find(anchor_kind) if e.subject == agent]
        good = [e for e in claims
                if not _claim_recomputes(e, snap, agent, town_keys)
                and e.detail.get("identity") == configured.get(agent)
                and (not compare or records.get(e.detail.get("identity"), {}).get("owner_pkh")
                     == e.detail.get("controller_pkh"))]
        keys = {e.detail.get("controller_pkh") for e in claims}
        if good and len(keys) == 1 and listed(agent):
            anchored_at[agent] = trace.index(good[0])
            evidence += _ids([good[0]])
        else:
            ok = False
            evidence += _ids(claims + listed(agent))
            notes.append(f"{agent}: needs one recomputable listed claim"
                         + (" resolving to the recorded controller" if compare else "")
                         + ", and no other key")
    if not compare:
        stages.append(_stage("current_controller_verified", "not_tested",
                             note="control: records are not compared"))
    elif ok:
        stages.append(_stage("current_controller_verified", "passed", evidence))
    else:
        stages.append(_stage("current_controller_verified",
                             "failed" if evidence else "not_enough_evidence",
                             evidence, "; ".join(notes)))

    paid_ok, evidence = True, []
    for agent in currents:
        orders = {}
        for e in trace.find("message_sent", kind="purchase_order"):
            body = e.detail.get("body")
            if (e.detail.get("to") == agent and agent in anchored_at
                    and trace.index(e) > anchored_at[agent] and isinstance(body, dict)
                    and isinstance(body.get("order_id"), str)):
                orders[body["order_id"]] = trace.index(e)
        releases = [e for e in trace.find("escrow_released", to=agent)
                    if e.subject in orders and trace.index(e) > orders[e.subject]]
        if releases:
            evidence += _ids(releases[:1])
        else:
            paid_ok = False
    if paid_ok:
        stages.append(_stage("current_controller_paid", "passed", evidence))
    elif anchored_at:
        stages.append(_stage("current_controller_paid", "failed",
                             evidence or _ids(trace.find("escrow_released"))
                             or trace.ids("run_finished"),
                             "the current controller must be paid for an order placed"
                             " after its claim was listed"))
    else:
        stages.append(_stage("current_controller_paid", "not_enough_evidence",
                             note="no current controller's claim was listed, so no order"
                                  " can be judged"))
    return stages
