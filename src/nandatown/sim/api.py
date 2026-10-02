"""TownAPI: the only door between an agent and the world.

Every call an agent makes goes through a layer plugin and is recorded as
an intent, so the trace shows both what agents asked for and what the
town observed happening.
"""

from __future__ import annotations

from typing import Any


class TownAPI:
    def __init__(self, engine, name: str):
        self._engine = engine
        self.name = name

    # -- time and randomness -------------------------------------------

    @property
    def now(self) -> float:
        return self._engine.now

    @property
    def rng(self):
        return self._engine.rng

    def later(self, delay: float, fn) -> None:
        self._engine.schedule(delay, fn)

    def observe(self, kind: str, subject: str,
                detail: dict[str, Any] | None = None) -> str:
        """Record an attributed fact: this agent saw this happen."""
        return self._engine.emit(self.name, kind, subject, detail or {})

    # -- communication over transport ----------------------------------

    def send(self, to: str, kind: str, body: dict[str, Any],
             conversation: str | None = None) -> dict[str, Any]:
        comms = self._engine.layers["communication"]
        auth = self._engine.layers["auth"]
        self._engine.layers["identity"].create(self.name)
        envelope = comms.envelope(self.name, to, kind, body, conversation)
        envelope["signature"] = auth.sign_as(self.name, envelope["body"])
        self._engine.record_intent(self.name, "send",
                                   {"to": to, "kind": kind, "body": body})
        self._engine.layers["transport"].send(self.name, to, envelope)
        return envelope

    def reply(self, original: dict[str, Any], kind: str,
              body: dict[str, Any]) -> dict[str, Any]:
        return self.send(original["sender"], kind, body,
                         conversation=original["conversation"])

    # -- identity and discovery ----------------------------------------

    def register(self, capabilities: list[str],
                 facts: dict[str, Any] | None = None,
                 forge_key_of: str | None = None) -> bool:
        """Publish this agent's card. A spoofer can try to sign with a
        forged key by naming another agent; verification then fails."""
        identity = self._engine.layers["identity"]
        auth = self._engine.layers["auth"]
        registry = self._engine.layers["registry"]
        card = identity.card(self.name, capabilities, facts or {})
        signer = forge_key_of or self.name
        if forge_key_of:
            identity.create(forge_key_of)
            signature = auth.sign_as(signer, dict(card, name=forge_key_of))
        else:
            signature = auth.sign_as(signer, card)
        self._engine.record_intent(self.name, "register",
                                   {"capabilities": capabilities})
        return registry.publish(self.name, card, signature)

    def lookup(self, capability: str) -> list[dict[str, Any]]:
        self._engine.record_intent(self.name, "lookup",
                                   {"capability": capability})
        return self._engine.layers["registry"].lookup(capability)

    # -- payments -------------------------------------------------------

    def pay(self, to: str, cents: int, memo: str) -> None:
        self._engine.record_intent(self.name, "pay",
                                   {"to": to, "cents": cents, "memo": memo})
        self._engine.layers["payments"].transfer(self.name, to, cents, memo)

    def escrow_hold(self, cents: int, ref: str) -> None:
        self._engine.record_intent(self.name, "escrow_hold",
                                   {"cents": cents, "ref": ref})
        self._engine.layers["payments"].hold(self.name, cents, ref)

    def escrow_release(self, ref: str, to: str) -> None:
        self._engine.record_intent(self.name, "escrow_release",
                                   {"ref": ref, "to": to})
        self._engine.layers["payments"].release(ref, to)

    def escrow_refund(self, ref: str) -> None:
        self._engine.record_intent(self.name, "escrow_refund", {"ref": ref})
        self._engine.layers["payments"].refund(ref)

    def balance(self) -> int:
        return self._engine.layers["payments"].balance(self.name)

    # -- memory ---------------------------------------------------------

    def remember(self, key: str, value: Any) -> None:
        self._engine.layers["memory"].remember(self.name, key, value)

    def recall(self, key: str) -> Any:
        return self._engine.layers["memory"].recall(self.name, key)

    # -- trust and facts ------------------------------------------------

    def attest(self, subject: str, claim: str, value: Any) -> str:
        self._engine.record_intent(self.name, "attest",
                                   {"subject": subject, "claim": claim,
                                    "value": value})
        return self._engine.layers["data_facts"].attest(
            self.name, subject, claim, value)

    def rate(self, subject: str, outcome: str) -> None:
        receipt = self.attest(subject, "trade.outcome", outcome)
        self._engine.layers["trust"].update(self.name, subject, outcome,
                                            receipt)

    def reputation(self, name: str) -> int:
        return self._engine.layers["trust"].score(name)

    # -- coordination ---------------------------------------------------

    def announce(self, task_id: str, spec: dict[str, Any],
                 rule: str = "lowest") -> None:
        self._engine.record_intent(self.name, "announce",
                                   {"task_id": task_id, "rule": rule})
        self._engine.layers["coordination"].announce(self.name, task_id,
                                                     spec, rule)

    def bid(self, task_id: str, cents: int) -> bool:
        self._engine.record_intent(self.name, "bid",
                                   {"task_id": task_id, "cents": cents})
        return self._engine.layers["coordination"].bid(task_id, self.name,
                                                       cents)

    def award(self, task_id: str):
        self._engine.record_intent(self.name, "award", {"task_id": task_id})
        return self._engine.layers["coordination"].award(task_id)

    def cancel_task(self, task_id: str) -> bool:
        self._engine.record_intent(self.name, "cancel_task",
                                   {"task_id": task_id})
        return self._engine.layers["coordination"].cancel(task_id, self.name)

    # -- negotiation ----------------------------------------------------

    def negotiation_start(self, seller: str, subject: str) -> str:
        return self._engine.layers["negotiation"].start(self.name, seller,
                                                        subject)

    def negotiation_offer(self, nid: str, cents: int) -> None:
        self._engine.layers["negotiation"].offer(nid, self.name, cents)

    def negotiation_accept(self, nid: str) -> int:
        return self._engine.layers["negotiation"].accept(nid, self.name)
