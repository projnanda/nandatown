# SPDX-License-Identifier: MIT
# Copyright (c) 2026 nanda-resilience contributors
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from nandatown.layers import register
from nandatown.layers.coordination import ContractNet
from nandatown.records import TownEvent

from .model import POLICIES, Policy, stable_rank, world_from_spec

DEPENDENCY_FAILURES = frozenset({"unavailable", "capacity"})


@dataclass(frozen=True)
class Candidate:
    name: str
    price: int
    reputation: int
    dependency: str | None


def choose(
    policy: Policy, candidates: list[Candidate], *, seed: int, task: str,
    attempt: int, inflight: Counter[str], reservations: dict[str, str],
    failed_groups: set[str],
) -> Candidate:
    if not candidates:
        raise ValueError("cannot select from an empty candidate set")
    group_load = Counter(reservations.values())

    def key(candidate: Candidate) -> tuple:
        tie = stable_rank(seed, task, attempt, candidate.name)
        price = (candidate.price, -candidate.reputation, tie)
        if policy == "price":
            return price
        if policy == "random":
            return (tie,)
        if policy == "load":
            return (inflight[candidate.name], *price)
        # Unknown is one conservative group, not a fabricated independent backend.
        group = candidate.dependency or "unknown"
        return (group in failed_groups, group_load[group],
                inflight[candidate.name], *price)

    return min(candidates, key=key)


class ResilienceCoordination(ContractNet):
    """Shared reservation service; no access to outage schedules in selection."""

    policy: Policy
    subscribes_to = ("resilience.attempt_finished",)

    def __init__(self, engine):
        super().__init__(engine)
        self.inflight: Counter[str] = Counter()
        self.reservations: dict[str, str] = {}
        self.owners: dict[str, str] = {}
        self.hidden_dependencies: dict[str, str] = {}
        if self.policy == "oracle":
            self.hidden_dependencies = {
                p.name: p.dependency for p in world_from_spec(engine.spec).providers
            }

    def select(self, buyer: str, task: str, attempt: int,
               cards: list[dict[str, Any]], tried: set[str],
               failed_groups: set[str]) -> tuple[Candidate, str]:
        candidates = []
        for card in cards:
            if card["name"] in tried:
                continue
            facts = card["facts"]
            dependency = (self.hidden_dependencies[card["name"]]
                          if self.policy == "oracle"
                          else facts.get("dependency"))
            candidates.append(Candidate(
                card["name"], facts["price_cents"],
                self.engine.layers["trust"].score(card["name"]), dependency,
            ))
        selected = choose(
            self.policy, candidates, seed=self.engine.spec.seed, task=task,
            attempt=attempt, inflight=self.inflight,
            reservations=self.reservations, failed_groups=failed_groups,
        )
        ref = f"{task}/attempt-{attempt}"
        if ref in self.owners:
            raise ValueError(f"duplicate reservation {ref}")
        self.inflight[selected.name] += 1
        self.owners[ref] = selected.name
        self.reservations[ref] = selected.dependency or "unknown"
        self.engine.emit(buyer, "resilience.selected", ref, {
            "task": task, "attempt": attempt, "provider": selected.name,
            "price_cents": selected.price, "policy": self.policy,
            "visible_dependency": selected.dependency,
            "candidate_count": len(candidates),
        })
        return selected, ref

    def on_event(self, event: TownEvent) -> None:
        provider = self.owners.pop(event.subject)
        self.inflight[provider] -= 1
        del self.reservations[event.subject]


for _policy in POLICIES:
    register("coordination", f"resilience.{_policy}.v1")(
        type(f"{_policy.title()}Coordination", (ResilienceCoordination,),
             {"policy": _policy, "__doc__": f"{_policy} selection with bounded retries."})
    )
