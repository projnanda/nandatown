# SPDX-License-Identifier: MIT
# Copyright (c) 2026 nanda-resilience contributors
from __future__ import annotations

import hashlib
import random
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nandatown.sim.scenario import AgentSpec, ScenarioSpec

from . import EVALUATOR

Policy = Literal["price", "random", "load", "dependency", "oracle"]
Condition = Literal["clean", "shared", "independent"]
POLICIES: tuple[Policy, ...] = ("price", "random", "load", "dependency", "oracle")
CONDITIONS: tuple[Condition, ...] = ("clean", "shared", "independent")


def stable_rank(*parts: object) -> int:
    text = "|".join(str(part) for part in parts)
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    seed: int = Field(default=7, ge=0)
    policy: Policy = "dependency"
    condition: Condition = "shared"
    disclosure: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)
    buyers: int = Field(default=8, ge=1, le=32)
    rounds: int = Field(default=20, ge=18, le=100)
    attempts: int = Field(default=3, ge=1, le=12)
    interval: float = Field(default=2.0, ge=2.0, le=10, allow_inf_nan=False)
    service_time: float = Field(default=0.35, gt=0, le=0.5, allow_inf_nan=False)
    dependency_capacity: int = Field(default=12, ge=1, le=100)


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str
    dependency: str
    disclosed: bool
    price_cents: int = Field(ge=1)
    outages: list[tuple[float, float]] = []


class World(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    settings: Settings
    providers: list[Provider]
    dependency_outages: dict[str, list[tuple[float, float]]]

    @model_validator(mode="after")
    def valid_world(self):
        names = [provider.name for provider in self.providers]
        if len(set(names)) != len(names) or not names:
            raise ValueError("provider names must be nonempty and unique")
        for provider in self.providers:
            if provider.dependency not in self.dependency_outages:
                raise ValueError("provider refers to an unknown dependency")
        intervals = [
            interval
            for outages in self.dependency_outages.values()
            for interval in outages
        ] + [interval for p in self.providers for interval in p.outages]
        if any(not 0 <= start < end <= self.end_time for start, end in intervals):
            raise ValueError("outages must be ordered intervals inside the run")
        return self

    @property
    def end_time(self) -> float:
        return 1.0 + (self.settings.rounds - 1) * self.settings.interval + 5.0

    @property
    def last_outage_end(self) -> float | None:
        ends = [end for windows in self.dependency_outages.values()
                for _, end in windows]
        ends += [end for p in self.providers for _, end in p.outages]
        return max(ends) if ends else None


def make_world(settings: Settings) -> World:
    rng = random.Random(stable_rank("faults", settings.seed))
    names = [f"provider-{i:02d}" for i in range(12)]
    # Paired policies share the same disclosure mask and fault schedule.
    disclosure_order = sorted(names, key=lambda name: stable_rank("disclosure", settings.seed, name))
    disclosed = set(disclosure_order[:round(12 * settings.disclosure)])
    independent: dict[str, list[tuple[float, float]]] = {}
    if settings.condition == "independent":
        for name in rng.sample(names, 9):
            start = round(rng.uniform(4.0, 20.0), 6)
            independent[name] = [(start, start + 16.0)]
    providers = [
        Provider(
            name=name,
            dependency="backend-0" if i < 9 else f"backend-{i - 8}",
            disclosed=name in disclosed,
            price_cents=10 if i < 9 else 12 + 2 * (i - 9),
            outages=independent.get(name, []),
        )
        for i, name in enumerate(names)
    ]
    outages = {f"backend-{i}": [] for i in range(4)}
    if settings.condition == "shared":
        outages["backend-0"] = [(12.0, 28.0)]
    return World(settings=settings, providers=providers,
                 dependency_outages=outages)


def build_spec(settings: Settings) -> ScenarioSpec:
    world = make_world(settings)
    agents = [
        AgentSpec(name="experiment", role="resilience_clock",
                  config={"world": world.model_dump(mode="json")}),
        *[
            AgentSpec(
                name=name, role="resilience_backend",
                config={"outages": [list(window) for window in outages],
                        "capacity": settings.dependency_capacity,
                        "service_time": settings.service_time},
            )
            for name, outages in world.dependency_outages.items()
        ],
        *[
            AgentSpec(name=p.name, role="resilience_provider",
                      config=p.model_dump(mode="json"))
            for p in world.providers
        ],
        *[
            AgentSpec(
                name=f"buyer-{i:02d}", role="resilience_buyer",
                config={"rounds": settings.rounds, "interval": settings.interval,
                        "attempts": settings.attempts,
                        "balance_cents": settings.rounds * 20},
            )
            for i in range(settings.buyers)
        ],
    ]
    return ScenarioSpec(
        name=f"resilience-{settings.condition}",
        description="Shared dependencies, bounded retries, and partial disclosure.",
        seed=settings.seed, agents=agents,
        layers={"coordination": f"resilience.{settings.policy}.v1",
                "registry": "index.owned.v1"},
        validator=EVALUATOR, max_time=world.end_time,
    )


def world_from_spec(spec: ScenarioSpec) -> World:
    clocks = [a for a in spec.agents if a.role == "resilience_clock"]
    if len(clocks) != 1:
        raise ValueError("exactly one resilience_clock is required")
    # JSON validation accepts serialized interval arrays while preserving strict types.
    import json
    return World.model_validate_json(json.dumps(clocks[0].config["world"]))
